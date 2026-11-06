import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import bootstrap, ca_tool, set_endpoint

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


class SetEndpointTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.work = Path(self.tmp.name)
        self.runtime = self.work / 'runtime'
        bootstrap.RUNTIME = self.runtime
        bootstrap.main()
        self.ca = self.runtime / 'enterprise-ca'
        ca_tool.init_root(self.ca, days=30)
        ca_tool.init_intermediate(self.ca, days=30)
        ca_tool.issue(self.ca, 'ocsp-signer', 'ocsp', 30, 'http://127.0.0.1:2560')
        (self.runtime / 'certs/ENTERPRISE_CA.json').write_text(json.dumps({'format': 1, 'ca_dir': str(self.ca)}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_san_for_ip_and_dns(self):
        self.assertIn('IP:10.8.0.5', set_endpoint.san_for('10.8.0.5'))
        self.assertIn('DNS:vpn.example.com', set_endpoint.san_for('vpn.example.com'))

    def test_dry_run_changes_nothing(self):
        before = (self.runtime / 'endpoint.txt').read_text() if (self.runtime / 'endpoint.txt').exists() else None
        report = set_endpoint.set_endpoint(self.runtime, '10.8.0.5', dry_run=True)
        self.assertTrue(report['dry_run'])
        after = (self.runtime / 'endpoint.txt').read_text() if (self.runtime / 'endpoint.txt').exists() else None
        self.assertEqual(before, after)

    def test_switch_to_vpn_ip_reissues_cert_and_validates(self):
        report = set_endpoint.set_endpoint(self.runtime, '10.8.0.5', port=14222, listen='0.0.0.0:14222')
        self.assertEqual((self.runtime / 'endpoint.txt').read_text(), 'tls://10.8.0.5:14222\n')
        self.assertIn('re-issued', report['server_cert'])
        self.assertTrue(report['nats_conf_valid'])
        chain = (self.runtime / 'certs/server.crt').read_text()
        self.assertEqual(chain.count('BEGIN CERTIFICATE'), 2)
        text = subprocess.run(['openssl', 'x509', '-in', str(self.runtime / 'certs/server.crt'), '-noout', '-text'],
                              capture_output=True, text=True).stdout
        self.assertIn('IP Address:10.8.0.5', text)

    def test_keep_sans_preserves_lan_address_when_adding_vpn(self):
        set_endpoint.set_endpoint(self.runtime, '10.8.0.5')
        report = set_endpoint.set_endpoint(self.runtime, '10.0.0.60')
        text = subprocess.run(['openssl', 'x509', '-in', str(self.runtime / 'certs/server.crt'), '-noout', '-text'],
                              capture_output=True, text=True).stdout
        self.assertIn('IP Address:10.8.0.5', text)
        self.assertIn('IP Address:10.0.0.60', text)
        self.assertIn('IP:10.8.0.5', report['san'])

    def test_dns_name_san(self):
        report = set_endpoint.set_endpoint(self.runtime, 'vpn.example.com')
        text = subprocess.run(['openssl', 'x509', '-in', str(self.runtime / 'certs/server.crt'), '-noout', '-text'],
                              capture_output=True, text=True).stdout
        self.assertIn('DNS:vpn.example.com', text)
        self.assertEqual(report['endpoint'], 'tls://vpn.example.com:14222')


if __name__ == '__main__':
    unittest.main()
