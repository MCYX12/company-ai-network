import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.task_store import MAX_ATTEMPTS, TaskStore


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name) / 'tasks.sqlite3')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def submit(self, target='T', key='k', ttl=600):
        return self.store.submit('owner', target, key, 'ask_peer', {'question': 'q'}, ttl)['task_id']

    def test_repeated_delivery_becomes_dead_letter(self):
        task_id = self.submit()
        now = time.time()
        for _ in range(MAX_ATTEMPTS):
            self.assertTrue(self.store.sent(task_id, now=now))
            self.assertEqual(self.store.get(task_id)['state'], 'dispatched')
        self.assertFalse(self.store.sent(task_id, now=now))
        row = self.store.get(task_id)
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(json.loads(row['result'])['code'], 'DELIVERY_FAILED')
        self.assertIn('dead_letter', [event['event'] for event in self.store.history(task_id)])

    def test_per_target_concurrency_holds_extra_without_dead_letter(self):
        ids = [self.submit(target='T2', key=f'k{i}') for i in range(3)]
        chosen = self.store.plan(time.time(), lambda row: True, lambda target, operation: True)
        self.assertEqual(len(chosen), 2)
        self.assertEqual(self.store.inflight_counts()['T2'], 2)
        self.assertEqual(sorted(self.store.get(i)['state'] for i in ids),
                         ['dispatched', 'dispatched', 'queued'])
        self.assertEqual(self.store.plan(time.time() + 2, lambda row: True, lambda target, operation: True), [])
        self.assertEqual(self.store.get(ids[2])['state'], 'queued')

    def test_offline_target_defers_without_starving_others(self):
        offline = self.submit(target='OFFLINE', key='off')
        online = self.submit(target='ONLINE', key='on')
        chosen = self.store.plan(time.time(), lambda row: True, lambda target, operation: target == 'ONLINE')
        self.assertEqual([row['task_id'] for row in chosen], [online])
        self.assertEqual(self.store.get(offline)['state'], 'queued')
        self.assertEqual(self.store.get(offline)['deferrals'], 1)
        self.assertEqual(self.store.get(online)['state'], 'dispatched')

    def test_running_task_is_not_redelivered(self):
        task_id = self.submit()
        self.assertTrue(self.store.sent(task_id))
        self.assertTrue(self.store.mark_running(task_id))
        self.assertEqual(self.store.get(task_id)['state'], 'running')
        self.assertEqual(self.store.plan(time.time(), lambda row: True, lambda target, operation: True), [])
        self.assertEqual(self.store.due(time.time()), [])

    def test_queued_task_can_still_be_cancelled(self):
        task_id = self.submit()
        self.assertTrue(self.store.defer(task_id, time.time()))
        self.assertEqual(self.store.get(task_id)['state'], 'queued')
        code, row = self.store.cancel(task_id, 'owner')
        self.assertEqual(code, 'CANCELLED')
        self.assertEqual(row['attempts'], 0)

    def test_dispatched_task_enters_cooperative_cancel(self):
        task_id = self.submit()
        self.assertTrue(self.store.sent(task_id))
        code, row = self.store.cancel(task_id, 'owner')
        self.assertEqual(code, 'CANCEL_REQUESTED')
        self.assertEqual(row['state'], 'cancel_requested')

    def test_public_reports_deferrals(self):
        task_id = self.submit()
        self.store.defer(task_id, time.time())
        self.assertIn('deferrals', self.store.public(self.store.get(task_id)))

    def test_schema_migration_adds_deferrals_to_existing_db(self):
        import sqlite3
        path = Path(self.tmp.name) / 'legacy.sqlite3'
        legacy = sqlite3.connect(path)
        legacy.execute('CREATE TABLE tasks(task_id TEXT PRIMARY KEY,owner TEXT NOT NULL,target TEXT NOT NULL,idempotency_key TEXT NOT NULL,fingerprint TEXT NOT NULL,operation TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,created_at REAL NOT NULL,deadline REAL NOT NULL,next_send REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,result TEXT,UNIQUE(owner,idempotency_key))')
        legacy.execute('CREATE TABLE task_audit(id INTEGER PRIMARY KEY,task_id TEXT NOT NULL,event TEXT NOT NULL,timestamp REAL NOT NULL)')
        legacy.commit()
        legacy.close()
        migrated = TaskStore(path)
        columns = {row['name'] for row in migrated.db.execute('PRAGMA table_info(tasks)')}
        self.assertIn('deferrals', columns)
        migrated.db.close()


class RouterStartAckTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_started_marks_running(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('app.router.RUNTIME', Path(directory)):
                from app.common import encode
                from app.router import Router

                class StubNC:
                    async def publish(self, *args, **kwargs):
                        pass

                router = Router(StubNC())
                try:
                    task = router.tasks.submit('owner', 'mac-x', 'k', 'ask_peer', {'question': 'q'}, 600)
                    message = SimpleNamespace(subject='corp.dev.v1.agent.mac-x.result',
                                              data=encode({'type': 'task_started', 'task_id': task['task_id']}))
                    await router.task_result(message)
                    self.assertEqual(router.tasks.get(task['task_id'])['state'], 'running')
                finally:
                    router.tasks.db.close()


class RouterDirectoryProposalTests(unittest.IsolatedAsyncioTestCase):
    async def test_directory_proposals_processed_while_running(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('app.router.RUNTIME', Path(directory)), patch('app.router.allowed', return_value=True):
                from app.common import encode
                from app.router import Router

                class StubNC:
                    async def publish(self, *args, **kwargs):
                        pass

                calls = []

                class FakeDirectoryRequests:
                    def request(self, owner, target, root):
                        calls.append((owner, target, root))
                        return {'id': 'g1', 'root': root, 'profile': 'project_notes_v1', 'state': 'pending', 'expires': 1.0}

                    def permits_ids(self, *args):
                        return True

                with patch('app.directory_requests.DirectoryRequests', FakeDirectoryRequests):
                    router = Router(StubNC())
                    try:
                        task = router.tasks.submit('owner', 'mac-x', 'k', 'ask_peer',
                                                   {'question': 'q', 'caller': 'owner', 'projects': ['demo-project']}, 600)
                        self.assertTrue(router.tasks.sent(task['task_id']))
                        self.assertTrue(router.tasks.mark_running(task['task_id']))
                        message = SimpleNamespace(subject='corp.dev.v1.agent.mac-x.result', data=encode(
                            {'task_id': task['task_id'], 'code': 'OK', 'data': {'directory_requests': [{'root': '/x'}]}}))
                        await router.task_result(message)
                        row = router.tasks.get(task['task_id'])
                        self.assertEqual(row['state'], 'succeeded')
                        self.assertEqual([item['root'] for item in row_result(row)], ['/x'])
                        self.assertEqual(calls, [('owner', 'mac-x', '/x')])
                    finally:
                        router.tasks.db.close()


def row_result(row):
    return json.loads(row['result'])['data']['directory_requests']


if __name__ == '__main__':
    unittest.main()
