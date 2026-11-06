import asyncio
import os
import time
from .common import RUNTIME,PREFIX,encode,decode
from .task_store import WorkerStore
from .registry import capabilities
from .adapters import execute
from .text_task import execute_text
from .peer_question import execute_question
from .lease import LeaseStore, maybe_await
from .worker_kernel import ReliableTaskRunner

class TaskWorker:
    """Built-in device worker: the platform's reliable execution core.

    Envelope validation, fencing, deadline, claim/dedup, durable result persistence and the
    ack-driven resend pump come from app/worker_kernel.py, which the standalone client ships and
    imports too; this class only supplies the platform model adapters and the NATS transport.
    """

    def __init__(self,nc,row):
        self.nc,self.row=nc,row
        self.store=WorkerStore(RUNTIME/('worker-'+row['agent_id']+'.sqlite3'))
        if os.environ.get('POC_HA') == '1' and hasattr(nc,'jetstream'):
            # Cross-host fencing: validate the dispatch epoch against the shared KV lease.
            from .kv_lease import KvLease
            self.lease=KvLease(nc.jetstream(),replicas=int(os.environ.get('POC_JETSTREAM_REPLICAS','1')))
        else:
            lease_path=RUNTIME/'leases.sqlite3'
            self.lease=LeaseStore(lease_path) if lease_path.exists() else None
        self._jetstream_task=None
        self.runner=ReliableTaskRunner(identity=row['agent_id'],ledger=self.store,
                                       execute=self._execute,publish_started=self._publish_started,
                                       publish_result=self._publish_result,stale=self._stale)
        # A draining worker claimed no new task; in-flight work is left to finish.
        self.draining=False

    async def _stale(self,epoch):
        if self.lease is None:return False
        return await maybe_await(self.lease.stale('dispatch',epoch))

    async def _publish_started(self,task_id):
        await self.nc.publish(f"{PREFIX}.agent.{self.row['agent_id']}.result",
                              encode({'type':'task_started','task_id':task_id}))

    async def _publish_result(self,task_id,result):
        await self.nc.publish(f"{PREFIX}.agent.{self.row['agent_id']}.result",
                              encode({'task_id':task_id,**result}))

    async def _execute(self,envelope,grants,history):
        """Run one already-claimed task. Expired/revoked grants never reach this point."""
        if envelope['operation']!='ask_peer' or envelope['operation'] not in capabilities(self.row):
            return {'code':'UNSUPPORTED_ACTION','data':None}
        from .harness_adapter import execute as execute_harness, is_harness
        if is_harness(self.row):
            # The one-shot harness answers from the prompt, so it must see exactly the
            # Router-supplied grants (they carry the approved directories the prompt may
            # summarise); the kernel already removed anything expired or revoked.
            from .directory_context import scope as grant_scope
            def run_harness():
                with grant_scope(self.row,envelope['payload'],grants,history):
                    return execute_harness(self.row,envelope['payload'],envelope['deadline'])
            code,data=await asyncio.to_thread(run_harness)
        else:
            from .directory_context import execute as execute_with_grants
            code,data=await asyncio.to_thread(execute_with_grants,self.row,envelope['payload'],
                                              envelope['deadline'],grants,history)
        return {'code':code,'data':data}

    async def process(self,body):
        """Process one decoded task envelope through the shared reliable contract.

        Cancel envelopes are cooperative and handled separately; everything else goes through the
        kernel: validate -> fence -> claim exactly once -> check deadlines and grant validity ->
        execute -> persist. Invalid input is dropped, not raised, so a durable consumer can ack it.
        """
        if self.draining:
            # The Router stops dispatching to a draining heartbeat; this also covers a task
            # already on the wire. A durable consumer redelivers it after the worker is back.
            return
        if isinstance(body,dict) and body.get('type')=='cancel':
            await self._cancel(body)
            return
        await self.runner.handle(body)

    def stop_consuming(self):
        """Enter draining: refuse new tasks, but never abort the in-flight one.

        Cancelling the durable consumer here also cancels the handle() awaiting it, which
        aborts exactly the execution draining is meant to protect (the live restart drill
        showed the harness killed and the task then reported WORKER_INTERRUPTED). New
        deliveries are refused by process() and the Router re-dispatches them; the
        consumer is cancelled only at process exit.
        """
        self.draining=True

    def resume(self):
        """Leave maintenance draining; the consumer was never cancelled, so work flows again."""
        self.draining=False

    async def receive(self,msg):
        try:
            body=decode(msg.data)
            await self.process(body)
        except (ValueError,TypeError,KeyError):
            print('invalid task rejected',flush=True)

    async def start_jetstream(self,js,prefix,ack_wait=30,max_deliver=5,replicas=None):
        """Consume this device's tasks from JetStream instead of Core NATS (opt-in).

        At-least-once delivery is safe because the ledger claim() is idempotent; a
        processing exception lets the consumer nak and retry within max_deliver.
        The stream is (re)ensured first with retry so a worker that starts before
        the Router does not fail on a missing stream; an existing stream with a
        different replica count is accepted rather than retried forever.
        """
        import os
        from . import jetstream_bus
        if replicas is None:
            replicas=int(os.environ.get('POC_JETSTREAM_REPLICAS','1'))
        async def runner():
            while True:
                try:
                    await jetstream_bus.ensure(js,'TASKS',prefix,replicas=replicas)
                    break
                except Exception:
                    try:
                        await js.stream_info('TASKS')
                        break
                    except Exception:
                        await asyncio.sleep(1)
            async def handler(body):
                await self.process(body)
            await jetstream_bus.consume(js,prefix,handler,durable='worker-'+self.row['agent_id'],
                                        target=self.row['agent_id'],max_deliver=max_deliver,ack_wait=ack_wait)
        self._jetstream_task=asyncio.ensure_future(runner())

    async def _cancel(self,body):
        task_id=body.get('task_id')
        if not isinstance(task_id,str):return
        row=self.store.db.execute('SELECT state FROM work WHERE task_id=?',(task_id,)).fetchone()
        if row and row['state'] in {'running','done'}:
            code='CANNOT_CANCEL';data={'reason':'in-flight execution cannot be interrupted'}
        else:
            code='CANCELLED';data=None
        await self.nc.publish(f"{PREFIX}.agent.{self.row['agent_id']}.result",encode({'task_id':task_id,'code':code,'data':data}))

    async def ack(self,msg):
        try:
            body=decode(msg.data)
            if body.get('type')=='task_result_ack' and isinstance(body.get('task_id'),str):
                self.store.acknowledge(body['task_id'])
        except (ValueError,TypeError,AttributeError):pass

    async def start(self):
        identity=self.row['agent_id']
        await self.nc.subscribe(f'{PREFIX}.agent.{identity}.task',cb=self.receive)
        await self.nc.subscribe(f'{PREFIX}.client.{identity}.response',cb=self.ack)

    async def pump(self):
        while True:
            if self.nc.is_connected:
                for task_id,result in self.store.outbox():
                    await self.nc.publish(f"{PREFIX}.agent.{self.row['agent_id']}.result",encode({'task_id':task_id,**result}))
            await asyncio.sleep(1)
