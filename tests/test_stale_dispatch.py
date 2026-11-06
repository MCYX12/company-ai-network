import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app.common import encode
from app.task_store import TaskStore


class FakeNc:
    def __init__(self):
        self.published = []

    async def publish(self, subject, data):
        self.published.append((subject, data))


class FakeMsg:
    def __init__(self, body):
        self.subject = 'poc.agent.peer-x.result'
        self.data = encode(body)


class StoreRequeueTests(unittest.TestCase):
    def test_requeue_stale_moves_dispatched_back_to_queued(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder) / 'tasks.sqlite3')
            row = store.submit('ag-a', 'peer-x', 'key', 'ask_peer',
                               {'question': 'x', 'caller': 'ag-a', 'projects': ['p']}, 300)
            self.assertTrue(store.sent(row['task_id']))
            self.assertEqual(store.get(row['task_id'])['state'], 'dispatched')
            self.assertTrue(store.requeue_stale(row['task_id']))
            self.assertEqual(store.get(row['task_id'])['state'], 'queued')
            self.assertIn('stale_dispatch', [e['event'] for e in store.history(row['task_id'])])

    def test_requeue_stale_ignores_terminal_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder) / 'tasks.sqlite3')
            row = store.submit('ag-a', 'peer-x', 'key', 'ask_peer',
                               {'question': 'x', 'caller': 'ag-a', 'projects': ['p']}, 300)
            store.finish(row['task_id'], {'code': 'OK', 'data': None})
            self.assertFalse(store.requeue_stale(row['task_id']))


class RouterStaleDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        store = TaskStore(self.runtime / 'tasks.sqlite3')
        self.task_id = store.submit('ag-a', 'peer-x', 'key', 'ask_peer',
                                    {'question': 'x', 'caller': 'ag-a', 'projects': ['demo-project']}, 300)['task_id']
        store.sent(self.task_id)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stale_dispatch_is_recorded_and_requeued(self):
        with patch('app.router.RUNTIME', self.runtime), patch.dict(os.environ, {'POC_DISPATCH_LEASE': '0'}):
            from app.router import Router
            router = Router(FakeNc())
            body = {'task_id': self.task_id, 'code': 'STALE_DISPATCH', 'data': {'reason': 'stale dispatch lease epoch'}}
            asyncio.run(router.task_result(FakeMsg(body)))
            self.assertEqual(router.tasks.get(self.task_id)['state'], 'queued')
            events = [e['event'] for e in router.tasks.history(self.task_id)]
            self.assertIn('stale_dispatch', events)
            audit = (self.runtime / 'audit.jsonl').read_text()
            self.assertIn('STALE_DISPATCH', audit)


if __name__ == '__main__':
    unittest.main()
