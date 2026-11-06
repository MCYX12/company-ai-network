import argparse
import asyncio
import os
import signal
import time
from pathlib import Path
from .common import PREFIX, RUNTIME, connect, encode, decode
from .registry import load_registry, capabilities, TALK_ACTIONS
from .adapters import describe, execute
from .harness_adapter import is_harness, preflight as harness_preflight
from .inventory import COMMANDS, inspect_tool
from .task_worker import TaskWorker
from .text_task import enabled
from .project_records import read_progress
from .build_info import summary as build_summary

def maintenance_requested(runtime=None,identity=None):
    """True once the operator created RUNTIME/maintenance (a planned drain, not a kill).

    This is the pre-SIGTERM half of maintenance: the worker advertises draining and claims
    no new task, but keeps running so an in-flight answer can finish; the operator watches
    the ledger and only then stops the service.
    """
    if identity is None:return Path(runtime or RUNTIME).joinpath('maintenance').exists()
    from .maintenance import requested
    return requested(identity,runtime or RUNTIME)


def apply_maintenance(worker):
    """Idempotently move a worker into draining; True only when it just changed."""
    if worker.draining:
        return False
    worker.stop_consuming()
    return True

def current_status(row,worker):
    if is_harness(row):
        status,hint=harness_preflight(row['tool'])
        return 'OK',{'synthetic':False,'agent_id':row['agent_id'],'tool':row['tool'],
                     'source':'platform_worker_status','adapter_status':status,'hint':hint,
                     'worker_activity':worker.runner.activity(),
                     'native_work':{'status':'not_integrated','progress':None,
                                    'summary':'Desktop session activity is not observed'}}
    code,data=execute(row,'get_status',{})
    if isinstance(data,dict):data['worker_activity']=worker.runner.activity()
    return code,data


async def main(identity):
    row = load_registry().get(identity)
    if not row or not row['enabled']:
        raise ValueError('Identity is not enrolled or is disabled')
    nc = await connect(identity)
    worker=TaskWorker(nc,row)
    async def receive(msg):
        try:
            envelope = decode(msg.data)
            body = envelope['request']
            if body['target_agent_id'] != identity or (body['action'] not in capabilities(row) or (body['action'] == 'get_project_progress' and body['payload'].get('project_id') not in row['projects'])):
                code, data = 'FORBIDDEN', None
            elif body['expires_at'] <= time.time():
                code, data = 'EXPIRED', None
            elif body['action']=='get_status':
                code,data=current_status(row,worker)
            else:
                code, data = await asyncio.to_thread(execute, row, body['action'], body['payload'])
            await nc.publish(f'{PREFIX}.agent.{identity}.reply', encode(
                dict(dispatch_id=envelope['dispatch_id'], code=code, data=data)))
        except (ValueError, KeyError, TypeError):
            print('invalid query rejected', flush=True)
    await nc.subscribe(f'{PREFIX}.agent.{identity}.query', cb=receive)
    await worker.start()
    if os.environ.get('POC_JETSTREAM') == '1' and set(capabilities(row)) & TALK_ACTIONS:
        # Durable delivery (opt-in), for worker identities only. The rendered broker
        # ACLs grant $JS.API./_INBOX. to identities that advertise a talk action, so a
        # client-only fixture must not request them. The Router also publishes the Core
        # task subject, which is how a client-only worker still receives its tasks.
        await worker.start_jetstream(nc.jetstream(), PREFIX)
    result_pump=asyncio.create_task(worker.pump())
    await nc.flush()
    drain=asyncio.Event()
    def begin_drain(*_):drain.set()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):
        try:loop.add_signal_handler(sig,begin_drain)
        except (NotImplementedError,RuntimeError):signal.signal(sig,begin_drain)
    print(f'{identity} ready', flush=True)
    last_metadata={}
    try:
        while not drain.is_set():
            metadata = describe(row)
            declared = set(capabilities(row))
            harness_ready = False
            if row['tool'] in COMMANDS:
                probe = await asyncio.to_thread(inspect_tool, row['tool'])
                metadata.update(adapter_status=probe['adapter_status'], capabilities=probe['capabilities'])
            elif is_harness(row):
                # A configured one-shot local harness answers dialogue directly, but
                # only advertise ask_peer once its command actually resolves; a
                # misconfigured harness shows as not_found/probe_failed instead of
                # failing a user's question later.
                harness_status, _hint = await asyncio.to_thread(harness_preflight, row['tool'])
                metadata['adapter_status'] = harness_status
                harness_ready = harness_status == 'ready'
                if not harness_ready:
                    metadata['capabilities'] = [c for c in metadata['capabilities'] if c != 'ask_peer']
            elif row['tool'] != 'fixture':
                # The built-in worker has no adapter for a self-developed agent; that
                # agent's own worker advertises its capabilities instead.
                metadata['capabilities'] = [a for a in metadata['capabilities'] if a == 'get_project_progress']
            if 'get_project_progress' in declared:
                records=await asyncio.to_thread(lambda: any(read_progress(row,{'project_id':p})[0]=='OK' for p in row['projects']))
                if records and 'get_project_progress' not in metadata['capabilities']:
                    metadata['capabilities']=[*metadata['capabilities'],'get_project_progress']
            talk = ({'ask_peer'} if harness_ready else set()) | ({'generate_text','ask_peer'} if enabled() else set())
            if 'get_status' in metadata['capabilities']:
                metadata['capabilities'] = list(dict.fromkeys([*metadata['capabilities'], *sorted(talk & declared)]))
            metadata['task_actions'] = [a for a in metadata['capabilities'] if a in {'get_status','generate_text','ask_peer'}] if 'get_status' in metadata['capabilities'] else []
            requested=maintenance_requested(identity=identity)
            if requested and apply_maintenance(worker):
                print(f'{identity} maintenance requested: draining (no new tasks)', flush=True)
            elif not requested and worker.draining:
                # Cancelling maintenance must restore dispatch, or the flag left behind
                # would keep this worker draining forever.
                worker.resume()
                print(f'{identity} maintenance cleared: accepting tasks', flush=True)
            # 'ready' means task subscription and outbox pump are up; 'draining' means a
            # planned maintenance stop has begun and the Router must not dispatch here.
            readiness = 'draining' if worker.draining else 'ready'
            last_metadata=metadata
            await nc.publish(f'{PREFIX}.agent.{identity}.heartbeat', encode({'agent_id': identity, 'device_id': row['device_id'], 'status': 'online', 'build': build_summary(), 'readiness': readiness, **metadata}))
            # Short ticks so a maintenance stop is noticed promptly instead of after 15s.
            for _ in range(150):
                if drain.is_set():break
                await asyncio.sleep(.1)
        # Maintenance stop: claim no new work, announce draining so the Router stops
        # dispatching here, then let the in-flight answer finish before the process exits.
        worker.stop_consuming()
        try:
            await nc.publish(f'{PREFIX}.agent.{identity}.heartbeat', encode({'agent_id': identity, 'device_id': row['device_id'], 'status': 'online', 'build': build_summary(), 'readiness': 'draining', **last_metadata}))
        except Exception:
            pass
        grace=float(os.environ.get('POC_DRAIN_SECONDS', '15') or 0)
        if grace>0:
            await worker.runner.wait_idle(grace)
    finally:
        result_pump.cancel()
        pending=[result_pump]
        if worker._jetstream_task is not None:
            worker._jetstream_task.cancel()
            pending.append(worker._jetstream_task)
        # Bound shutdown: a dead broker must not keep the process alive past the service
        # manager's stop timeout (which would force a SIGKILL and look like a crash).
        try:
            await asyncio.wait_for(asyncio.gather(*pending,return_exceptions=True),timeout=3)
        except Exception:
            pass
        if worker.runner.inflight==0:
            worker.store.db.close()
        try:
            await asyncio.wait_for(nc.close(),timeout=3)
        except Exception:
            pass

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('identity')
    asyncio.run(main(parser.parse_args().identity))
