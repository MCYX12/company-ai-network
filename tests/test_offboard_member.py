"""Offboarding: no lost update, employee-scoped on shared devices, staged failures reported."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import offboard_member
from app.iam import RosterStore

REGISTRY = {'schema_version': 1, 'agents': [
    {'agent_id': 'ag-a', 'device_id': 'dev-x', 'employee_id': 'emp-a', 'tool': 'fixture',
     'projects': ['demo-project'], 'enabled': True},
    {'agent_id': 'ag-b', 'device_id': 'dev-x', 'employee_id': 'emp-b', 'tool': 'fixture',
     'projects': ['demo-project'], 'enabled': True}]}
CONF = {'accounts': {'POC': {'users': [{'user': 'ag-a'}, {'user': 'ag-b'}]}}}


class OffboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        (self.runtime / 'registry.json').write_text(json.dumps(REGISTRY))
        (self.runtime / 'nats.conf').write_text(json.dumps(CONF))

    def tearDown(self):
        self.tmp.cleanup()

    def _offboard(self, employee, apply=True, ca=None):
        with patch('scripts.offboard_member.subprocess.run') as run:
            run.return_value = SimpleNamespace(returncode=0)
            if ca is not None:
                with patch('scripts.ca_tool.revoke', side_effect=ca), \
                        patch('scripts.ca_tool.crl', side_effect=ca):
                    return offboard_member.offboard(self.runtime, employee, 'test', apply)
            return offboard_member.offboard(self.runtime, employee, 'test', apply)

    def _enabled(self):
        return {row['agent_id']: row['enabled'] for row in json.loads((self.runtime / 'registry.json').read_text())['agents']}

    def test_shared_device_only_offboards_this_employee(self):
        report = self._offboard('emp-a')
        self.assertEqual(report['identities'], ['ag-a'])
        self.assertEqual(self._enabled(), {'ag-a': False, 'ag-b': True})
        users = [u['user'] for u in json.loads((self.runtime / 'nats.conf').read_text())['accounts']['POC']['users']]
        self.assertEqual(users, ['ag-b'])

    def test_no_lost_update_when_two_offboards_interleave(self):
        dry = self._offboard('emp-a', apply=False)          # A plans first
        self.assertEqual(dry['stages'], {'plan': 'ok'})
        self._offboard('emp-b')                              # B applies
        self._offboard('emp-a')                              # A applies with a fresh read
        self.assertEqual(self._enabled(), {'ag-a': False, 'ag-b': False})

    def _with_ca(self, revoke, crl=None):
        (self.runtime / 'certs').mkdir(exist_ok=True)
        (self.runtime / 'certs/ENTERPRISE_CA.json').write_text(json.dumps({'ca_dir': str(self.runtime / 'ca')}))
        with patch('scripts.offboard_member.subprocess.run') as run, \
                patch('scripts.ca_tool.revoke', side_effect=revoke), \
                patch('scripts.ca_tool.crl', side_effect=(crl or revoke)):
            run.return_value = SimpleNamespace(returncode=0)
            return offboard_member.offboard(self.runtime, 'emp-a', 'test', True)

    def test_real_ca_error_is_caught_and_blocking_happens_first(self):
        def boom(*a, **k):
            raise subprocess.CalledProcessError(1, ['openssl', 'ca'], stderr='some openssl failure')
        report = self._with_ca(boom)
        self.assertTrue(report['applied'])
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['stages']['certificate'], 'failed')
        self.assertEqual(report['stages']['roster'], 'ok')
        self.assertEqual(self._enabled(), {'ag-a': False, 'ag-b': True})   # blocked before CA
        self.assertEqual(RosterStore(self.runtime / 'roster.sqlite3').status('emp-a'), 'left')

    def test_already_revoked_is_achieved_and_pending_activation(self):
        def already(*a, **k):
            raise subprocess.CalledProcessError(1, ['openssl', 'ca'], stderr='certificate already revoked')

        def noop(*a, **k):
            return None
        report = self._with_ca(already, noop)
        self.assertEqual(report['status'], 'pending_transport_activation')
        self.assertEqual(report['stages']['certificate'], 'ok')
        self.assertEqual(report['revoked_certificates'][0]['outcome'], 'already_revoked')
        self.assertTrue(report['transport_activation_required'])

    def test_genuine_failure_with_revoked_in_path_is_not_already(self):
        from unittest.mock import MagicMock
        ca = MagicMock()
        ca.status.return_value = {'issued': [{'name': 'other', 'state': 'V'}]}
        ca.revoke.side_effect = subprocess.CalledProcessError(
            1, ['openssl', 'ca', '-config', '/tmp/revoked-ca/openssl.cnf'], stderr='unable to load CA private key')
        with self.assertRaises(subprocess.CalledProcessError):
            offboard_member._revoke(ca, self.runtime / 'ca', 'ag-a')

    def test_already_revoked_uses_the_ca_database(self):
        from unittest.mock import MagicMock
        ca = MagicMock()
        # authentic index subject format; the CURRENT cert serial must be confirmed revoked
        ca.status.return_value = {'issued': [{'name': '/CN=ag-a', 'state': 'R', 'serial': '1000'}]}
        with patch('scripts.offboard_member._certificate_serial', return_value=0x1000):
            self.assertEqual(offboard_member._revoke(ca, self.runtime / 'ca', 'ag-a')['outcome'], 'already_revoked')
        ca.revoke.assert_not_called()
        ca.revoke_serial.assert_not_called()

    def test_same_subject_old_row_without_current_serial_is_inconsistent(self):
        from unittest.mock import MagicMock
        ca = MagicMock()
        ca.status.return_value = {'issued': [{'name': '/CN=ag-a', 'state': 'R', 'serial': '0999'}]}
        with patch('scripts.offboard_member._certificate_serial', return_value=0x1000):
            with self.assertRaises(ValueError):
                offboard_member._revoke(ca, self.runtime / 'ca', 'ag-a')

    def test_activation_marks_signaled_and_complete(self):
        with patch('scripts.offboard_member.subprocess.run') as run, \
                patch('scripts.offboard_member.activate_transport', return_value=1234):
            run.return_value = SimpleNamespace(returncode=0)
            report = offboard_member.offboard(self.runtime, 'emp-a', 'test', True, True)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['stages']['transport'], 'signaled')
        self.assertFalse(report['transport_activation_required'])

    def test_ca_failure_is_reported_as_partial(self):
        (self.runtime / 'certs').mkdir()
        (self.runtime / 'certs/ENTERPRISE_CA.json').write_text(json.dumps({'ca_dir': str(self.runtime / 'ca')}))
        report = self._offboard('emp-a', ca=OSError('CA unavailable'))
        self.assertTrue(report['applied'])
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['stages']['certificate'], 'failed')
        self.assertEqual(report['stages']['crl'], 'failed')
        self.assertIn('certificate', report['failed_stages'])
        self.assertEqual(report['revoked_certificates'], [])


if __name__ == '__main__':
    unittest.main()
