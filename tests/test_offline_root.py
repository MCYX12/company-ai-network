import json
from pathlib import Path
import tempfile
import unittest

from scripts import ca_tool


class OfflineRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.ca = base / 'ca'
        self.offline = base / 'offline'
        ca_tool.init_root(self.ca, days=30)
        ca_tool.init_intermediate(self.ca, days=30)
        ca_tool.issue(self.ca, 'ocsp-signer', 'ocsp', 30, 'http://127.0.0.1:2560')
        ca_tool.issue(self.ca, 'client-a', 'client', 30, 'http://127.0.0.1:2560')

    def tearDown(self):
        self.tmp.cleanup()

    def _export(self):
        self.assertFalse(ca_tool.export_root(self.ca, self.offline)['applied'])
        report = ca_tool.export_root(self.ca, self.offline, apply=True)
        self.assertTrue(report['applied'])
        return report

    def test_export_moves_root_and_leaves_verifiable_marker(self):
        report = self._export()
        self.assertFalse((self.ca / 'root/root.key').exists())
        self.assertTrue((self.offline / 'root/root.key').exists())
        self.assertTrue((self.offline / 'root/root.crt').exists())
        marker = json.loads((self.ca / 'OFFLINE_ROOT.json').read_text())
        self.assertEqual(marker['root_cert_sha256'], report and json.loads((self.ca / 'OFFLINE_ROOT.json').read_text())['root_cert_sha256'])
        status = ca_tool.root_status(self.ca)
        self.assertFalse(status['local_root'])
        self.assertTrue(status['offline_root_reachable'])
        self.assertIn('OK', ca_tool.verify_chain(self.ca, 'client-a'))

    def test_issuance_revocation_and_crl_only_need_the_intermediate(self):
        self._export()
        ca_tool.issue(self.ca, 'client-b', 'client', 30, 'http://127.0.0.1:2560')
        self.assertIn('OK', ca_tool.verify_chain(self.ca, 'client-b'))
        ca_tool.revoke(self.ca, 'client-a')
        self.assertTrue(Path(ca_tool.crl(self.ca)['crl']).exists())
        states = {row['serial']: row['state'] for row in ca_tool.status(self.ca)['issued']}
        self.assertIn('R', states.values())

    def test_new_intermediate_can_be_signed_from_the_offline_root(self):
        self._export()
        second = Path(self.tmp.name) / 'ca2'
        ca_tool.init_intermediate(second, days=30, root_dir=self.offline / 'root')
        self.assertTrue((second / 'intermediate/intermediate.crt').exists())
        # the new intermediate chains to the offline root
        import subprocess
        result = subprocess.run(['openssl', 'verify', '-CAfile', str(self.offline / 'root/root.crt'),
                                 str(second / 'intermediate/intermediate.crt')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_root_is_reported_clearly(self):
        self._export()
        (self.offline / 'root/root.crt').unlink()
        with self.assertRaises(ValueError):
            ca_tool.verify_chain(self.ca, 'client-a')


if __name__ == '__main__':
    unittest.main()
