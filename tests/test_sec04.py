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
from app.task_store import TaskStore


class FakeNc:
    def __init__(self):
        self.published = []

    async def publish(self, subject, data):
        self.published.append((subject, decode(data)))


class FakeMsg:
    def __init__(self, source, body):
        self.subject = f'corp.dev.v1.ingress.{source}.request'
        self.data = encode(body)


class Sec04Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        enrollment=patch.dict(os.environ, {'AGENT_REGISTRY':str(self.runtime/'registry.json')})
        enrollment.start();self.addCleanup(enrollment.stop)
        (self.runtime / 'registry.json').write_text(json.dumps({'schema_version': 1, 'agents': [
            {'agent_id': 'ag-a', 'device_id': 'ubuntu-demo', 'employee_id': 'demo-a', 'tool': 'codex', 'projects': ['demo-project'], 'enabled': True},
            {'agent_id': 'ag-b', 'device_id': 'mac-demo', 'employee_id': 'demo-b', 'tool': 'codex', 'projects': ['demo-project'], 'enabled': True}]}))
        store = AccessStore(self.runtime / 'conversation-access.sqlite3')
        row = store.request('ag-a', 'ag-b', ['demo-project'])
        store.decide(row['approval_id'], 'approved', 3600, 'sec04')

    def tearDown(self):
        self.tmp.cleanup()

    def _body(self, action, payload=None, target=None):
        body = {'schema_version': '1.0', 'message_id': str(uuid.uuid4()), 'correlation_id': str(uuid.uuid4()),
                'sender_agent_id': 'ag-a', 'action': action, 'expires_at': time.time() + 5, 'payload': payload or {}}
        if target:
            body['target_agent_id'] = target
        return body

    def _router(self):
        with (
            patch.dict(os.environ, {'AGENT_REGISTRY': str(self.runtime / 'registry.json')}),
            patch('app.router.RUNTIME', self.runtime),
            patch('app.conversation_access.RUNTIME', self.runtime),
            patch('app.common.RUNTIME', self.runtime),
            patch('app.admin_channel.RUNTIME', self.runtime),
        ):
            from app.router import Router
            nc = FakeNc()
            return Router(nc), nc

    def _send(self, router, source, body):
        asyncio.run(router.ingress(FakeMsg(source, body)))

    def test_replayed_message_id_is_rejected(self):
        router, nc = self._router()
        body = self._body('list_agents')
        self._send(router, 'ag-a', body)
        self.assertEqual(nc.published[-1][1]['code'], 'OK')
        self._send(router, 'ag-a', body)
        self.assertEqual(nc.published[-1][1]['code'], 'REPLAY_REJECTED')

    def test_distinct_message_ids_are_not_rejected(self):
        router, nc = self._router()
        self._send(router, 'ag-a', self._body('list_agents'))
        self._send(router, 'ag-a', self._body('list_agents'))
        self.assertEqual([p[1]['code'] for p in nc.published[-2:]], ['OK', 'OK'])

    def test_admin_action_replay_is_also_rejected(self):
        router, nc = self._router()
        body = self._body('admin_status')
        self._send(router, 'ag-a', body)         # ag-a is not an admin -> FORBIDDEN, but still recorded
        first = nc.published[-1][1]['code']
        self._send(router, 'ag-a', body)
        self.assertEqual(nc.published[-1][1]['code'], 'REPLAY_REJECTED')
        self.assertEqual(first, 'FORBIDDEN')

    def test_tampered_payload_is_rejected(self):
        router, nc = self._router()
        body = self._body('list_agents', payload={'extra': 'x'})
        self._send(router, 'ag-a', body)
        self.assertEqual(nc.published[-1][1]['code'], 'INVALID_REQUEST')

    def test_cross_project_query_is_forbidden(self):
        router, nc = self._router()
        body = self._body('get_project_progress', payload={'project_id': 'other-project'}, target='ag-b')
        self._send(router, 'ag-a', body)
        self.assertEqual(nc.published[-1][1]['code'], 'FORBIDDEN')

    def test_duplicate_result_does_not_rewrite_terminal_state(self):
        store = TaskStore(self.runtime / 'tasks.sqlite3')
        task = store.submit('ag-a', 'ag-b', 'k', 'ask_peer', {'question': 'x'}, 300)
        store.finish(task['task_id'], {'code': 'OK', 'data': {'n': 1}})
        store.finish(task['task_id'], {'code': 'FORBIDDEN', 'data': None})
        row = store.get(task['task_id'])
        self.assertEqual(row['state'], 'succeeded')
        self.assertEqual(json.loads(row['result'])['code'], 'OK')


if __name__ == '__main__':
    unittest.main()
