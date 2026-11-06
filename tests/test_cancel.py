import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.task_store import TaskStore

class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'tasks.db';self.store=TaskStore(self.path)
        self.task=self.store.submit('owner','target','key','get_status',{},60);self.tid=self.task['task_id']
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def test_cancel_is_durable_and_idempotent(self):
        self.assertEqual(self.store.cancel(self.tid,'owner')[0],'CANCELLED')
        self.assertEqual(self.store.cancel(self.tid,'owner')[0],'CANCELLED')
        self.assertEqual([e['event'] for e in self.store.history(self.tid)],['accepted','cancelled'])
        self.store.db.close();self.store=TaskStore(self.path)
        self.assertEqual(self.store.submit('owner','target','key','get_status',{},60)['state'],'cancelled')
        self.assertEqual(self.store.due(),[])
    def test_stale_dispatch_and_late_result_cannot_resurrect(self):
        stale=self.store.due();self.assertEqual(len(stale),1)
        self.store.cancel(self.tid,'owner')
        self.assertFalse(self.store.sent(stale[0]['task_id']))
        self.store.finish(self.tid,{'code':'OK'})
        self.assertEqual(self.store.get(self.tid)['state'],'cancelled')
    def test_dispatched_enters_cooperative_cancel(self):
        self.assertTrue(self.store.sent(self.tid))
        code,row=self.store.cancel(self.tid,'owner')
        self.assertEqual(code,'CANCEL_REQUESTED');self.assertEqual(row['state'],'cancel_requested')
        self.assertEqual(self.store.cancel(self.tid,'owner')[0],'CANCEL_REQUESTED')
        self.store.finish(self.tid,{'code':'CANCELLED','data':None})
        self.assertEqual(self.store.get(self.tid)['state'],'cancelled')
    def test_running_task_cannot_be_stopped_keeps_tracking(self):
        self.assertTrue(self.store.sent(self.tid));self.store.mark_running(self.tid)
        self.store.cancel(self.tid,'owner')
        self.assertEqual(self.store.get(self.tid)['state'],'cancel_requested')
        self.store.finish(self.tid,{'code':'CANNOT_CANCEL','data':{'reason':'x'}})
        row=self.store.get(self.tid)
        # A failed cancellation is not a failed task: keep tracking the real result.
        self.assertEqual(row['state'],'cancel_requested')
        self.assertEqual(row['cancel_state'],'cannot_cancel')
        self.store.finish(self.tid,{'code':'OK','data':{'n':1}})
        row=self.store.get(self.tid)
        self.assertEqual(row['state'],'succeeded')
        self.assertEqual(row['cancel_state'],'cannot_cancel')
    def test_cancel_requested_does_not_block_completion(self):
        self.assertTrue(self.store.sent(self.tid));self.store.mark_running(self.tid)
        self.store.cancel(self.tid,'owner');self.store.finish(self.tid,{'code':'OK','data':{'n':1}})
        self.assertEqual(self.store.get(self.tid)['state'],'succeeded')
    def test_owner_and_missing_do_not_leak(self):
        self.assertEqual(self.store.cancel(self.tid,'other'),('FORBIDDEN',None))
        self.assertEqual(self.store.cancel('missing','owner'),('FORBIDDEN',None))
    def test_expired_before_cancel(self):
        with patch('app.task_store.time.time',return_value=self.task['deadline']+1):
            self.assertEqual(self.store.cancel(self.tid,'owner')[0],'EXPIRED')
    def test_finished_not_rewritten(self):
        self.store.finish(self.tid,{'code':'OK'})
        self.assertEqual(self.store.cancel(self.tid,'owner')[0],'ALREADY_FINISHED')
        self.assertEqual(self.store.get(self.tid)['state'],'succeeded')
if __name__=='__main__':unittest.main()
