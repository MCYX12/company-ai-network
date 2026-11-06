import os
import time
import unittest
from unittest.mock import patch

from app import model_stub
from app.text_task import execute_text

ROW = {'agent_id': 'peer-x', 'tool': 'codex'}


class ModelStubTests(unittest.TestCase):
    def test_answer_is_deterministic_and_self_labelled(self):
        prompt = '问题：demo-project 进展如何？\n- [DATA_UNAVAILABLE] demo-project\n'
        first = model_stub.answer(prompt)
        second = model_stub.answer(prompt)
        self.assertEqual(first, second)
        self.assertIn('本地模型替身', first)
        self.assertIn('非真实模型', first)
        self.assertIn('DATA_UNAVAILABLE', first)
        self.assertIn('不编造', first)

    def test_execute_text_uses_stub_without_cloud_or_cli(self):
        with patch.dict(os.environ, {'POC_LOCAL_MODEL_STUB': '1'}, clear=False):
            os.environ.pop('ALLOW_CLOUD_MODEL_TESTS', None)
            code, data = execute_text(ROW, {'prompt': '问题：进展？\n- [DATA_UNAVAILABLE] p'}, time.time() + 30)
        self.assertEqual(code, 'OK')
        self.assertTrue(data['synthetic'])
        self.assertEqual(data['source'], model_stub.MARKER)
        self.assertIn('本地模型替身', data['text'])

    def test_without_stub_and_cloud_it_is_disabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('POC_LOCAL_MODEL_STUB', None)
            os.environ.pop('ALLOW_CLOUD_MODEL_TESTS', None)
            code, data = execute_text(ROW, {'prompt': 'hello'}, time.time() + 30)
        self.assertEqual(code, 'CLOUD_TEST_DISABLED')
        self.assertIsNone(data)

    def test_stub_rejects_malformed_payload(self):
        with patch.dict(os.environ, {'POC_LOCAL_MODEL_STUB': '1'}, clear=False):
            code, data = execute_text(ROW, {'prompt': 'x', 'extra': 'y'}, time.time() + 30)
        self.assertEqual(code, 'INVALID_REQUEST')


if __name__ == '__main__':
    unittest.main()
