import subprocess
from pathlib import Path
import shutil
import tempfile
import unittest

from scripts import ca_tool

ROOT = Path(__file__).resolve().parents[1]
CERT_DIR = ROOT / '.runtime'


def openssl(*args):
    return subprocess.run(['openssl', *map(str, args)], capture_output=True, text=True)


class CaToolTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.ca = Path(tempfile.mkdtemp(prefix='test-ca-', dir=CERT_DIR))
        self.addCleanup(shutil.rmtree, self.ca, ignore_errors=True)
        ca_tool.init_root(self.ca, days=3650)
        ca_tool.init_intermediate(self.ca, days=1825)
        ca_tool.issue(self.ca, 'client-a', 'client', 365, 'http://127.0.0.1:2560')
        ca_tool.issue(self.ca, 'client-b', 'client', 365, 'http://127.0.0.1:2560')
        ca_tool.issue(self.ca, 'poc-server', 'server', 365, 'http://127.0.0.1:2560')

    def intermediate(self):
        return self.ca / 'intermediate'

    def test_leaf_chains_to_offline_root_via_intermediate(self):
        result = openssl('verify', '-CAfile', self.ca / 'root/root.crt', '-untrusted', self.intermediate() / 'intermediate.crt',
                         self.intermediate() / 'certs/client-a.crt')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_server_san_and_extended_key_usage(self):
        text = openssl('x509', '-in', self.intermediate() / 'certs/poc-server.crt', '-noout', '-text').stdout
        self.assertIn('TLS Web Server Authentication', text)
        self.assertIn('DNS:localhost', text)

    def test_ocsp_aia_url_embedded(self):
        text = openssl('x509', '-in', self.intermediate() / 'certs/client-a.crt', '-noout', '-text').stdout
        self.assertIn('OCSP - URI:http://127.0.0.1:2560', text)

    def test_revoked_certificate_fails_crl_check_while_valid_passes(self):
        ca_tool.revoke(self.ca, 'client-b')
        ca_tool.crl(self.ca)
        crl = self.intermediate() / 'crl.pem'
        good = openssl('verify', '-crl_check', '-CAfile', self.ca / 'root/root.crt', '-untrusted',
                       self.intermediate() / 'intermediate.crt', '-CRLfile', crl, self.intermediate() / 'certs/client-a.crt')
        self.assertEqual(good.returncode, 0, good.stderr)
        bad = openssl('verify', '-crl_check', '-CAfile', self.ca / 'root/root.crt', '-untrusted',
                      self.intermediate() / 'intermediate.crt', '-CRLfile', crl, self.intermediate() / 'certs/client-b.crt')
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn('revoked', bad.stderr)
        crl_text = openssl('crl', '-in', crl, '-noout', '-text').stdout
        self.assertIn('Serial Number: 1001', crl_text)

    def test_renew_reuses_key_with_new_serial(self):
        cert = self.intermediate() / 'certs/client-a.crt'
        before = openssl('x509', '-in', cert, '-noout', '-serial').stdout
        pub_before = openssl('x509', '-in', cert, '-noout', '-pubkey').stdout
        ca_tool.renew(self.ca, 'client-a', 'client', self.intermediate() / 'private/client-a.key', 365, 'http://127.0.0.1:2560')
        after = openssl('x509', '-in', cert, '-noout', '-serial').stdout
        self.assertNotEqual(before, after)
        self.assertEqual(pub_before, openssl('x509', '-in', cert, '-noout', '-pubkey').stdout)
        result = openssl('verify', '-CAfile', self.ca / 'root/root.crt', '-untrusted', self.intermediate() / 'intermediate.crt', cert)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_status_and_refuse_overwrite(self):
        rows = ca_tool.status(self.ca)['issued']
        names = {row['name'] for row in rows}
        self.assertEqual(names, {'/CN=client-a', '/CN=client-b', '/CN=poc-server'})
        with self.assertRaises(ValueError):
            ca_tool.init_root(self.ca)
        with self.assertRaises(ValueError):
            ca_tool.issue(self.ca, 'client-a', 'client')


if __name__ == '__main__':
    unittest.main()
