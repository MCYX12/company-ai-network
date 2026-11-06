"""Cross-boundary answers are guarded once at the Router trust boundary.

A device may run an older build, or be compromised, and return an answer without
running its own output guard. The Router must therefore apply the policy itself
(learned from a2a-samples: treat every remote agent's data as untrusted input).
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.common import PREFIX, encode


class RouterOutputGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _submit_and_result(self, data, code='OK'):
        from app.router import Router
        with patch('app.router.RUNTIME', Path(self.tmp.name)), \
             patch('app.directory_requests.RUNTIME', Path(self.tmp.name)), \
             patch('app.router.allowed', return_value=True):
            router = Router(SimpleNamespace(publish=AsyncMock()))
            row = router.tasks.submit('owner', 'peer', 'key', 'ask_peer',
                                      {'caller': 'owner', 'projects': ['p'], 'question': 'x'}, 120)
            body = {'task_id': row['task_id'], 'code': code, 'data': data}
            asyncio.run(router.task_result(SimpleNamespace(
                subject=f'{PREFIX}.agent.peer.result', data=encode(body))))
            result = json.loads(router.tasks.get(row['task_id'])['result'])
            router.tasks.db.close()
            return result

    def test_credential_redacted_from_unguarded_device_result(self):
        result = self._submit_and_result({'text': 'remote answer password: hunter2 end'})
        self.assertEqual(result['code'], 'OK')
        self.assertNotIn('hunter2', result['data']['text'])
        self.assertIn('CREDENTIAL_REDACTED', result['data']['output_warnings'])

    def test_canary_blocks_from_unguarded_device_result(self):
        with patch.dict(os.environ, {'POC_OUTPUT_CANARY': 'ROUTERCANARY'}):
            result = self._submit_and_result({'text': 'marker ROUTERCANARY here'})
        self.assertEqual(result['code'], 'OUTPUT_BLOCKED')
        self.assertEqual(result['data']['hint'], 'OUTPUT_CANARY_BLOCKED')

    def test_non_ok_result_passes_through(self):
        result = self._submit_and_result(None, code='EXECUTION_FAILED')
        self.assertEqual(result['code'], 'EXECUTION_FAILED')

    def test_oversize_result_is_truncated_at_router(self):
        result = self._submit_and_result({'text': 'a' * 17000})
        self.assertEqual(result['code'], 'OK')
        self.assertTrue(result['data']['text'].endswith('[输出过长，已截断]'))
        self.assertIn('TRUNCATED', result['data']['output_warnings'])


if __name__ == '__main__':
    unittest.main()
