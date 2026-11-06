import tempfile
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from app.history_access import HistoryAccess, allowed_scopes, decision, handle
from app.claude_history import scoped


class DecisionTests(unittest.TestCase):
    def test_unmanaged_when_no_relevant_grant(self):
        self.assertEqual(decision('ag-a', 'mac', []), 'UNMANAGED')
        other = [{'caller': 'ag-b', 'target': 'mac', 'state': 'approved', 'expires': time.time() + 60}]
        self.assertEqual(decision('ag-a', 'mac', other), 'UNMANAGED')
        self.assertEqual(decision('ag-a', 'mac', None), 'DENIED')

    def test_approved_requires_active_approval(self):
        now = time.time()
        approved = [{'caller': 'ag-a', 'target': 'mac', 'state': 'approved', 'expires': now + 60}]
        self.assertEqual(decision('ag-a', 'mac', approved, now), 'APPROVED')
        # Only an explicit denial is a standing veto; lapsed records hand the decision back
        # to the device owner instead of silently disabling their local history.
        denied = [{'caller': 'ag-a', 'target': 'mac', 'state': 'denied', 'expires': 0}]
        self.assertEqual(decision('ag-a', 'mac', denied, now), 'DENIED')
        for state in ['pending', 'revoked', 'expired']:
            grants = [{'caller': 'ag-a', 'target': 'mac', 'state': state, 'expires': now + 60}]
            self.assertEqual(decision('ag-a', 'mac', grants, now), 'UNMANAGED', state)
        stale = [{'caller': 'ag-a', 'target': 'mac', 'state': 'approved', 'expires': now - 1}]
        self.assertEqual(decision('ag-a', 'mac', stale, now), 'UNMANAGED')

    def test_lapsed_records_do_not_outlive_an_explicit_denial(self):
        now = time.time()
        # An explicit denial stands even next to a lapsed approval...
        mixed = [{'caller': 'ag-a', 'target': 'mac', 'state': 'denied', 'expires': 0},
                 {'caller': 'ag-a', 'target': 'mac', 'state': 'approved', 'expires': now - 5}]
        self.assertEqual(decision('ag-a', 'mac', mixed, now), 'DENIED')
        # ...but an active approval overrides it, and revoking the denial lifts it entirely.
        active = [{'caller': 'ag-a', 'target': 'mac', 'state': 'denied', 'expires': 0},
                  {'caller': 'ag-a', 'target': 'mac', 'state': 'approved', 'expires': now + 60}]
        self.assertEqual(decision('ag-a', 'mac', active, now), 'APPROVED')
        lifted = [{'caller': 'ag-a', 'target': 'mac', 'state': 'revoked', 'expires': 0}]
        self.assertEqual(decision('ag-a', 'mac', lifted, now), 'UNMANAGED')


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HistoryAccess(Path(self.tmp.name) / 'history-access.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_request_is_idempotent_and_decision_recorded(self):
        first = self.store.request('ag-a', 'mac')
        second = self.store.request('ag-a', 'mac')
        self.assertEqual(first['id'], second['id'])
        self.store.decide(first['id'], 'approved', 3600, 'pilot', 'admin')
        self.assertEqual(self.store.listing()[0]['state'], 'approved')
        grants = self.store.grants('ag-a', 'mac', time.time() + 100)
        self.assertEqual([g['state'] for g in grants], ['approved'])
        self.assertEqual(set(grants[0]), {'id', 'caller', 'target', 'state', 'expires', 'scope'})
        self.assertEqual(decision('ag-a', 'mac', grants), 'APPROVED')

    def test_scope_narrows_a_grant(self):
        now = time.time()
        broad = [{'caller': 'ag-a', 'target': 'mac', 'state': 'approved', 'expires': now + 60, 'scope': '*'}]
        narrow = [{'caller': 'ag-a', 'target': 'mac', 'state': 'approved', 'expires': now + 60, 'scope': 'gemma-lyrics'}]
        self.assertEqual(decision('ag-a', 'mac', broad, now, scope='anything'), 'APPROVED')
        self.assertEqual(decision('ag-a', 'mac', narrow, now, scope='gemma-lyrics'), 'APPROVED')
        self.assertEqual(decision('ag-a', 'mac', narrow, now, scope='qwen-mac'), 'DENIED')
        self.assertEqual(allowed_scopes(narrow, 'ag-a', 'mac', now), {'gemma-lyrics'})
        self.assertEqual(allowed_scopes(broad, 'ag-a', 'mac', now), {'*'})

    def test_scoped_filters_history_rows(self):
        rows = [{'name': 'gemma-lyrics', 'project_path': '/x/gemma-lyrics'}, {'name': 'qwen-mac', 'project_path': '/x/qwen-mac'}]
        self.assertEqual([r['name'] for r in scoped(rows, {'gemma'})], ['gemma-lyrics'])
        self.assertEqual(len(scoped(rows, {'*'})), 2)
        self.assertEqual(scoped(rows, {'nope'}), [])

    def test_scope_is_recorded_and_dispatched(self):
        row = self.store.request('ag-a', 'mac', scope='gemma-lyrics')
        self.store.decide(row['id'], 'approved', 3600, 'pilot', 'admin')
        grants = self.store.grants('ag-a', 'mac', time.time() + 100)
        self.assertEqual(grants[0]['scope'], 'gemma-lyrics')
        self.assertEqual(decision('ag-a', 'mac', grants, scope='gemma-lyrics'), 'APPROVED')
        self.assertEqual(decision('ag-a', 'mac', grants, scope='qwen-mac'), 'DENIED')

    def test_revoke_returns_the_pair_to_device_consent(self):
        row = self.store.request('ag-a', 'mac')
        self.store.decide(row['id'], 'approved', 3600, 'pilot', 'admin')
        self.store.decide(row['id'], 'revoked', 3600, 'incident', 'admin')
        grants = self.store.grants('ag-a', 'mac', time.time() + 100)
        # Revoking is the administrator withdrawing their involvement, not a permanent veto:
        # the device owner's own consent decides again.
        self.assertEqual(decision('ag-a', 'mac', grants), 'UNMANAGED')

    def test_expiry_returns_the_pair_to_device_consent(self):
        row = self.store.request('ag-a', 'mac')
        self.store.decide(row['id'], 'approved', 1, 'short window', 'admin')
        time.sleep(1.2)
        grants = self.store.grants('ag-a', 'mac', time.time() + 100)
        self.assertEqual(decision('ag-a', 'mac', grants), 'UNMANAGED')

    def test_explicit_denial_survives_and_can_be_lifted(self):
        row = self.store.request('ag-a', 'mac')
        self.store.decide(row['id'], 'denied', 86400, 'no history', 'admin')
        self.assertEqual(decision('ag-a', 'mac', self.store.grants('ag-a', 'mac', time.time() + 100)), 'DENIED')
        self.store.decide(row['id'], 'revoked', 1, 'owner may decide again', 'admin')
        self.assertEqual(decision('ag-a', 'mac', self.store.grants('ag-a', 'mac', time.time() + 100)), 'UNMANAGED')

    def test_handle_creates_then_decides_without_an_id(self):
        with patch('app.history_access.HistoryAccess', return_value=self.store):
            result = handle('admin', {'caller': 'ag-a', 'target': 'mac', 'decision': 'denied', 'seconds': 3600, 'reason': 'no history'})
        self.assertIn('updated', result)
        self.assertEqual(self.store.listing()[0]['state'], 'denied')

    def test_handle_rejects_invalid_fields(self):
        with patch('app.history_access.HistoryAccess', return_value=self.store):
            with self.assertRaises(ValueError):
                handle('admin', {'decision': 'approved'})


if __name__ == '__main__':
    unittest.main()
