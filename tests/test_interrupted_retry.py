"""An interrupted read-only task may be retried exactly once, on the same target."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.common import PREFIX, encode


def _result_body(task_id, code='WORKER_INTERRUPTED', target='peer-x'):
    return SimpleNamespace(subject=f'{PREFIX}.agent.{target}.result',
                           data=encode({'task_id': task_id, 'code': code, 'data': None}))


class InterruptedRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.runtime=Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _router(self):
        from app.router import Router
        with (patch('app.router.RUNTIME',self.runtime),
              patch('app.directory_requests.RUNTIME',self.runtime),
              patch('app.history_access.RUNTIME',self.runtime)):
            return Router(SimpleNamespace(publish=AsyncMock()))

    def _interrupt(self,router,task_id):
        # The Router re-checks authorization when it finalises a result; keep it permitted.
        with patch('app.router.allowed',return_value=True):
            asyncio.run(router.task_result(_result_body(task_id)))

    def test_interrupted_read_only_task_is_retried_once(self):
        with patch.dict(os.environ,{'POC_RETRY_INTERRUPTED':'1'}):
            router=self._router()
        row=router.tasks.submit('ag-a','peer-x','k1','ask_peer',{'question':'进展？'},600)
        self._interrupt(router,row['task_id'])
        retries=router.tasks.db.execute('SELECT * FROM retries').fetchall()
        self.assertEqual(len(retries),1)
        retry_id=retries[0]['retry_task_id']
        new=router.tasks.get(retry_id)
        self.assertEqual((new['owner'],new['target'],new['operation']),('ag-a','peer-x','ask_peer'))
        self.assertEqual(json.loads(new['payload'])['question'],'进展？')
        self.assertIn(new['state'],('accepted','queued','dispatched'))
        # The original keeps its failure record.
        self.assertEqual(router.tasks.get(row['task_id'])['state'],'failed')
        # A second interruption of the retry must not create a third task.
        self._interrupt(router,retry_id)
        self.assertEqual(router.tasks.db.execute('SELECT count(*) FROM retries').fetchone()[0],1)

    def test_retry_linkage_is_visible_and_disabled_by_default(self):
        router=self._router()
        row=router.tasks.submit('ag-a','peer-x','k1','ask_peer',{'question':'q'},600)
        self._interrupt(router,row['task_id'])
        self.assertEqual(router.tasks.db.execute('SELECT count(*) FROM retries').fetchone()[0],0)
        self.assertIsNone(router.tasks.retried_from(row['task_id']))

    def test_original_task_exposes_its_retry_id_and_linkage_is_atomic(self):
        with patch.dict(os.environ,{'POC_RETRY_INTERRUPTED':'1'}):
            router=self._router()
        row=router.tasks.submit('ag-a','peer-x','k1','ask_peer',{'question':'q'},600)
        new=router.tasks.submit_retry(row['task_id'],'ag-a','peer-x','ask_peer',{'question':'q'},600)
        self.assertEqual(router.tasks.retry_for(row['task_id'])['retry_task_id'],new['task_id'])
        self.assertEqual(router.tasks.retried_from(new['task_id'])['root_task_id'],row['task_id'])
        original=asyncio.run(router.task_public(router.tasks.get(row['task_id'])))
        self.assertEqual(original['retry_task_id'],new['task_id'])
        retry=asyncio.run(router.task_public(router.tasks.get(new['task_id'])))
        self.assertEqual((retry['retry_of'],retry['attempt']),(row['task_id'],2))
        again=router.tasks.submit_retry(row['task_id'],'ag-a','peer-x','ask_peer',{'question':'q'},600)
        self.assertEqual(again['task_id'],new['task_id'])

    def test_a_non_read_only_result_is_never_retried(self):
        with patch.dict(os.environ,{'POC_RETRY_INTERRUPTED':'1'}):
            router=self._router()
        row=router.tasks.submit('ag-a','peer-x','k2','generate_text',{'prompt':'x'},600)
        self._interrupt(router,row['task_id'])
        self.assertEqual(router.tasks.db.execute('SELECT count(*) FROM retries').fetchone()[0],0)


if __name__=='__main__':
    unittest.main()
