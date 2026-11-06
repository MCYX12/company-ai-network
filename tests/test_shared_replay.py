import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from app.common import decode, encode
from app.conversation_access import AccessStore
from app.replay_store import ReplayStore

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


class FakeNc:
    def __init__(self):
        self.published = []

    async def publish(self, subject, data):
        self.published.append((subject, decode(data)))


class FakeMsg:
    def __init__(self, source, body):
        self.subject = f'corp.dev.v1.ingress.{source}.request'
        self.data = encode(body)


class SharedReplayTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.runtime = Path(self.tmp.name)
        self.store = ReplayStore(self.runtime / 'replay.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_replay_detected_across_store_instances(self):
        other = ReplayStore(self.runtime / 'replay.sqlite3')
        self.assertTrue(self.store.check_and_record('ag-a', 'm1', time.time() + 30))
        self.assertFalse(other.check_and_record('ag-a', 'm1', time.time() + 30))

    def test_distinct_ids_and_expiry(self):
        self.assertTrue(self.store.check_and_record('ag-a', 'm1', time.time() + 30))
        self.assertTrue(self.store.check_and_record('ag-a', 'm2', time.time() + 30))
        self.assertTrue(self.store.check_and_record('ag-a', 'old', time.time() - 1))
        self.assertTrue(self.store.check_and_record('ag-a', 'old', time.time() + 30))

    def test_rate_limit(self):
        self.assertTrue(self.store.check_and_record('ag-a', 'm1', time.time() + 30, limit=1))
        self.assertEqual(self.store.check_and_record('ag-a', 'm2', time.time() + 30, limit=1), 'rate_limited')


class SharedReplayRouterTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.runtime = Path(self.tmp.name)
        enrollment=patch.dict(os.environ, {'AGENT_REGISTRY':str(self.runtime/'registry.json')})
        enrollment.start();self.addCleanup(enrollment.stop)
        (self.runtime / 'registry.json').write_text(json.dumps({'schema_version': 1, 'agents': [
            {'agent_id': 'ag-a', 'device_id': 'ubuntu-demo', 'employee_id': 'demo-a', 'tool': 'codex', 'projects': ['demo-project'], 'enabled': True},
            {'agent_id': 'ag-b', 'device_id': 'mac-demo', 'employee_id': 'demo-b', 'tool': 'codex', 'projects': ['demo-project'], 'enabled': True}]}))

    def tearDown(self):
        self.tmp.cleanup()

    def _router(self):
        with (
            patch.dict(os.environ, {'AGENT_REGISTRY': str(self.runtime / 'registry.json'), 'POC_SHARED_REPLAY': '1'}),
            patch('app.router.RUNTIME', self.runtime),
            patch('app.conversation_access.RUNTIME', self.runtime),
            patch('app.common.RUNTIME', self.runtime),
            patch('app.admin_channel.RUNTIME', self.runtime),
        ):
            from app.router import Router
            nc = FakeNc()
            return Router(nc), nc

    def test_two_router_instances_share_replay_state(self):
        body = {'schema_version': '1.0', 'message_id': str(uuid.uuid4()), 'correlation_id': str(uuid.uuid4()),
                'sender_agent_id': 'ag-a', 'action': 'list_agents', 'expires_at': time.time() + 5, 'payload': {}}
        first, nc1 = self._router()
        second, nc2 = self._router()
        asyncio.run(first.ingress(FakeMsg('ag-a', body)))
        asyncio.run(second.ingress(FakeMsg('ag-a', body)))
        self.assertEqual(nc1.published[-1][1]['code'], 'OK')
        self.assertEqual(nc2.published[-1][1]['code'], 'REPLAY_REJECTED')


if __name__ == '__main__':
    unittest.main()
