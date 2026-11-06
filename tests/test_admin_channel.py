import asyncio
import json
import os
import tempfile
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from app import admin_channel
from app.admin_channel import admins, handle
from app.common import encode, decode
from app.task_store import TaskStore, MAX_ATTEMPTS
from scripts import ca_tool


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        (self.runtime / 'certs').mkdir()
        self.ca = self.runtime / 'enterprise-ca'
        ca_tool.init_root(self.ca, days=30)
        ca_tool.init_intermediate(self.ca, days=30)
        ca_tool.issue(self.ca, 'ocsp-signer', 'ocsp', 30, 'http://127.0.0.1:2560')
        (self.runtime / 'certs/ENTERPRISE_CA.json').write_text(json.dumps({'format': 1, 'ca_dir': str(self.ca)}))
        (self.runtime / 'admin-identities.json').write_text(json.dumps({'admins': ['admin-01']}))

    def tearDown(self):
        self.tmp.cleanup()

    def _handle(self, action, payload=None):
        return handle(action, payload or {}, 'admin-01', 'corr-1', runtime=self.runtime)

    def test_admins_and_status(self):
        self.assertEqual(admins(self.runtime), {'admin-01'})
        result = self._handle('admin_status')
        self.assertIn('admin-01', result['admins'])
        result = self._handle('admin_ca_status')
        self.assertIn('issued', result['status'])

    def test_issue_then_revoke_and_audit_hides_key(self):
        issued = self._handle('admin_issue', {'name': 'laptop-01', 'kind': 'client', 'days': 30})
        self.assertIn('BEGIN CERTIFICATE', issued['cert'])
        self.assertIn('BEGIN PRIVATE KEY', issued['key'])
        self.assertIn('OK', ca_tool.verify_chain(self.ca, 'laptop-01'))
        self._handle('admin_revoke', {'name': 'laptop-01', 'reason': 'cessationOfOperation'})
        revoked = [row for row in ca_tool.status(self.ca)['issued'] if row['state'] == 'R']
        self.assertTrue(revoked)
        audit = (self.runtime / 'admin-history/channel.jsonl').read_text().splitlines()
        self.assertEqual(len(audit), 2)
        self.assertNotIn('PRIVATE KEY', ''.join(audit))
        self.assertEqual(json.loads(audit[0])['action'], 'admin_issue')

    def test_dlq_list_and_requeue(self):
        store = TaskStore(self.runtime / 'tasks.sqlite3')
        task = store.submit('ag-a', 'ag-b', 'k1', 'ask_peer', {'question': 'x'}, 300)
        for _ in range(MAX_ATTEMPTS + 1):
            store.sent(task['task_id'])
        listed = self._handle('admin_dlq_list', {'limit': 10})
        self.assertEqual(len(listed['dead_letters']), 1)
        self._handle('admin_dlq_requeue', {'task_id': task['task_id']})
        self.assertEqual(TaskStore(self.runtime / 'tasks.sqlite3').dead_letters(), [])

    def test_backup_and_errors(self):
        TaskStore(self.runtime / 'tasks.sqlite3').submit('ag-a', 'ag-b', 'k-backup', 'ask_peer', {'question': 'x'}, 300)
        result = self._handle('admin_backup')
        self.assertTrue(Path(result['output']).exists())
        with self.assertRaises(ValueError):
            self._handle('admin_nope')
        with self.assertRaises(ValueError):
            self._handle('admin_issue', {'name': 'BAD NAME'})

    def test_non_admin_validation_of_events(self):
        # the audit channel is append-only JSONL; malformed runtime just yields no admins
        (self.runtime / 'admin-identities.json').write_text('not json')
        self.assertEqual(admins(self.runtime), set())


class FakeNc:
    def __init__(self):
        self.published = []

    async def publish(self, subject, data):
        self.published.append((subject, decode(data)))


class FakeMsg:
    def __init__(self, source, body):
        self.subject = f'corp.dev.v1.ingress.{source}.request'
        self.data = encode(body)


class RouterAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        (self.runtime / 'registry.json').write_text(json.dumps({'schema_version': 1, 'agents': [
            {'agent_id': 'ag-a', 'device_id': 'ubuntu-demo', 'employee_id': 'demo-a', 'tool': 'fixture', 'projects': ['demo-project'], 'enabled': True}]}))
        (self.runtime / 'admin-identities.json').write_text(json.dumps({'admins': ['admin-01']}))

    def tearDown(self):
        self.tmp.cleanup()

    def _body(self, source, action, payload=None):
        import uuid
        return {'schema_version': '1.0', 'message_id': str(uuid.uuid4()), 'correlation_id': str(uuid.uuid4()),
                'sender_agent_id': source, 'action': action, 'expires_at': time.time() + 5, 'payload': payload or {}}

    def _ingress(self, source, action, payload=None):
        with (
            patch.dict(os.environ, {'AGENT_REGISTRY': str(self.runtime / 'registry.json')}),
            patch('app.router.RUNTIME', self.runtime),
            patch('app.admin_channel.RUNTIME', self.runtime),
            patch('app.common.RUNTIME', self.runtime),
        ):
            from app.router import Router
            nc = FakeNc()
            router = Router(nc)
            asyncio.run(router.ingress(FakeMsg(source, self._body(source, action, payload))))
        return nc.published[-1][1]

    def test_admin_identity_can_call_admin_action(self):
        response = self._ingress('admin-01', 'admin_status')
        self.assertEqual(response['code'], 'OK')

    def test_normal_identity_cannot_call_admin_action(self):
        response = self._ingress('ag-a', 'admin_status')
        self.assertEqual(response['code'], 'FORBIDDEN')

    def test_admin_only_identity_cannot_call_normal_action(self):
        response = self._ingress('admin-01', 'list_agents')
        self.assertEqual(response['code'], 'FORBIDDEN')


if __name__ == '__main__':
    unittest.main()
