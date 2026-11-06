"""Authorization control-plane authority: digest, HA declaration, and divergence detection."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import control_plane


def seed(runtime, grants='[]', registry='{}'):
    """Create the complete authority file set; missing files are now a gating failure."""
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    for name in control_plane.STORE_FILES:
        (runtime / name).write_bytes(b'')
    (runtime / 'registry.json').write_text(registry)
    (runtime / 'conversation-access.sqlite3').write_bytes(grants.encode())
    return runtime


class DigestTests(unittest.TestCase):
    def test_digest_is_stable_and_changes_with_the_authorization_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = seed(Path(tmp) / 'r')
            first = control_plane.authz_digest(runtime)
            self.assertEqual(first, control_plane.authz_digest(runtime))
            (runtime / 'conversation-access.sqlite3').write_bytes(b'[{"state":"revoked"}]')
            self.assertNotEqual(first, control_plane.authz_digest(runtime))

    def test_missing_files_are_marked_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(control_plane.file_digest(Path(tmp) / 'nope.sqlite3'), '<missing>')

    def test_ha_requires_an_explicit_shared_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(control_plane.ha_problems(tmp, env={}), [])
            problems = control_plane.ha_problems(tmp, env={'POC_HA': '1'})
            self.assertEqual(len(problems), 2)       # missing shared runtime AND missing declaration
            self.assertTrue(any('POC_AUTHZ_RUNTIME' in p for p in problems))
            self.assertTrue(any('POC_AUTHZ_SHARED' in p for p in problems))
            self.assertEqual(len(control_plane.ha_problems(
                tmp, env={'POC_HA': '1', 'POC_AUTHZ_SHARED': '1'})), 1)   # still no shared runtime
            self.assertEqual(control_plane.ha_problems(
                tmp, env={'POC_HA': '1', 'POC_AUTHZ_SHARED': '1', 'POC_AUTHZ_RUNTIME': tmp}), [])

    def test_compare_detects_divergent_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            left = seed(Path(tmp) / 'left', grants='[{"state":"approved"}]')
            right = seed(Path(tmp) / 'right', grants='[{"state":"approved"}]')
            self.assertTrue(control_plane.compare([left, right])['consistent'])
            (right / 'conversation-access.sqlite3').write_bytes(b'[{"state":"revoked"}]')
            report = control_plane.compare([left, right])
            self.assertFalse(report['consistent'])
            self.assertIn('conversation-access.sqlite3', report['divergent'])


class HealthCheckTests(unittest.TestCase):
    """HA without a declared shared authority is visible in the health report."""

    def _stage(self, runtime):
        from app.healthcheck import local_checks
        return next(c for c in local_checks(runtime) if c['check'] == 'authz_consistency')

    def test_single_router_is_ok_without_ha(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop('POC_HA', None)
                os.environ.pop('POC_AUTHZ_SHARED', None)
                stage = self._stage(tmp)
        self.assertEqual(stage['status'], 'ok')
        self.assertFalse(stage['detail']['shared_declared'])

    def test_ha_without_shared_authority_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'POC_HA': '1', 'POC_AUTHZ_RUNTIME': tmp}, clear=False):
                os.environ.pop('POC_AUTHZ_SHARED', None)
                stage = self._stage(tmp)
        self.assertEqual(stage['status'], 'warn')
        self.assertIn('POC_AUTHZ_SHARED', stage['detail']['problems'][0])

    def test_ha_with_shared_authority_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'POC_HA': '1', 'POC_AUTHZ_SHARED': '1',
                                          'POC_AUTHZ_RUNTIME': tmp}, clear=False):
                stage = self._stage(tmp)
        self.assertEqual(stage['status'], 'ok')
        self.assertTrue(stage['detail']['shared_declared'])


if __name__ == '__main__':
    unittest.main()
