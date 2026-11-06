import asyncio
import json
import os
import tempfile
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from app.conversation_access import AccessStore
from app.task_store import TaskStore

ROOT = Path(__file__).resolve().parents[1]


class FakeNc:
    def __init__(self):
        self.published = []

    async def publish(self, subject, data):
        self.published.append(subject)


class LeaseRouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        (self.runtime / 'registry.json').write_text(json.dumps({'schema_version': 1, 'agents': [
            {'agent_id': 'ag-a', 'device_id': 'ubuntu-demo', 'employee_id': 'demo-a', 'tool': 'fixture', 'projects': ['demo-project'], 'enabled': True},
            {'agent_id': 'ag-b', 'device_id': 'mac-demo', 'employee_id': 'demo-b', 'tool': 'codex', 'projects': ['demo-project'], 'enabled': True}]}))
        TaskStore(self.runtime / 'tasks.sqlite3').submit('ag-a', 'ag-b', 'lease-key', 'ask_peer',
                                                         {'question': 'x', 'caller': 'ag-a', 'projects': ['demo-project']}, 300)
        access = AccessStore(self.runtime / 'conversation-access.sqlite3')
        row = access.request('ag-a', 'ag-b', ['demo-project'])
        access.decide(row['approval_id'], 'approved', 3600, 'lease test')

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_the_lease_holder_dispatches(self):
        nc1 = FakeNc(); nc2 = FakeNc()
        with (
            patch.dict(os.environ, {'POC_DISPATCH_LEASE': '1', 'AGENT_REGISTRY': str(self.runtime / 'registry.json')}),
            patch('app.router.RUNTIME', self.runtime),
            patch('app.conversation_access.RUNTIME', self.runtime),
            patch('app.directory_requests.RUNTIME', self.runtime),
            patch('app.history_access.RUNTIME', self.runtime),
        ):
            from app.router import Router
            first = Router(nc1); second = Router(nc2)
            for router in (first, second):
                router.presence = {'ag-b': time.time()}
                router.runtime_tasks = {'ag-b': ['ask_peer']}
            asyncio.run(first.dispatch_tasks())
            asyncio.run(second.dispatch_tasks())
            self.assertEqual(len(nc1.published), 1, 'lease holder must dispatch')
            self.assertEqual(len(nc2.published), 0, 'non-holder must not dispatch')
            # After the holder releases/stops heartbeating, the other takes over once expired.
            self.assertIsNotNone(first.lease.holder('dispatch'))
            self.assertEqual(first.lease.holder('dispatch')['holder'], first.holder)


if __name__ == '__main__':
    unittest.main()
