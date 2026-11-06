import tempfile
from pathlib import Path
import time
import unittest

from app.task_store import TaskStore, MAX_ATTEMPTS


class DeadLetterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name) / 'tasks.sqlite3')
        self.task = self.store.submit('ag-a', 'mac-x', 'key-1', 'ask_peer', {'question': 'x'}, 300)

    def tearDown(self):
        self.tmp.cleanup()

    def _kill(self, task_id=None):
        task_id = task_id or self.task['task_id']
        for _ in range(MAX_ATTEMPTS + 1):
            self.store.sent(task_id)

    def test_dead_letter_requeue_resets_delivery(self):
        self._kill()
        rows = self.store.dead_letters()
        self.assertEqual([r['task_id'] for r in rows], [self.task['task_id']])
        replayed = self.store.requeue(self.task['task_id'])
        self.assertEqual(replayed['state'], 'accepted')
        self.assertEqual(replayed['attempts'], 0)
        self.assertIsNone(replayed['result'])
        self.assertEqual(self.store.dead_letters(), [])
        self.assertIn('requeued', [e['event'] for e in self.store.history(self.task['task_id'])])

    def test_requeue_rejects_non_dead_letter(self):
        with self.assertRaises(ValueError):
            self.store.requeue(self.task['task_id'])

    def test_requeue_rejects_expired_deadline(self):
        self._kill()
        self.store.db.execute('UPDATE tasks SET deadline=? WHERE task_id=?', (time.time() - 1, self.task['task_id']))
        self.store.db.commit()
        with self.assertRaises(ValueError):
            self.store.requeue(self.task['task_id'])

    def test_purge_archives_and_enforce_quota(self):
        self._kill()
        second = self.store.submit('ag-a', 'mac-x', 'key-2', 'ask_peer', {'question': 'y'}, 300)
        self._kill(second['task_id'])
        self.assertEqual(len(self.store.dead_letters()), 2)
        status = self.store.quota_status()
        self.assertGreater(status['size_bytes'], 0)
        self.assertEqual(status['dead_letters'], 2)
        report = self.store.enforce_quota(limit_bytes=1)
        self.assertEqual(report['purged'], 2)
        self.assertEqual(self.store.dead_letters(), [])
        archived = self.store.db.execute('SELECT count(*) FROM task_archive').fetchone()[0]
        self.assertEqual(archived, 2)

    def test_purge_respects_cutoff(self):
        self._kill()
        self.assertEqual(self.store.purge_dead_letters(time.time() - 3600), 0)
        self.assertEqual(self.store.purge_dead_letters(time.time() + 1), 1)


if __name__ == '__main__':
    unittest.main()
