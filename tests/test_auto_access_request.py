"""Asking an agent without a live grant files the conversation request automatically."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.conversation_access as conversation_access
import app.router as router_module
from app.common import PREFIX, decode, encode, request
from app.router import Router


def registry():
    return {'alice': {'agent_id': 'alice', 'device_id': 'dev', 'employee_id': 'e', 'tool': 'fixture',
                      'projects': ['p'], 'enabled': True},
            'mac': {'agent_id': 'mac', 'device_id': 'dev', 'employee_id': 'e', 'tool': 'codex',
                    'projects': ['p'], 'enabled': True}}


def submit_message(key='k1'):
    body = request('alice', 'mac', 'ask_peer', None)
    body['action'] = 'submit_task'
    body['payload'] = {'operation': 'ask_peer', 'question': 'hi',
                       'idempotency_key': key, 'ttl_seconds': 60}
    return body


class AutoRequestTests(unittest.IsolatedAsyncioTestCase):
    async def ask(self, router, nc, key='k1'):
        msg = SimpleNamespace(subject=f'{PREFIX}.ingress.alice.request', data=encode(submit_message(key)))
        await router.ingress(msg)
        return [decode(call.args[1]) for call in nc.publish.call_args_list
                if call.args[0] == f'{PREFIX}.client.alice.response']

    async def test_ask_without_grant_files_a_pending_request(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(router_module, 'RUNTIME', Path(tmp)), \
             patch.object(conversation_access, 'RUNTIME', Path(tmp)), \
             patch.object(router_module, 'load_registry', return_value=registry()):
            nc = AsyncMock()
            router = Router(nc)
            replies = await self.ask(router, nc)
            self.assertEqual(replies[-1]['code'], 'APPROVAL_REQUIRED')
            self.assertEqual(replies[-1]['data']['state'], 'pending')
            self.assertTrue(replies[-1]['data']['approval_id'])
            stored = conversation_access.AccessStore(Path(tmp) / 'conversation-access.sqlite3').listing('alice')
            self.assertEqual([row['state'] for row in stored], ['pending'])

    async def test_asking_again_reuses_the_same_request(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(router_module, 'RUNTIME', Path(tmp)), \
             patch.object(conversation_access, 'RUNTIME', Path(tmp)), \
             patch.object(router_module, 'load_registry', return_value=registry()):
            nc = AsyncMock()
            router = Router(nc)
            first = (await self.ask(router, nc, 'k1'))[-1]
            second = (await self.ask(router, nc, 'k2'))[-1]
            self.assertEqual(first['data']['approval_id'], second['data']['approval_id'])
            stored = conversation_access.AccessStore(Path(tmp) / 'conversation-access.sqlite3').listing('alice')
            self.assertEqual(len(stored), 1)

    async def test_ask_without_shared_project_is_still_forbidden(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(router_module, 'RUNTIME', Path(tmp)), \
             patch.object(conversation_access, 'RUNTIME', Path(tmp)), \
             patch.object(router_module, 'load_registry', return_value=registry()):
            nc = AsyncMock()
            router = Router(nc)
            body = submit_message()
            body['payload']['projects'] = ['p']       # client payload is not the authority
            msg = SimpleNamespace(subject=f'{PREFIX}.ingress.alice.request', data=encode(body))
            with patch.object(router_module, 'load_registry',
                              return_value={'alice': registry()['alice'], 'mac': registry()['mac']}):
                await router.ingress(msg)
            replies = [decode(call.args[1]) for call in nc.publish.call_args_list
                       if call.args[0] == f'{PREFIX}.client.alice.response']
            self.assertNotEqual(replies[-1]['code'], 'APPROVAL_REQUIRED')


if __name__ == '__main__':
    unittest.main()
