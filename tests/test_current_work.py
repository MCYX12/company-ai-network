import asyncio,tempfile,time,unittest,json,subprocess,os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from app.worker_kernel import WorkerLedger,ReliableTaskRunner
from app.agent import current_status
from tests.helpers.client import load_client_worker
class Tests(unittest.IsolatedAsyncioTestCase):
 async def test_actual_running_then_pending_result_then_idle_without_body(self):
  with tempfile.TemporaryDirectory() as tmp:
   ledger=WorkerLedger(Path(tmp)/'worker.sqlite3');entered=asyncio.Event();release=asyncio.Event()
   async def execute(*_):entered.set();await release.wait();return {'code':'OK','data':{'text':'PRIVATE RESULT'}}
   async def publish(*_):pass
   runner=ReliableTaskRunner('peer',ledger,execute,publish,publish)
   body={'task_id':'12345678-1234-4234-9234-123456789abc','target':'peer','operation':'ask_peer','payload':{'question':'PRIVATE PROMPT'},'deadline':time.time()+60}
   self.assertEqual(runner.activity()['state'],'idle')
   task=asyncio.create_task(runner.handle(body));await asyncio.wait_for(entered.wait(),1)
   r=runner.activity();self.assertEqual(r['state'],'running');self.assertEqual(r['executing_count'],1);self.assertIsNone(r['progress']);self.assertNotIn('PRIVATE',json.dumps(r));self.assertNotIn(body['task_id'],json.dumps(r))
   release.set();await task;r=runner.activity();self.assertEqual(r['state'],'result_pending');self.assertEqual(r['results_pending'],1)
   ledger.acknowledge(body['task_id']);self.assertEqual(runner.activity()['state'],'idle');ledger.db.close()
 def test_harness_status_uses_fixed_preflight_not_version_adapter_or_model(self):
  row={'agent_id':'peer','tool':'dsh-harness'};worker=SimpleNamespace(runner=SimpleNamespace(activity=lambda:{'state':'idle'}))
  with patch('app.agent.is_harness',return_value=True),patch('app.agent.harness_preflight',return_value=('ready',None)) as check,patch('app.agent.execute') as fallback:
   code,data=current_status(row,worker);self.assertEqual(code,'OK');check.assert_called_once_with('dsh-harness');fallback.assert_not_called();self.assertEqual(data['worker_activity']['state'],'idle');self.assertEqual(data['native_work']['status'],'not_integrated')
 def test_client_status_carries_snapshot_without_model_call(self):
  w=load_client_worker();body={'target_agent_id':'peer','action':'get_status','payload':{},'expires_at':time.time()+60}
  with tempfile.TemporaryDirectory() as tmp,patch.object(w,'run_command') as model,patch.object(w,'preflight',return_value=('ready',None)):
   r=w.query_result({'capabilities':['get_status']},'peer',body,Path(tmp),activity={'state':'running','executing_count':1})
   self.assertEqual(r[1]['worker_activity']['executing_count'],1);model.assert_not_called()
 def test_remote_freeze_skips_mirror_but_retains_local_refresh(self):
  root=Path(__file__).resolve().parents[1]
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);(p/'scripts').mkdir();(p/'scripts/run_record_refresh.sh').write_bytes((root/'scripts/run_record_refresh.sh').read_bytes());stub=p/'python';stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "'+str(p/'calls')+'"\nprintf "{}\\n"\n');stub.chmod(0o700)
   env={**os.environ,'PYTHON':str(stub),'POC_RECORD_MIRRORS':'0','POC_RUNTIME':str(p/'runtime')}
   r=subprocess.run(['bash',str(p/'scripts/run_record_refresh.sh')],env=env,capture_output=True,text=True)
   self.assertEqual(r.returncode,0);calls=(p/'calls').read_text();self.assertIn('refresh_project_record',calls);self.assertNotIn('record_mirror',calls);self.assertIn('disabled by operator',r.stdout)
