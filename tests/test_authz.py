import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.authz import decide, decide_egress, history_scope
from app.conversation_access import AccessStore
from app.file_access import FileAccess
from app.history_access import HistoryAccess
from app.model_budget import ModelBudget

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'
ROOT = Path(__file__).resolve().parents[1]


class AuthzTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.runtime = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _approve_conversation(self):
        store = AccessStore(self.runtime / 'conversation-access.sqlite3')
        row = store.request('ag-a', 'peer-x', ['demo-project'])
        store.decide(row['approval_id'], 'approved', 3600, 'test')

    def test_conversation_requires_approval(self):
        denied = decide('ag-a', 'peer-x', 'conversation', project='demo-project', runtime=self.runtime)
        self.assertFalse(denied['allowed'])
        self.assertEqual(denied['reason'], 'NO_APPROVAL')
        self._approve_conversation()
        allowed = decide('ag-a', 'peer-x', 'conversation', project='demo-project', runtime=self.runtime)
        self.assertTrue(allowed['allowed'])
        self.assertEqual(allowed['reason'], 'OK')

    def test_file_scope_and_revocation(self):
        self.assertEqual(decide('ag-a', 'peer-x', 'file', project='demo-project', runtime=self.runtime)['reason'], 'NO_SCOPE')
        store = FileAccess(self.runtime / 'file-access.sqlite3')
        scope_id = store.create('ag-a', 'peer-x', 'demo-project', str(ROOT / 'README.md'), seconds=3600)
        self.assertTrue(decide('ag-a', 'peer-x', 'file', project='demo-project', runtime=self.runtime)['allowed'])
        store.revoke(scope_id)
        self.assertEqual(decide('ag-a', 'peer-x', 'file', project='demo-project', runtime=self.runtime)['reason'], 'NO_SCOPE')

    def test_history_grant(self):
        # No administrative record means the device owner decides (UNMANAGED), matching the
        # execution path; the facade used to call this NO_GRANT.
        unmanaged = decide('ag-a', 'peer-x', 'history', runtime=self.runtime)
        self.assertTrue(unmanaged['allowed'])
        self.assertFalse(unmanaged['managed'])
        store = HistoryAccess(self.runtime / 'history-access.sqlite3')
        row = store.request('ag-a', 'peer-x')
        store.decide(row['id'], 'approved', 3600, 'test', 'admin')
        self.assertTrue(decide('ag-a', 'peer-x', 'history', runtime=self.runtime)['allowed'])

    def test_model_egress_follows_conversation_then_budget(self):
        self.assertEqual(decide('ag-a', 'peer-x', 'model_egress', project='demo-project', runtime=self.runtime)['reason'], 'NO_APPROVAL')
        self._approve_conversation()
        self.assertTrue(decide('ag-a', 'peer-x', 'model_egress', project='demo-project', runtime=self.runtime)['allowed'])
        with patch.dict(os.environ, {'POC_MODEL_CALL_BUDGET': '1'}):
            ModelBudget(self.runtime / 'model-budget.sqlite3', limit=1).spend('ag-a', 'peer-x')
            denied = decide('ag-a', 'peer-x', 'model_egress', project='demo-project', runtime=self.runtime)
        self.assertEqual(denied['reason'], 'BUDGET_EXCEEDED')

    def test_policy_conversation_matches_authz(self):
        from app import policy
        registry = {
            'ag-a': {'agent_id': 'ag-a', 'device_id': 'd', 'employee_id': 'e', 'tool': 'codex', 'projects': ['demo-project'], 'enabled': True},
            'peer-x': {'agent_id': 'peer-x', 'device_id': 'd', 'employee_id': 'e', 'tool': 'codex', 'projects': ['demo-project'], 'enabled': True}}
        with patch('app.conversation_access.RUNTIME', self.runtime):
            before = policy.allowed('ag-a', 'peer-x', 'ask_peer', {'projects': ['demo-project']}, registry)
            self._approve_conversation()
            after = policy.allowed('ag-a', 'peer-x', 'ask_peer', {'projects': ['demo-project']}, registry)
        self.assertFalse(before)
        self.assertTrue(after)
        self.assertEqual(after, decide('ag-a', 'peer-x', 'conversation', projects=['demo-project'], runtime=self.runtime)['allowed'])

    def test_model_egress_budget_only_and_with_conversation(self):
        with patch.dict(os.environ, {'POC_MODEL_CALL_BUDGET': '1'}):
            ModelBudget(self.runtime / 'model-budget.sqlite3', limit=1).spend('ag-a', 'peer-x')
            budget_only = decide('ag-a', 'peer-x', 'model_egress', runtime=self.runtime, require_conversation=False)
            with_conversation = decide('ag-a', 'peer-x', 'model_egress', projects=['demo-project'], runtime=self.runtime)
        self.assertEqual(budget_only['reason'], 'BUDGET_EXCEEDED')
        self.assertEqual(budget_only['source'], 'model_budget')
        # with conversation required, a missing approval is reported before the budget
        self.assertEqual(with_conversation['reason'], 'NO_APPROVAL')

    def test_history_and_file_agree_with_stores(self):
        store = HistoryAccess(self.runtime / 'history-access.sqlite3')
        # No administrative record = UNMANAGED: the device owner decides, exactly like the
        # execution path (authz.history_scope). The facade used to report NO_GRANT here.
        unmanaged = decide('ag-a', 'peer-x', 'history', runtime=self.runtime)
        self.assertTrue(unmanaged['allowed'])
        self.assertFalse(unmanaged['managed'])
        row = store.request('ag-a', 'peer-x')
        store.decide(row['id'], 'approved', 3600, 'test', 'admin')
        self.assertTrue(decide('ag-a', 'peer-x', 'history', runtime=self.runtime)['allowed'])
        files = FileAccess(self.runtime / 'file-access.sqlite3')
        scope = files.create('ag-a', 'peer-x', 'demo-project', str(ROOT / 'README.md'), seconds=3600)
        self.assertTrue(decide('ag-a', 'peer-x', 'file', project='demo-project', runtime=self.runtime)['allowed'])
        files.revoke(scope)
        self.assertEqual(decide('ag-a', 'peer-x', 'file', project='demo-project', runtime=self.runtime)['reason'], 'NO_SCOPE')

    def test_history_scope_must_match_requested_project(self):
        store = HistoryAccess(self.runtime / 'history-access.sqlite3')
        row = store.request('ag-a', 'peer-x', scope='project-a')
        store.decide(row['id'], 'approved', 3600, 'test', 'admin')
        self.assertTrue(decide('ag-a', 'peer-x', 'history', project='project-a', runtime=self.runtime)['allowed'])
        mismatch = decide('ag-a', 'peer-x', 'history', project='project-b', runtime=self.runtime)
        self.assertFalse(mismatch['allowed'])
        self.assertEqual(mismatch['reason'], 'SCOPE_MISMATCH')
        # An unrestricted query past the grant still resolves to the grant.
        self.assertTrue(decide('ag-a', 'peer-x', 'history', runtime=self.runtime)['allowed'])

    def test_history_facade_agrees_with_the_execution_path_on_explicit_deny(self):
        store = HistoryAccess(self.runtime / 'history-access.sqlite3')
        row = store.request('ag-a', 'peer-x', scope='project-a')
        store.decide(row['id'], 'denied', 86400, 'veto', 'admin')
        self.assertFalse(decide('ag-a', 'peer-x', 'history', project='project-a', runtime=self.runtime)['allowed'])
        self.assertFalse(decide('ag-a', 'peer-x', 'history', project='other', runtime=self.runtime)['allowed'])

    def test_unknown_kind(self):
        self.assertEqual(decide('ag-a', 'peer-x', 'nonsense', runtime=self.runtime)['reason'], 'UNKNOWN_KIND')

    def test_egress_requires_cloud_approved_scope(self):
        self._approve_conversation()
        files = FileAccess(self.runtime / 'file-access.sqlite3')
        local = files.create('ag-a', 'peer-x', 'demo-project', str(ROOT / 'README.md'), seconds=3600, cloud=False)
        self.assertEqual(decide_egress('ag-a', 'peer-x', project='demo-project', scope_ids=[local],
                                       runtime=self.runtime, require_conversation=False)['reason'],
                         'EGRESS_NOT_APPROVED')
        cloud = files.create('ag-a', 'peer-x', 'demo-project', str(ROOT / 'README.md'), seconds=3600, cloud=True)
        self.assertTrue(decide_egress('ag-a', 'peer-x', project='demo-project', scope_ids=[cloud],
                                      runtime=self.runtime, require_conversation=False)['allowed'])

    def test_egress_scope_mismatch_and_revocation(self):
        self._approve_conversation()
        files = FileAccess(self.runtime / 'file-access.sqlite3')
        other = files.create('ag-a', 'other-target', 'demo-project', str(ROOT / 'README.md'), seconds=3600, cloud=True)
        self.assertEqual(decide_egress('ag-a', 'peer-x', project='demo-project', scope_ids=[other],
                                       runtime=self.runtime, require_conversation=False)['reason'],
                         'SCOPE_MISMATCH')
        scope = files.create('ag-a', 'peer-x', 'demo-project', str(ROOT / 'README.md'), seconds=3600, cloud=True)
        files.revoke(scope)
        self.assertEqual(decide_egress('ag-a', 'peer-x', project='demo-project', scope_ids=[scope],
                                       runtime=self.runtime, require_conversation=False)['reason'],
                         'NO_SCOPE')

    def test_egress_budget_blocks_even_with_cloud_scope(self):
        self._approve_conversation()
        files = FileAccess(self.runtime / 'file-access.sqlite3')
        scope = files.create('ag-a', 'peer-x', 'demo-project', str(ROOT / 'README.md'), seconds=3600, cloud=True)
        with patch.dict(os.environ, {'POC_MODEL_CALL_BUDGET': '1'}):
            ModelBudget(self.runtime / 'model-budget.sqlite3', limit=1).spend('ag-a', 'peer-x')
            decision = decide_egress('ag-a', 'peer-x', project='demo-project', scope_ids=[scope],
                                     runtime=self.runtime, require_conversation=False)
        self.assertEqual(decision['reason'], 'BUDGET_EXCEEDED')

    def test_history_scope_facade_preserves_tri_state(self):
        far = 1790000000.0
        now = far - 100
        unmanaged = history_scope('ag-a', 'peer-x', [], now=now)
        self.assertTrue(unmanaged['allowed'])
        self.assertFalse(unmanaged['managed'])
        self.assertEqual(unmanaged['scopes'], set())
        approved = history_scope('ag-a', 'peer-x', [
            {'caller': 'ag-a', 'target': 'peer-x', 'state': 'approved', 'expires': far, 'scope': '*'}], now=now)
        self.assertTrue(approved['allowed'])
        self.assertTrue(approved['managed'])
        self.assertEqual(approved['scopes'], {'*'})
        mismatch = history_scope('ag-a', 'peer-x', [
            {'caller': 'ag-a', 'target': 'peer-x', 'state': 'approved', 'expires': far, 'scope': 'project-a'}],
            project='project-b', now=now)
        self.assertFalse(mismatch['allowed'])
        self.assertEqual(mismatch['reason'], 'SCOPE_MISMATCH')


if __name__ == '__main__':
    unittest.main()
