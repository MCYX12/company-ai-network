import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ollama_adapter', Path(__file__).resolve().parents[1] / 'packaging/client/ollama_adapter.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class OllamaAdapterTests(unittest.TestCase):
    def replies(self, **changes):
        return [{'models': [{'name': 'qwen2.5:7b'}]}, {'capabilities': ['completion']},
                dict(response='中文答复', done=True, done_reason='stop', eval_count=5, **changes)]

    def test_request_uses_fixed_local_endpoint_no_proxy_and_no_redirect(self):
        with patch.object(a, 'build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"models": []}'
            a.request_json('/api/tags')
            handlers = opener.call_args.args
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], a.NoRedirect)
            self.assertEqual(opener.return_value.open.call_args.args[0].full_url,
                             'http://127.0.0.1:11434/api/tags')
        with self.assertRaises(ValueError):
            a.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://outside.example')

    def test_generation_has_no_tools_or_history_and_preserves_unicode(self):
        with patch.object(a, 'request_json', side_effect=self.replies()) as call:
            self.assertEqual(a.generate('中文问题', 'qwen2.5:7b')['response'], '中文答复')
            payload = call.call_args_list[-1].args[1]
            self.assertEqual(payload['prompt'], '中文问题')
            self.assertFalse(payload['stream'])
            self.assertNotIn('context', payload)
            self.assertNotIn('tools', payload)

    def test_uninstalled_remote_and_embedding_models_refused(self):
        for replies in ([{'models': []}],
                        [{'models': [{'name': 'qwen2.5:7b'}]}, {'capabilities': ['completion'], 'remote_host': 'https://cloud.example'}],
                        [{'models': [{'name': 'qwen2.5:7b'}]}, {'capabilities': ['embedding']}]):
            with patch.object(a, 'request_json', side_effect=replies), self.assertRaises(ValueError):
                a.generate('问题', 'qwen2.5:7b')
        with patch.object(a, 'request_json', side_effect=AssertionError('network')), self.assertRaises(ValueError):
            a.generate('问题', 'x:cloud')

    def test_empty_incomplete_truncated_and_oversize_answers_fail(self):
        for reply in [dict(response='', done=True), dict(response='partial', done=False),
                      dict(response='partial', done=True, done_reason='length')]:
            replies = self.replies(); replies[-1] = reply
            with patch.object(a, 'request_json', side_effect=replies), self.assertRaises(ValueError):
                a.generate('问题', 'qwen2.5:7b')
        with patch.object(a, 'build_opener') as opener, self.assertRaises(ValueError):
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'x' * (a.MAX_RESPONSE + 1)
            a.request_json('/api/tags')

    def test_oversize_prompt_refused_before_network(self):
        with patch.object(a, 'request_json', side_effect=AssertionError('network')), self.assertRaises(ValueError):
            a.generate('x' * (a.MAX_PROMPT + 1), 'qwen2.5:7b')
