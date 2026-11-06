"""Member activation is dual-role by default, caller-only on request."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app.member_admin as member_admin


def body(askable=None):
    value = {'op': 'activate', 'id': 'member-x'}
    if askable is not None:
        value['askable'] = askable
    return value


class ActivationTests(unittest.TestCase):
    def activate(self, askable):
        calls = {}
        store = MagicMock()
        store.listing.return_value = {'members': [{'id': 'member-x', 'state': 'pending', 'projects': ['p']}]}
        def enroll(runtime, row, client_only=False):
            calls['row'] = row
            calls['client_only'] = client_only
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(member_admin, 'RUNTIME', Path(tmp)), \
             patch.object(member_admin, 'Accounts', return_value=store), \
             patch('app.registry.load_registry', return_value={}), \
             patch('scripts.enroll_agent.enroll', side_effect=enroll), \
             patch('app.member_admin.subprocess.run'):
            member_admin.handle('admin', body(askable))
        return calls

    def test_default_activation_is_dual_role(self):
        calls = self.activate(None)
        self.assertFalse(calls['client_only'])
        self.assertEqual(calls['row']['capabilities'], ['get_status', 'ask_peer'])

    def test_askable_false_keeps_caller_only(self):
        calls = self.activate(False)
        self.assertTrue(calls['client_only'])
        self.assertNotIn('capabilities', calls['row'])

    def test_unexpected_field_rejected(self):
        with self.assertRaises(ValueError):
            member_admin.handle('admin', {'op': 'activate', 'id': 'member-x', 'admin': True})


if __name__ == '__main__':
    unittest.main()
