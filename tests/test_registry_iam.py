"""Registry admission must honor the IAM roster (manual offboard and OIDC sync)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import iam, registry


class RegistryIamTests(unittest.TestCase):
    def test_invalid_diagnostic_never_breaks_authoritative_admission(self):
        (self.runtime/'health').mkdir()
        file=self.runtime/'health/admission-problems.json'
        for value in (None,[],True,{'problems':[None]},{'problems':[]}):
            with self.subTest(value=value):
                file.write_text(json.dumps(value))
                self.assertTrue(registry.load_registry()['ag-a']['enabled'])
                snapshot=registry.admission_snapshot(self.runtime)
                self.assertEqual(snapshot['problems'],[])
                self.assertIsNotNone(snapshot['observed_at'])

    def test_invalid_diagnostic_reports_unknown_and_preserves_iam_fail_closed(self):
        (self.runtime/'health').mkdir()
        file=self.runtime/'health/admission-problems.json'
        for value in (None,[],{'observed_at':True,'problems':[]},
                      {'observed_at':1,'problems':[None]}):
            with self.subTest(value=value):
                file.write_text(json.dumps(value))
                self.assertEqual(registry.admission_snapshot(self.runtime)['snapshot_error'],
                                 'ADMISSION_SNAPSHOT_INVALID')
        (self.runtime/'roster.sqlite3').mkdir()
        self.assertFalse(registry.load_registry()['ag-a']['enabled'])
        self.assertEqual(registry.admission_snapshot(self.runtime)['problems'][0]['problem'],
                         'IAM_DEPENDENCY_UNAVAILABLE')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        (self.runtime / 'registry.json').write_text(json.dumps({'schema_version': 1, 'agents': [
            {'agent_id': 'ag-a', 'device_id': 'dev-a', 'employee_id': 'emp-7',
             'tool': 'fixture', 'projects': ['demo-project'], 'enabled': True}]}))
        self.env = patch.dict(os.environ, {'AGENT_REGISTRY': str(self.runtime / 'registry.json')})
        self.env.start()
        self.reg = patch('app.registry.RUNTIME', self.runtime); self.reg.start()
        self.iam = patch('app.iam.RUNTIME', self.runtime); self.iam.start()

    def tearDown(self):
        self.iam.stop(); self.reg.stop(); self.env.stop(); self.tmp.cleanup()

    def test_active_member_stays_enabled(self):
        iam.RosterStore(self.runtime / 'roster.sqlite3').set('emp-7', 'active', source='oidc', observed_at=1)
        self.assertTrue(registry.load_registry()['ag-a']['enabled'])

    def test_unreadable_authority_fails_closed(self):
        (self.runtime / 'roster.sqlite3').mkdir()      # sqlite cannot open a directory
        rows = registry.load_registry()
        self.assertFalse(rows['ag-a']['enabled'])
        self.assertEqual(rows['ag-a']['iam_problem'], 'IAM_DEPENDENCY_UNAVAILABLE')
        self.assertTrue(registry.iam_problems())

    def test_unreadable_authority_is_persisted_for_another_process(self):
        (self.runtime / 'roster.sqlite3').mkdir()
        registry.load_registry()
        snapshot = registry.admission_snapshot(self.runtime)
        self.assertTrue(snapshot['problems'])
        self.assertEqual(snapshot['problems'][0]['problem'], 'IAM_DEPENDENCY_UNAVAILABLE')
        self.assertIsNotNone(snapshot['observed_at'])
        (self.runtime / 'roster.sqlite3').rmdir()          # recovery clears the snapshot
        registry.load_registry()
        self.assertEqual(registry.admission_snapshot(self.runtime)['problems'], [])

    def test_strict_policy_blocks_unknown_identity(self):
        with patch.dict(os.environ, {'POC_IAM_STRICT': '1'}):
            rows = registry.load_registry()
        self.assertFalse(rows['ag-a']['enabled'])
        self.assertEqual(rows['ag-a']['iam_problem'], 'IAM_UNKNOWN_NOT_MANAGED')
        self.assertTrue(registry.iam_problems())

    def test_offboard_disables_agent_without_editing_registry(self):
        iam.RosterStore(self.runtime / 'roster.sqlite3').set('emp-7', 'left', source='offboard')
        self.assertFalse(registry.load_registry()['ag-a']['enabled'])


if __name__ == '__main__':
    unittest.main()
