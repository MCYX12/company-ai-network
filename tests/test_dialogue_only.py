import json,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from types import SimpleNamespace
from app.common import request,encode
from app.router import Router
from app.task_worker import TaskWorker

class DialogueOnlyTests(unittest.IsolatedAsyncioTestCase):
 async def test_router_rejects_old_execution_even_with_registered_identity(self):
  with tempfile.TemporaryDirectory() as tmp,patch('app.router.RUNTIME',Path(tmp)),patch('app.router.load_registry',return_value={'ag-a':{'enabled':True}}):
   router=Router(AsyncMock());router.respond=AsyncMock()
   for op,payload in [('generate_text',{'prompt':'hello'}),('get_status',{})]:
    b=request('ag-a','ag-b');b['action']='submit_task';b['payload']={'operation':op,'idempotency_key':'test','ttl_seconds':60,**payload}
    await router.ingress(SimpleNamespace(subject='corp.dev.v1.ingress.ag-a.request',data=encode(b)))
    self.assertEqual(router.respond.call_args.args[2],'UNSUPPORTED_ACTION')
 async def test_worker_rejects_old_model_execution(self):
  import time,uuid
  with tempfile.TemporaryDirectory() as tmp,patch('app.task_worker.RUNTIME',Path(tmp)):
   worker=TaskWorker(AsyncMock(),{'agent_id':'b','tool':'codex'})
   body=dict(task_id=str(uuid.uuid4()),target='b',operation='generate_text',payload={'prompt':'do work'},deadline=time.time()+60)
   with patch('app.task_worker.execute_text') as execute:
    await worker.receive(SimpleNamespace(data=encode(body)));execute.assert_not_called()
   self.assertEqual(worker.store.outbox()[0][1]['code'],'UNSUPPORTED_ACTION')
   worker.store.db.close()
