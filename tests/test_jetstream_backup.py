"""Real-server snapshot/restore incl. KV indexes, lease epoch, consumers and quarantine."""
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

import nats
from app.kv_task_store import KvTaskStore
from app.kv_lease import KvLease
from scripts import jetstream_backup, runtime_backup
from app.task_store import TaskStore

BINARY=Path(__file__).resolve().parents[1]/'.runtime/bin/nats-server'

@unittest.skipUnless(BINARY.exists(), 'local NATS binary unavailable')
class NativeSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.processes=[];self.connections=[]

    async def asyncTearDown(self):
        for nc in self.connections:await nc.close()
        for process in self.processes:
            process.terminate();process.wait(timeout=5)
        self.tmp.cleanup()

    async def server(self,name):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        from scripts.render_site import render_nats
        from test_multihost_plan import PROFILE
        rendered=render_nats(PROFILE,self.root/'unused-certs')
        router=next(row for row in rendered['accounts']['POC']['users'] if row['user']=='router')
        router['password']='fixture-only'
        config={'listen':f'127.0.0.1:{port}', 'max_payload':65536,
                'jetstream':{'store_dir':str(self.root/name)},
                'accounts':{'POC':{'jetstream':'enabled','users':[router]}}}
        conf=self.root/(name+'.json');conf.write_text(json.dumps(config))
        process=subprocess.Popen([str(BINARY),'-c',str(conf)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.processes.append(process)
        for _ in range(80):
            try:
                async def quiet(_error):pass
                nc=await nats.connect(f'nats://127.0.0.1:{port}',user='router',password='fixture-only',connect_timeout=.2,max_reconnect_attempts=0,error_cb=quiet)
                self.connections.append(nc);return nc
            except OSError:await asyncio.sleep(.05)
        self.fail('fixture server failed to start')

    async def test_full_native_roundtrip_and_safe_restore(self):
        nc=await self.server('source');js=nc.jetstream()
        for stream in ('TASKS','TASKSDLQ'):
            subject='corp.dev.v1.' + ('task.>' if stream=='TASKS' else 'dlq.>')
            await js.add_stream(name=stream,subjects=[subject])
            if stream=='TASKS':
                await js.publish('corp.dev.v1.task.target',b'payload')
        from nats.js.api import ConsumerConfig
        await js.add_consumer('TASKS',config=ConsumerConfig(durable_name='worker',ack_policy='explicit'))
        store=KvTaskStore(js)
        pending=await store.submit('caller','target','pending','ask_peer',{'projects':['p']},600)
        done=await store.submit('caller','target','done','ask_peer',{'projects':['p']},600)
        await store.finish(done['task_id'],{'code':'OK','data':'saved'})
        lease=await KvLease(js).acquire('dispatch','old-holder')
        snapshot=await jetstream_backup.snapshot(nc,self.root/'js-snapshot')
        self.assertEqual(len(snapshot['streams']),4)
        local=self.root/'local';local.mkdir();TaskStore(local/'tasks.sqlite3')
        runtime_backup.backup(local,self.root/'full',jetstream_snapshot=self.root/'js-snapshot')
        runtime_backup.restore(self.root/'full',self.root/'restored')
        target=await self.server('target')
        with self.assertRaises(ValueError):await jetstream_backup.restore(target,self.root/'restored/jetstream')
        report=await jetstream_backup.restore(target,self.root/'restored/jetstream',isolated=True)
        self.assertEqual(report['pending_tasks_held'],1)
        restored=KvTaskStore(target.jetstream())
        self.assertEqual(json.loads((await restored.get(pending['task_id']))['result'])['code'],'RESTORE_REVIEW_REQUIRED')
        self.assertEqual(json.loads((await restored.get(done['task_id']))['result'])['data'],'saved')
        self.assertEqual((await restored.submit('caller','target','done','ask_peer',{'projects':['p']},600))['task_id'],done['task_id'])
        new=await KvLease(target.jetstream()).acquire('dispatch','new-holder')
        self.assertGreater(new['epoch'],lease['epoch'])
        consumer=await target.jetstream().consumer_info('TASKS','worker')
        self.assertEqual(consumer.name,'worker')
        with self.assertRaises(ValueError):await jetstream_backup.restore(target,self.root/'js-snapshot',isolated=True)

if __name__ == '__main__':unittest.main()
