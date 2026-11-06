from pathlib import Path
import tempfile
import unittest

from app.task_store import TaskStore

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


class CancelSemanticsTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.store = TaskStore(Path(self.tmp.name) / 'tasks.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def _dispatched(self):
        task = self.store.submit('ag-a', 'peer-x', 'k', 'ask_peer', {'question': 'x'}, 600)
        self.store.sent(task['task_id'])
        return task['task_id']

    def test_immediate_cancel(self):
        task = self.store.submit('ag-a', 'peer-x', 'k', 'ask_peer', {'question': 'x'}, 600)
        code, row = self.store.cancel(task['task_id'], 'ag-a')
        self.assertEqual(code, 'CANCELLED')
        self.assertEqual(row['state'], 'cancelled')
        self.assertEqual(row['cancel_state'], 'cancelled')

    def test_cooperative_cancel_then_confirm(self):
        task_id = self._dispatched()
        code, row = self.store.cancel(task_id, 'ag-a')
        self.assertEqual(code, 'CANCEL_REQUESTED')
        self.assertEqual(row['state'], 'cancel_requested')
        self.assertEqual(row['cancel_state'], 'requested')
        self.assertTrue(self.store.confirm_cancelled(task_id))
        row = self.store.get(task_id)
        self.assertEqual(row['state'], 'cancelled')
        self.assertEqual(row['cancel_state'], 'cancelled')

    def test_cannot_cancel_does_not_fail_and_keeps_tracking_result(self):
        task_id = self._dispatched()
        self.store.cancel(task_id, 'ag-a')
        self.store.finish(task_id, {'code': 'CANNOT_CANCEL', 'data': None})
        row = self.store.get(task_id)
        self.assertNotEqual(row['state'], 'failed')
        self.assertEqual(row['cancel_state'], 'cannot_cancel')
        # the real result still lands and is preserved
        self.store.finish(task_id, {'code': 'OK', 'data': {'text': 'answer'}})
        row = self.store.get(task_id)
        self.assertEqual(row['state'], 'succeeded')
        self.assertEqual(row['cancel_state'], 'cannot_cancel')
        import json
        self.assertEqual(json.loads(row['result'])['data']['text'], 'answer')

    def test_public_exposes_cancel_state(self):
        task = self.store.submit('ag-a', 'peer-x', 'k', 'ask_peer', {'question': 'x'}, 600)
        self.assertIn('cancel_state', TaskStore.public(self.store.get(task['task_id'])))


if __name__ == '__main__':
    unittest.main()
