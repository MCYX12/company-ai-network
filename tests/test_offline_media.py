import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts import ca_tool


class OfflineMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.ca = self.base / 'ca'
        ca_tool.init_root(self.ca, days=30)
        ca_tool.init_intermediate(self.ca, days=30)
        ca_tool.issue(self.ca, 'ocsp-signer', 'ocsp', 30, 'http://127.0.0.1:2560')

    def tearDown(self):
        self.tmp.cleanup()

    def _export(self, target):
        ca_tool.export_root(self.ca, target, apply=True)
        return target / 'root'

    def test_bundle_verify_and_tamper(self):
        root = self._export(self.base / 'offline')
        bundle = self.base / 'bundle.tar.gz'
        report = ca_tool.bundle_root(root, bundle)
        self.assertTrue(bundle.exists())
        self.assertGreater(report['files'], 0)
        verified = ca_tool.verify_bundle(bundle, self.ca / 'intermediate/intermediate.crt')
        self.assertEqual(verified['checksums'], 'ok')
        self.assertEqual(verified['intermediate'], 'ok')
        with tarfile.open(bundle, 'r:gz') as archive:
            names = archive.getnames()
        self.assertTrue(any(name.endswith('MANIFEST.json') for name in names))
        self.assertTrue(any(name.endswith('README.txt') for name in names))
        # tamper with a bundled file and re-pack; checksum verification must fail
        tampered = self.base / 'tampered.tar.gz'
        with tempfile.TemporaryDirectory() as folder:
            with tarfile.open(bundle, 'r:gz') as archive:
                archive.extractall(folder)
            target = next(Path(folder).rglob('root.crt'))
            target.write_bytes(target.read_bytes() + b'x')
            with tarfile.open(tampered, 'w:gz') as archive:
                archive.add(folder, arcname='.')
        with self.assertRaises(ValueError):
            ca_tool.verify_bundle(tampered)

    def test_audit_root_before_and_after_export(self):
        runtime = self.base / 'runtime'
        (runtime / 'certs').mkdir(parents=True)
        (runtime / 'certs/ENTERPRISE_CA.json').write_text(json.dumps({'ca_dir': str(self.ca)}))
        before = ca_tool.audit_root(runtime)
        self.assertIn('ROOT_KEY_PRESENT_ON_SERVER', before['findings'])
        self.assertEqual(before['status'], 'review')
        # move it outside the runtime
        ca_tool.export_root(self.ca, self.base / 'offline', apply=True)
        after = ca_tool.audit_root(runtime)
        self.assertEqual(after['status'], 'pass', after['findings'])
        self.assertTrue(after['offline_root_reachable'])

    def test_audit_flags_root_inside_runtime(self):
        runtime = self.base / 'runtime2'
        (runtime / 'certs').mkdir(parents=True)
        (runtime / 'certs/ENTERPRISE_CA.json').write_text(json.dumps({'ca_dir': str(self.ca)}))
        ca_tool.export_root(self.ca, runtime / 'offline', apply=True)
        audit = ca_tool.audit_root(runtime)
        self.assertIn('OFFLINE_ROOT_INSIDE_RUNTIME', audit['findings'])

    def test_bundle_refuses_to_overwrite(self):
        root = self._export(self.base / 'offline')
        bundle = self.base / 'bundle.tar.gz'
        ca_tool.bundle_root(root, bundle)
        with self.assertRaises(ValueError):
            ca_tool.bundle_root(root, bundle)


if __name__ == '__main__':
    unittest.main()
