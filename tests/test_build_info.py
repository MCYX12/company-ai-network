"""Build visibility: source revision, released version and running digest are distinct."""
import tempfile
import unittest
from pathlib import Path

from app import build_info
from app.worker_kernel import CONTRACT_VERSION


class BuildInfoTests(unittest.TestCase):
    def test_summary_exposes_all_three_versions(self):
        summary = build_info.summary()
        self.assertTrue(summary['running_digest'])
        self.assertEqual(summary['contract_version'], CONTRACT_VERSION)
        self.assertTrue(summary['authz_version'])
        self.assertNotEqual(summary['contract_version'], summary['released_version'])

    def test_running_digest_changes_with_the_running_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / 'app'
            app.mkdir()
            (app / 'x.py').write_text('x=1')
            first = build_info.running_digest(tmp)
            (app / 'x.py').write_text('x=2')
            self.assertNotEqual(first, build_info.running_digest(tmp))
            (app / 'y.py').write_text('y=1')
            self.assertNotEqual(first, build_info.running_digest(tmp))

    def test_absent_release_manifest_is_none_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(build_info.released_version(tmp))
            self.assertIn('source_revision', build_info.summary(tmp))


if __name__ == '__main__':
    unittest.main()
