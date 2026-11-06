import unittest

from scripts import release_manifest


class ReleaseManifestTests(unittest.TestCase):
    def test_manifest_has_version_components_and_levels(self):
        data = release_manifest.build()
        self.assertEqual(data['product'], 'company-agent-platform')
        self.assertTrue(data['version'])
        self.assertEqual(data['baseline'], 'docs/BASELINE.md')
        self.assertIn('nats-server', (data['nats_server'].get('version') or ''))
        self.assertIn('nats-py', data['dependencies'])
        self.assertEqual(data['evidence_levels'], ['设计', '已实现', '隔离验证', '实机验证', '现场验收'])


if __name__ == '__main__':
    unittest.main()
