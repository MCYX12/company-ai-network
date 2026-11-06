"""DLQ administration must use the Router's authoritative ledger, not a local file."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.common import decode


class FakeNc:
    def __init__(self):
        self.published = []

    async def publish(self, subject, data):
        self.published.append((subject, decode(data)))


class AsyncLedger:
    """Stands in for KvTaskStore: the async variant of the shared ledger."""

    def __init__(self):
        self.calls = []

    async def dead_letters(self, limit=100):
        self.calls.append(('dead_letters', limit))
        return [{'task_id': 't1', 'owner': 'ag-a', 'target': 'ag-b', 'operation': 'ask_peer',
                 'attempts': 6, 'created_at': 1.0, 'deadline': 2.0}]

    async def requeue(self, task_id, actor='admin'):
        self.calls.append(('requeue', task_id, actor))
        return {'task_id': task_id, 'state': 'accepted'}


class AdminDlqRouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _router(self):
        with patch('app.router.RUNTIME', self.runtime):
            from app.router import Router
            router = Router(FakeNc())
        router.tasks = AsyncLedger()
        return router

    def test_list_uses_router_ledger(self):
        router = self._router()
        with patch('app.router.RUNTIME', self.runtime):
            result = asyncio.run(router._admin_dlq('admin_dlq_list', {'limit': 10}, 'admin-01', 'c1'))
        self.assertEqual(result['summary']['count'], 1)
        self.assertEqual(result['dead_letters'][0]['task_id'], 't1')
        self.assertEqual(router.tasks.calls[0], ('dead_letters', 10))
        audit = (self.runtime / 'admin-history/channel.jsonl').read_text()
        self.assertIn('admin_dlq_list', audit)

    def test_requeue_uses_router_ledger(self):
        router = self._router()
        with patch('app.router.RUNTIME', self.runtime):
            result = asyncio.run(router._admin_dlq('admin_dlq_requeue', {'task_id': 't1'}, 'admin-01', 'c2'))
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(router.tasks.calls[0], ('requeue', 't1', 'admin-channel'))

    def test_invalid_payloads_rejected(self):
        router = self._router()
        with patch('app.router.RUNTIME', self.runtime):
            with self.assertRaises(ValueError):
                asyncio.run(router._admin_dlq('admin_dlq_list', {'limit': 0}, 'admin-01', 'c3'))
            with self.assertRaises(ValueError):
                asyncio.run(router._admin_dlq('admin_dlq_requeue', {}, 'admin-01', 'c4'))


if __name__ == '__main__':
    unittest.main()
