import json
from pathlib import Path
import tempfile
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from scripts import ca_tool
from scripts.request_certificate import generate

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


class TerminalCsrTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.work = Path(self.tmp.name)
        self.ca = self.work / 'enterprise-ca'
        ca_tool.init_root(self.ca, days=30)
        ca_tool.init_intermediate(self.ca, days=30)
        ca_tool.issue(self.ca, 'ocsp-signer', 'ocsp', 30, 'http://127.0.0.1:2560')

    def tearDown(self):
        self.tmp.cleanup()

    def test_terminal_key_and_csr_are_local(self):
        result = generate(self.work / 'terminal', 'laptop-01')
        key_text = Path(result['key']).read_text()
        csr_text = Path(result['csr']).read_text()
        self.assertIn('BEGIN PRIVATE KEY', key_text)
        self.assertIn('BEGIN CERTIFICATE REQUEST', csr_text)
        self.assertNotIn('PRIVATE KEY', csr_text)

    def test_sign_csr_rejects_mismatched_identity(self):
        generated = generate(self.work / 'terminal', 'laptop-01')
        with self.assertRaises(ValueError):
            ca_tool.sign_csr(self.ca, 'laptop-02', 'client', generated['csr'], days=30)
        self.assertFalse((self.ca / 'intermediate/certs/laptop-02.crt').exists())
        self.assertEqual(ca_tool.csr_subject_cn(generated['csr']), 'laptop-01')

    def test_sign_csr_returns_cert_matching_device_key(self):
        generated = generate(self.work / 'terminal', 'laptop-01')
        signed = ca_tool.sign_csr(self.ca, 'laptop-01', 'client', generated['csr'], days=30)
        self.assertNotIn('key', signed)
        key = serialization.load_pem_private_key(Path(generated['key']).read_bytes(), password=None)
        cert = x509.load_pem_x509_certificate(Path(signed['cert']).read_bytes())
        self.assertEqual(cert.public_key().public_numbers(), key.public_key().public_numbers())
        self.assertIn('OK', ca_tool.verify_chain(self.ca, 'laptop-01'))


if __name__ == '__main__':
    unittest.main()
