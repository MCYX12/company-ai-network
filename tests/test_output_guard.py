import os
import tempfile
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from app.output_guard import scan, sanitize, guard_result
from app.model_budget import ModelBudget, daily_limit
from app.peer_question import execute_question

ROW = {'agent_id': 'mac-x', 'tool': 'claude-code', 'projects': ['demo-project']}
PAYLOAD = {'question': '进展', 'projects': ['demo-project'], 'caller': 'ag-a'}


class FakeStore:
    def listing(self):
        return []

    def read(self, *args, **kwargs):
        return []

    def check(self, ident):
        pass


class GuardResultTests(unittest.TestCase):
    def test_non_ok_and_non_text_pass_through(self):
        self.assertEqual(guard_result('EXECUTION_FAILED', None), ('EXECUTION_FAILED', None))
        self.assertEqual(guard_result('OK', {'progress': 1}), ('OK', {'progress': 1}))

    def test_credential_is_redacted(self):
        code, data = guard_result('OK', {'text': 'password: hunter2 end'})
        self.assertEqual(code, 'OK')
        self.assertNotIn('hunter2', data['text'])
        self.assertIn('CREDENTIAL_REDACTED', data['output_warnings'])

    def test_canary_blocks(self):
        with patch.dict(os.environ, {'POC_OUTPUT_CANARY': 'GUARDCANARY'}):
            code, data = guard_result('OK', {'text': 'leak GUARDCANARY here'})
        self.assertEqual(code, 'OUTPUT_BLOCKED')
        self.assertEqual(data['hint'], 'OUTPUT_CANARY_BLOCKED')

    def test_semantic_finding_is_annotated(self):
        code, data = guard_result('OK', {'text': 'I executed the command successfully.'})
        self.assertEqual(code, 'OK')
        self.assertIn('output_review', data)


class GuardTests(unittest.TestCase):
    def test_scan_finds_credential_canary_and_oversize(self):
        self.assertEqual(scan('normal answer'), {})
        self.assertTrue(scan('api_key: abc123')['credential'])
        self.assertTrue(scan('here CANARY there', canary='CANARY')['canary'])
        self.assertTrue(scan('x' * 17000)['oversize'])

    def test_sanitize_redacts_truncates_and_blocks_canary(self):
        clean, warnings = sanitize('credential password: hunter2 here')
        self.assertNotIn('hunter2', clean)
        self.assertIn('CREDENTIAL_REDACTED', warnings)
        clean, warnings = sanitize('a' * 17000)
        self.assertTrue(clean.endswith('[输出过长，已截断]'))
        self.assertIn('TRUNCATED', warnings)
        clean, warnings = sanitize('leak CANARY leak', canary='CANARY')
        self.assertIn('CANARY_BLOCKED', warnings)
        self.assertNotIn('CANARY', clean)
        self.assertEqual(sanitize(None), ('', ['NOT_TEXT']))


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.budget = ModelBudget(Path(self.tmp.name) / 'model-budget.sqlite3', limit=2)
        self.now = 1790000000.0

    def tearDown(self):
        self.tmp.cleanup()

    def test_allow_spend_and_daily_rollover(self):
        self.assertTrue(self.budget.allow('ag-a', 'mac-x', self.now))
        self.assertEqual(self.budget.spend('ag-a', 'mac-x', self.now), 1)
        self.assertEqual(self.budget.spend('ag-a', 'mac-x', self.now), 2)
        self.assertFalse(self.budget.allow('ag-a', 'mac-x', self.now))
        self.assertEqual(self.budget.remaining('ag-a', 'mac-x', self.now), 0)
        self.assertTrue(self.budget.allow('ag-a', 'mac-x', self.now + 86400))
        self.assertEqual(self.budget.used('ag-a', 'mac-x', self.now + 86400), 0)

    def test_env_limit(self):
        with patch.dict(os.environ, {'POC_MODEL_CALL_BUDGET': '3'}):
            self.assertEqual(daily_limit(), 3)
        with patch.dict(os.environ, {'POC_MODEL_CALL_BUDGET': 'bad'}):
            self.assertEqual(daily_limit(), 50)


class ExecuteQuestionGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.budget = ModelBudget(Path(self.tmp.name) / 'model-budget.sqlite3', limit=10)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, text, budget=None):
        with (
            patch('app.peer_question.read_progress', return_value=('DATA_UNAVAILABLE', None)),
            patch('app.file_access.FileAccess', return_value=FakeStore()),
            patch('app.peer_question.execute_text', return_value=('OK', {'text': text})),
            patch('app.claude_history.answer', return_value=None),
            patch('app.model_budget.ModelBudget', return_value=budget or self.budget),
        ):
            return execute_question(ROW, PAYLOAD, time.time() + 60)

    def test_credential_is_redacted(self):
        code, data = self._run('network answer password: hunter2 end')
        self.assertEqual(code, 'OK')
        self.assertNotIn('hunter2', data['text'])
        self.assertIn('CREDENTIAL_REDACTED', data['output_warnings'])

    def test_canary_blocks_output(self):
        with patch.dict(os.environ, {'POC_OUTPUT_CANARY': 'LEAKCANARY'}):
            code, data = self._run('the secret marker is LEAKCANARY here')
        self.assertEqual(code, 'OUTPUT_BLOCKED')
        self.assertEqual(data['hint'], 'OUTPUT_CANARY_BLOCKED')

    def test_exhausted_budget_returns_budget_exceeded(self):
        exhausted = ModelBudget(Path(self.tmp.name) / 'exhausted.sqlite3', limit=1)
        exhausted.spend('ag-a', 'mac-x')
        code, data = self._run('answer', budget=exhausted)
        self.assertEqual(code, 'BUDGET_EXCEEDED')
        self.assertEqual(data['hint'], 'MODEL_CALL_BUDGET_EXCEEDED')


if __name__ == '__main__':
    unittest.main()
