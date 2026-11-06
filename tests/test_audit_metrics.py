import json
from pathlib import Path
import tempfile
import unittest

from scripts import audit_chain, metrics_export


class AuditChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'channel.jsonl'
        self.path.write_text('\n'.join(json.dumps({'event': name, 'seq': index}) for index, name in enumerate(['a', 'b', 'c'])) + '\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_seal_verify_and_detect_tampering(self):
        sealed = audit_chain.seal(self.path)
        self.assertEqual(sealed['records'], 3)
        self.assertTrue(audit_chain.verify(self.path)['ok'])
        lines = self.path.read_text().splitlines()
        lines[1] = json.dumps({'event': 'tampered', 'seq': 1})
        self.path.write_text('\n'.join(lines) + '\n')
        self.assertFalse(audit_chain.verify(self.path)['ok'])

    def test_detect_deletion(self):
        audit_chain.seal(self.path)
        lines = self.path.read_text().splitlines()
        self.path.write_text('\n'.join([lines[0], lines[2]]) + '\n')
        self.assertFalse(audit_chain.verify(self.path)['ok'])


class MetricsExportTests(unittest.TestCase):
    def test_render_exports_status_and_numeric_leaves(self):
        text = metrics_export.render({'status': 'ok', 'identities_online': 5, 'certs': {'min_days': 20}, 'online': ['a', 'b']})
        self.assertIn('company_agent_health_status 1', text)
        self.assertIn('company_agent_health_identities_online 5', text)
        self.assertIn('company_agent_health_certs_min_days 20', text)
        self.assertIn('company_agent_health_online_count 2', text)

    def test_non_ok_status_is_zero(self):
        self.assertIn('company_agent_health_status 0', metrics_export.render({'status': 'failed'}))


if __name__ == '__main__':
    unittest.main()
