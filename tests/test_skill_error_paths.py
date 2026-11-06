"""Skill-visible error paths that the live drill does not replay, pinned deterministically.

`company-agent-chat` has to tell three different stories apart, and getting them wrong is
what makes a user chase the wrong problem:

  * device-owner history consent missing/expired  → HISTORY_ACCESS_DENIED / HISTORY_ACCESS_EXPIRED
    (a grant problem, never "the project does not exist");
  * the local summariser failing                   → LOCAL_SUMMARY_UNAVAILABLE
    (counts are not progress; the project is not missing);
  * a proposed directory that is not the real path → CASE_MISMATCH with candidates
    (the device must search and resubmit, the platform must not invent the path).

None of these calls a model or touches the live platform.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import claude_history, model_budget, peer_question
from app.claude_history import HistoryAccessError
from app.directory_requests import DirectoryRequests, RootRejected

ROW = {'agent_id': 'mac-claude-code-01', 'tool': 'claude-code', 'projects': ['demo-project'],
       'enabled': True, 'device_id': 'mac-demo', 'employee_id': 'demo-b'}
PAYLOAD = {'question': '这个项目现在进展如何？', 'projects': ['demo-project'], 'caller': 'ag-a'}


class DeviceOwnerConsentTests(unittest.TestCase):
    """`settings()` is the device owner's own consent file; the caller must see its code."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        self.policy = self.runtime / 'claude-history-access.json'
        self.patchers = [patch.object(claude_history, 'RUNTIME', self.runtime),
                         patch.object(model_budget, 'RUNTIME', self.runtime),
                         patch.object(claude_history, 'known_projects', return_value=[])]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def policy_for(self, **overrides):
        value = {'root': str(Path.home() / '.claude/projects'), 'callers': ['ag-a'],
                 'target': 'mac-claude-code-01', 'expires': time.time() + 3600,
                 'enabled': True, 'mode': 'local_only'}
        value.update(overrides)
        self.policy.write_text(json.dumps(value))
        self.policy.chmod(0o600)
        return value

    def test_disabled_consent_is_an_access_error_not_a_missing_project(self):
        self.policy_for(enabled=False)
        with self.assertRaises(HistoryAccessError) as caught:
            claude_history.answer(ROW, PAYLOAD, time.time() + 30)
        self.assertEqual(str(caught.exception), 'HISTORY_ACCESS_DENIED')
        code, data = peer_question.execute_question(ROW, PAYLOAD, time.time() + 30)
        self.assertEqual(code, 'INVALID_REQUEST')
        self.assertEqual(data['hint'], 'HISTORY_ACCESS_DENIED')
        self.assertIn('历史资料授权', data['next_action'])

    def test_caller_not_listed_is_an_access_error(self):
        self.policy_for(callers=['someone-else'])
        code, data = peer_question.execute_question(ROW, PAYLOAD, time.time() + 30)
        self.assertEqual((code, data['hint']), ('INVALID_REQUEST', 'HISTORY_ACCESS_DENIED'))

    def test_expired_consent_reports_expiry(self):
        self.policy_for(expires=time.time() - 5)
        with self.assertRaises(HistoryAccessError) as caught:
            claude_history.answer(ROW, PAYLOAD, time.time() + 30)
        self.assertEqual(str(caught.exception), 'HISTORY_ACCESS_EXPIRED')
        code, data = peer_question.execute_question(ROW, PAYLOAD, time.time() + 30)
        self.assertEqual((code, data['hint']), ('INVALID_REQUEST', 'HISTORY_ACCESS_EXPIRED'))

    def test_absent_policy_is_not_an_error_just_no_history(self):
        # No file at all: the device owner simply never enabled it, so the answer falls back
        # to published records instead of reporting an access failure.
        self.assertIsNone(claude_history.settings('ag-a', 'mac-claude-code-01'))


class LocalSummariserTests(unittest.TestCase):
    """A local-summary failure marks availability, never "project missing"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        self.row = {'name': 'qwen-mac', 'kind': 'project_activity', 'project_path': '/Users/mac/Qwen mac',
                    'sessions': 3, 'last_activity': '2026-09-30', 'read_requests': 2, 'edit_requests': 1,
                    'topics': ['desktop'], 'progress_signals': [], 'score': 5}
        self.coverage = {'files': 3, 'limited': False, 'skipped': 0, 'missing': False}
        self.patchers = [patch.object(claude_history, 'RUNTIME', self.runtime),
                         patch.object(claude_history, 'settings', return_value={'root': '/tmp', 'mode': 'local_only'}),
                         patch.object(claude_history, 'scan', return_value=([dict(self.row)], self.coverage)),
                         patch.object(claude_history, 'ensure_report', side_effect=OSError('local model down'))]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def test_summariser_failure_is_reported_as_availability_not_absence(self):
        code, data = claude_history.answer({'agent_id': 'mac-claude-code-01', 'tool': 'claude-code'},
                                           PAYLOAD, time.time() + 30)
        self.assertEqual(code, 'OK')
        self.assertEqual(data['projects'][0]['progress_status'], 'LOCAL_SUMMARY_UNAVAILABLE')
        self.assertEqual(data['projects'][0]['progress_error_type'], 'OSError')
        # The skill must read this as "local summariser failed", never as "no such project".
        self.assertIn('不能把下列计数当作项目进度', data['text'])
        self.assertNotIn('没有找到可识别的主会话项目记录', data['text'])
        self.assertEqual(data['source'], 'local_claude_history_index')


class DirectoryRefusalTests(unittest.TestCase):
    """Refusals are codes plus a sentence, and the real path is never invented."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        (self.base / 'QwenMac').mkdir()
        self.store = DirectoryRequests(self.base / 'directory-requests.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_case_variant_refused_with_candidates(self):
        with self.assertRaises(RootRejected) as caught:
            self.store.request('ag-a', 'mac-codex-01', str(self.base / 'qwenmac'))
        self.assertEqual(caught.exception.code, 'CASE_MISMATCH')
        self.assertIn(str(self.base / 'QwenMac'), caught.exception.candidates)
        # The refusal carries a sentence a human can act on, and names no invented path.
        self.assertIn('大小写', str(caught.exception))

    def test_syntax_refusals_carry_a_stable_code(self):
        cases = {'/': 'TOO_SHORT', 'relative/path': 'NOT_ABSOLUTE', '/home/demo': 'TOO_BROAD',
                 '/home/demo/../etc/proj': 'PARENT_TRAVERSAL', '/home/demo/.hidden/proj': 'HIDDEN_COMPONENT',
                 '/home/demo//proj': 'NOT_NORMALIZED'}
        for root, expected in cases.items():
            with self.assertRaises(RootRejected) as caught:
                self.store.request('ag-a', 'mac-codex-01', root)
            self.assertEqual(caught.exception.code, expected, root)

    def test_refusal_does_not_leave_a_pending_request(self):
        with self.assertRaises(RootRejected):
            self.store.request('ag-a', 'mac-codex-01', str(self.base / 'qwenmac'))
        self.assertEqual(self.store.listing('ag-a', 'mac-codex-01'), [])


if __name__ == '__main__':
    unittest.main()
