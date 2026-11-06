import json
from pathlib import Path
import tempfile
import unittest

from scripts import offline_bundle
from scripts import sbom
import subprocess
import sys


class OfflineBundleTests(unittest.TestCase):
    def test_missing_binary_and_missing_or_malformed_hash_never_match(self):
        with tempfile.TemporaryDirectory() as folder:
            for data in ({}, [], {'format': True, 'nats_server': {'sha256': 'a'*64}},
                         {'format': 1, 'nats_server': None},
                         {'format': 1, 'nats_server': {'sha256': None}},
                         {'format': 1, 'nats_server': {'sha256': 'a'*64}}):
                with self.subTest(data=data):
                    self.assertFalse(offline_bundle.verify(data, Path(folder)))
                    self.assertFalse(sbom.verify(data, Path(folder)))

    def test_check_cli_fails_on_invalid_and_mismatched_artifact(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); path=root/'manifest.json'
            for module in ('scripts.offline_bundle', 'scripts.sbom'):
                for content in ('not json', '[]', '{}', json.dumps({'format':1,'nats_server':{'sha256':'a'*64}})):
                    path.write_text(content)
                    result=subprocess.run([sys.executable,'-m',module,'--root',folder,'--check',str(path)],capture_output=True,text=True)
                    self.assertEqual(result.returncode,1,result.stderr)
                    self.assertFalse(json.loads(result.stdout)['nats_server_ok'])

    def test_manifest_lists_pinned_components_and_gaps(self):
        data = offline_bundle.manifest()
        self.assertEqual(data['format'], 1)
        self.assertIn('nats-server', (data['nats_server']['version'] or ''))
        self.assertIn('nats-py', data['dependencies'])
        self.assertTrue(any('external model' in gap for gap in data['known_gaps']))
        self.assertTrue(data['must_be_available_offline'])

    def test_write_and_verify_roundtrip_and_tamper(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.runtime/bin').mkdir(parents=True)
            binary = root / '.runtime/bin/nats-server'
            binary.write_text('fake nats binary')
            data = offline_bundle.manifest(root)
            self.assertTrue(offline_bundle.verify(data, root))
            binary.write_text('tampered')
            self.assertFalse(offline_bundle.verify(data, root))


if __name__ == '__main__':
    unittest.main()
