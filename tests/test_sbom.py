from pathlib import Path
import tempfile
import unittest

from scripts import sbom


class SbomTests(unittest.TestCase):
    def test_components_have_version_and_license(self):
        data = sbom.build()
        names = {row['name'].lower() for row in data['components']}
        self.assertTrue({'nats-py', 'jsonschema', 'cryptography'} <= names)
        for row in data['components']:
            self.assertTrue(row['version'])
            self.assertTrue(row['license'])

    def test_artifact_checksums_and_tamper(self):
        data = sbom.build()
        self.assertIsNotNone(data['artifacts']['pyproject.toml']['sha256'])
        self.assertIn('nats_server', data)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.runtime/bin').mkdir(parents=True)
            binary = root / '.runtime/bin/nats-server'
            binary.write_text('fake')
            fresh = sbom.build(root)
            self.assertTrue(sbom.verify(fresh, root))
            binary.write_text('tampered')
            self.assertFalse(sbom.verify(fresh, root))


if __name__ == '__main__':
    unittest.main()
