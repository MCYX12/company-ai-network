"""OIDC sync runner: honest accounting (no_input/empty/applied/skipped/verified)."""
import base64
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app import oidc
from scripts import oidc_sync_runner

ISS = 'https://idp.example'
AUD = 'company-agent'


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _token(key, claims):
    header = _b64(json.dumps({'alg': 'RS256', 'kid': 'k1'}).encode())
    payload = _b64(json.dumps(claims).encode())
    signing = (header + '.' + payload).encode()
    return header + '.' + payload + '.' + _b64(key.sign(signing, padding.PKCS1v15(), hashes.SHA256()))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.key.public_key().public_numbers()
        jwks = {'keys': [{'kty': 'RSA', 'kid': 'k1', 'alg': 'RS256',
                          'n': oidc.b64url_uint(numbers.n), 'e': oidc.b64url_uint(numbers.e)}]}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(jwks).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        os.environ.update(POC_OIDC_ISSUER=ISS, POC_OIDC_AUDIENCE=AUD,
                          POC_OIDC_JWKS_URL='http://127.0.0.1:%d/jwks' % self.server.server_port)
        self.now = time.time()

    def tearDown(self):
        for name in ('POC_OIDC_ISSUER', 'POC_OIDC_AUDIENCE', 'POC_OIDC_JWKS_URL'):
            os.environ.pop(name, None)
        self.server.shutdown(); self.server.server_close()

    def _claims(self, status=None, iat=None):
        claims = {'iss': ISS, 'aud': AUD, 'sub': 'sub-9', 'iat': iat or self.now, 'exp': self.now + 600}
        if status:
            claims['employment_status'] = status
        return claims

    def test_missing_directory_fails_and_does_not_refresh_success(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / 'runtime'
            snapshot = oidc_sync_runner.run(runtime, Path(folder) / 'nope', apply=True, now=self.now)
            self.assertEqual(snapshot['status'], 'no_input')
            self.assertIsNone(snapshot['last_success'])
            self.assertIsNotNone(snapshot['input_error'])

    def test_empty_directory_does_not_refresh_success(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            tokens = folder / 'tokens'; tokens.mkdir()
            snapshot = oidc_sync_runner.run(folder / 'runtime', tokens, apply=True, now=self.now)
            self.assertEqual(snapshot['status'], 'empty')
            self.assertIsNone(snapshot['last_success'])

    def test_no_status_claim_is_skipped_not_synced(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            tokens = folder / 'tokens'; tokens.mkdir()
            (tokens / 'a.jwt').write_text(_token(self.key, self._claims()))
            snapshot = oidc_sync_runner.run(folder / 'runtime', tokens, apply=True, now=self.now)
            self.assertEqual(snapshot['applied'], 0)
            self.assertEqual(snapshot['skipped'], 1)
            self.assertEqual(snapshot['status'], 'skipped')
            self.assertIsNone(snapshot['last_success'])
            self.assertEqual(snapshot['results'][0]['reason'], 'NO_EMPLOYMENT_STATUS')

    def test_apply_sets_last_success_and_verify_uses_last_verify(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            tokens = folder / 'tokens'; tokens.mkdir()
            (tokens / 'a.jwt').write_text(_token(self.key, self._claims('active')))
            applied = oidc_sync_runner.run(folder / 'runtime', tokens, apply=True, now=self.now)
            self.assertEqual(applied['applied'], 1)
            self.assertEqual(applied['status'], 'ok')
            self.assertIsNotNone(applied['last_success'])
            verify = oidc_sync_runner.run(folder / 'runtime', tokens, apply=False, now=self.now)
            self.assertEqual(verify['verified'], 1)
            self.assertEqual(verify['applied'], 0)
            self.assertIsNotNone(verify['last_verify'])
            self.assertEqual(verify['last_success'], applied['last_success'])


if __name__ == '__main__':
    unittest.main()
