import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts import bootstrap
from scripts.renew_credentials import cert_not_after, check, discover_leaves, renew

ROOT = Path(__file__).resolve().parents[1]
CERT_DIR = ROOT / '.runtime'


def openssl(*args):
    return subprocess.check_output(['openssl', *map(str, args)], text=True, stderr=subprocess.STDOUT)


def subject(path):
    return openssl('x509', '-in', path, '-noout', '-subject').strip()


def pubkey(path):
    return openssl('x509', '-in', path, '-noout', '-pubkey')


def verify(ca, leaf, purpose):
    subprocess.run(['openssl', 'verify', '-x509_strict', '-CAfile', str(ca), '-purpose', purpose, str(leaf)],
                   check=True, capture_output=True)


class RenewCredentialTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.runtime = Path(tempfile.mkdtemp(prefix='test-renew-', dir=CERT_DIR))
        self.addCleanup(shutil.rmtree, self.runtime, ignore_errors=True)
        bootstrap.RUNTIME = self.runtime
        bootstrap.main()
        self.certs = self.runtime / 'certs'

    def test_resign_extends_expiry_and_preserves_ca_and_identities(self):
        before_ca = pubkey(self.certs / 'ca.crt')
        before = {name: (subject(self.certs / f'{name}.crt'), cert_not_after(self.certs / f'{name}.crt')[0])
                  for name in discover_leaves(self.certs)}
        report = renew(self.runtime, days=90, ca_mode='resign')
        self.assertTrue(report['applied'])
        self.assertFalse(report['ca']['public_key_changed'])
        self.assertEqual(pubkey(self.certs / 'ca.crt'), before_ca)
        for name, (old_subject, old_expiry) in before.items():
            self.assertEqual(subject(self.certs / f'{name}.crt'), old_subject)
            self.assertGreater(cert_not_after(self.certs / f'{name}.crt')[0], old_expiry)
            purpose = 'sslserver' if name == 'server' else 'sslclient'
            verify(self.certs / 'ca.crt', self.certs / f'{name}.crt', purpose)
        self.assertTrue(Path(report['backup']).is_dir())
        self.assertTrue((Path(report['backup']) / 'renewal.json').is_file())
        self.assertEqual((self.certs / 'ca.key').stat().st_mode & 0o777, 0o600)

    def test_rotate_replaces_ca_key_and_reissues_leaves(self):
        shutil.copy2(self.certs / 'ca.crt', self.certs / 'ca.old.crt')
        before_ca = pubkey(self.certs / 'ca.crt')
        report = renew(self.runtime, days=90, ca_mode='rotate')
        self.assertTrue(report['applied'])
        self.assertTrue(report['ca']['public_key_changed'])
        self.assertNotEqual(pubkey(self.certs / 'ca.crt'), before_ca)
        for name in discover_leaves(self.certs):
            purpose = 'sslserver' if name == 'server' else 'sslclient'
            verify(self.certs / 'ca.crt', self.certs / f'{name}.crt', purpose)
            with self.assertRaises(subprocess.CalledProcessError):
                verify(self.certs / 'ca.old.crt', self.certs / f'{name}.crt', purpose)

    def test_new_leaf_keys_changes_private_key(self):
        before = (self.certs / 'ag-a.key').read_bytes()
        renew(self.runtime, days=30, ca_mode='none', new_leaf_keys=True)
        self.assertNotEqual((self.certs / 'ag-a.key').read_bytes(), before)
        verify(self.certs / 'ca.crt', self.certs / 'ag-a.crt', 'sslclient')

    def test_dry_run_writes_nothing(self):
        before = {p.name: p.read_bytes() for p in self.certs.glob('*.crt')}
        report = renew(self.runtime, days=90, dry_run=True)
        self.assertFalse(report['applied'])
        self.assertIsNone(report['backup'])
        self.assertEqual({p.name: p.read_bytes() for p in self.certs.glob('*.crt')}, before)
        self.assertFalse((self.certs / 'history').exists())

    def test_ca_none_only_refreshes_leaves(self):
        before_ca = (self.certs / 'ca.crt').read_bytes()
        report = renew(self.runtime, days=90, ca_mode='none')
        self.assertFalse(report['ca']['resigned'])
        self.assertEqual((self.certs / 'ca.crt').read_bytes(), before_ca)

    def test_identity_filter_and_unknown_identity(self):
        before = cert_not_after(self.certs / 'ag-b.crt')[0]
        renew(self.runtime, days=90, identities=['ag-a'])
        self.assertGreater(cert_not_after(self.certs / 'ag-a.crt')[0], before)
        self.assertEqual(cert_not_after(self.certs / 'ag-b.crt')[0], before)
        with self.assertRaises(ValueError):
            renew(self.runtime, identities=['missing-agent'])

    def test_existing_server_ip_san_is_preserved(self):
        extension = self.certs / 'server.ext'
        extension.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:192.168.1.20\n')
        subprocess.run(['openssl', 'x509', '-req', '-in', str(self.certs / 'server.csr'), '-CA', str(self.certs / 'ca.crt'),
                        '-CAkey', str(self.certs / 'ca.key'), '-CAcreateserial', '-days', '7', '-extfile', str(extension),
                        '-out', str(self.certs / 'server.crt')], check=True, capture_output=True)
        renew(self.runtime, days=90)
        san = openssl('x509', '-in', self.certs / 'server.crt', '-noout', '-ext', 'subjectAltName')
        self.assertIn('192.168.1.20', san)
        verify(self.certs / 'ca.crt', self.certs / 'server.crt', 'sslserver')

    def test_check_reports_ca_and_expiring_leaves(self):
        rows = check(self.runtime, warn_days=7)
        names = {row['name'] for row in rows}
        self.assertIn('ca', names)
        self.assertIn('server', names)
        self.assertTrue(all(row['expiring'] for row in rows))
        renew(self.runtime, days=90)
        self.assertFalse(any(row['expiring'] for row in check(self.runtime, warn_days=7)))

    def test_enterprise_marker_blocks_the_self_signed_tool(self):
        (self.certs / 'ENTERPRISE_CA.json').write_text('{}')
        with self.assertRaises(ValueError):
            renew(self.runtime, days=90)

    def test_repeated_renewal_keeps_distinct_backups(self):
        first = renew(self.runtime, days=30)['backup']
        second = renew(self.runtime, days=60)['backup']
        self.assertNotEqual(first, second)
        self.assertTrue(Path(first).is_dir() and Path(second).is_dir())


if __name__ == '__main__':
    unittest.main()
