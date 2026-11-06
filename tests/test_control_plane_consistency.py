"""The HA authorization comparison must not treat missing data as agreement."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import authz, control_plane, iam


class ConsistencyTests(unittest.TestCase):
    def test_empty_runtimes_are_not_consistent(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            report = control_plane.compare([Path(a), Path(b)])
            self.assertFalse(report['consistent'])
            self.assertTrue(report['missing'])

    def test_ha_requires_a_shared_authority_runtime(self):
        with tempfile.TemporaryDirectory() as shared:
            missing = control_plane.ha_problems(shared, env={'POC_HA': '1'})
            self.assertTrue(any('POC_AUTHZ_RUNTIME' in problem for problem in missing))
            ok = control_plane.ha_problems(shared, env={'POC_HA': '1', 'POC_AUTHZ_RUNTIME': shared,
                                                        'POC_AUTHZ_SHARED': '1'})
            self.assertEqual(ok, [])
            bad = control_plane.ha_problems(shared, env={'POC_HA': '1', 'POC_AUTHZ_RUNTIME': shared + '/nope',
                                                         'POC_AUTHZ_SHARED': '1'})
            self.assertTrue(any('不存在' in problem for problem in bad))

    def test_authorization_stores_resolve_to_the_shared_runtime(self):
        with tempfile.TemporaryDirectory() as shared:
            with patch.dict(os.environ, {'POC_AUTHZ_RUNTIME': shared}):
                self.assertEqual(authz._base(None), Path(shared))
                self.assertEqual(iam.authority_base(), Path(shared))
                self.assertEqual(iam.RosterStore().path, Path(shared) / 'roster.sqlite3')

    def test_authorization_stores_resolve_to_the_shared_runtime(self):
        with tempfile.TemporaryDirectory() as shared:
            with patch.dict(os.environ, {'POC_AUTHZ_RUNTIME': shared}):
                from app import conversation_access, file_access, history_access, replay_store
                root = Path(shared)
                self.assertEqual(conversation_access.AccessStore().path, root / 'conversation-access.sqlite3')
                self.assertEqual(history_access.HistoryAccess().path, root / 'history-access.sqlite3')
                self.assertEqual(file_access.FileAccess().path, root / 'file-access.sqlite3')
                self.assertEqual(replay_store.ReplayStore().path, root / 'replay.sqlite3')

    def test_router_refuses_ha_without_a_shared_authority(self):
        from app import router as router_mod
        with patch.dict(os.environ, {'POC_HA': '1'}, clear=False):
            os.environ.pop('POC_AUTHZ_RUNTIME', None)
            with self.assertRaises(RuntimeError):
                router_mod.Router(None)

    def test_roster_divergence_is_detected(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for folder in (a, b):
                (Path(folder) / 'registry.json').write_text('{}')
            (Path(a) / 'roster.sqlite3').write_bytes(b'active')
            (Path(b) / 'roster.sqlite3').write_bytes(b'left')
            report = control_plane.compare([Path(a), Path(b)])
            self.assertIn('roster.sqlite3', report['divergent'])
            self.assertFalse(report['consistent'])


if __name__ == '__main__':
    unittest.main()
