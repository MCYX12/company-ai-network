"""OIDC/JWT adapter: RS256/ES256 round-trips and the rejections enterprises care about."""
import base64
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app import oidc


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def make_token(private, kid, alg, claims, signer):
    header = b64(json.dumps({'alg': alg, 'kid': kid, 'typ': 'JWT'}).encode())
    payload = b64(json.dumps(claims).encode())
    signing_input = (header + '.' + payload).encode()
    return header + '.' + payload + '.' + b64(signer(private, signing_input))


def rsa_signer(private, data):
    return private.sign(data, padding.PKCS1v15(), hashes.SHA256())


def ec_signer(private, data):
    r, s = decode_dss_signature(private.sign(data, ec.ECDSA(hashes.SHA256())))
    size = 32
    return r.to_bytes(size, 'big') + s.to_bytes(size, 'big')


class OidcTests(unittest.TestCase):
    def setUp(self):
        self.issuer = 'https://idp.example.com'
        self.audience = 'company-agent'
        self.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.rsa.public_key().public_numbers()
        self.jwks = {'keys': [{'kty': 'RSA', 'kid': 'r1', 'alg': 'RS256',
                               'n': oidc.b64url_uint(numbers.n), 'e': oidc.b64url_uint(numbers.e)}]}
        self.ec = ec.generate_private_key(ec.SECP256R1())
        ec_numbers = self.ec.public_key().public_numbers()
        self.jwks['keys'].append({'kty': 'EC', 'crv': 'P-256', 'kid': 'e1', 'alg': 'ES256',
                                  'x': oidc.b64url_uint(ec_numbers.x), 'y': oidc.b64url_uint(ec_numbers.y)})
        self.now = time.time()
        self.claims = {'iss': self.issuer, 'aud': self.audience, 'sub': 'emp-42',
                       'groups': ['platform'], 'employment_status': 'active',
                       'exp': self.now + 600, 'nbf': self.now - 10}

    def token(self, **overrides):
        claims = dict(self.claims); claims.update(overrides)
        return make_token(self.rsa, 'r1', 'RS256', claims, rsa_signer)

    def verify(self, token, **kw):
        return oidc.verify_jwt(token, self.jwks, self.issuer, self.audience, now=self.now, **kw)

    def test_rs256_and_es256_round_trip(self):
        self.assertEqual(self.verify(self.token())['sub'], 'emp-42')
        es = make_token(self.ec, 'e1', 'ES256', self.claims, ec_signer)
        self.assertEqual(self.verify(es)['sub'], 'emp-42')

    def test_identity_mapping(self):
        identity = oidc.identity_from_claims(self.verify(self.token()))
        self.assertEqual(identity, {'employee_id': 'emp-42', 'groups': ['platform'],
                                    'employment_status': 'active'})

    def test_rejections(self):
        for bad in (self.token(iss='https://evil.example'), self.token(aud='other'),
                    self.token(exp=self.now - 3600), self.token(nbf=self.now + 3600)):
            with self.assertRaises(oidc.TokenError):
                self.verify(bad)
        none_token = make_token(self.rsa, 'r1', 'none', self.claims, rsa_signer)
        with self.assertRaises(oidc.TokenError):
            self.verify(none_token)
        tampered = self.token()[:-3] + 'AAA'
        with self.assertRaises(oidc.TokenError):
            self.verify(tampered)
        with self.assertRaises(oidc.TokenError):
            self.verify('not.a.jwt.at.all')


class OidcIamIntegrationTests(OidcTests):
    def test_verified_token_drives_employment_status(self):
        import tempfile
        from pathlib import Path as _Path
        from app import iam
        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = rsa_key.public_key().public_numbers()
        jwks = {'keys': [{'kty': 'RSA', 'kid': 'r1', 'alg': 'RS256',
                          'n': oidc.b64url_uint(numbers.n), 'e': oidc.b64url_uint(numbers.e)}]}
        now = time.time()
        claims = {'iss': 'https://idp.example', 'aud': 'company-agent', 'sub': 'emp-7',
                  'groups': ['platform'], 'employment_status': 'left',
                  'exp': now + 600, 'nbf': now - 5}
        token = make_token(rsa_key, 'r1', 'RS256', claims, rsa_signer)
        with tempfile.TemporaryDirectory() as folder:
            jwks_path = _Path(folder) / 'jwks.json'
            jwks_path.write_text(json.dumps(jwks))
            roster = iam.RosterStore(_Path(folder) / 'roster.sqlite3')
            provider = iam.OidcProvider(jwks_path=str(jwks_path), issuer='https://idp.example',
                                        audience='company-agent', roster=roster)
            result = provider.sync(token, now=now)
            self.assertEqual(result['employee_id'], 'emp-7')
            self.assertEqual(provider.status('emp-7'), 'left')
            self.assertIn(provider.status('emp-7'), iam.BLOCKED)
            self.assertNotIn(provider.status('emp-7'), iam.ACTIVE)

    def _provider(self, folder, roster):
        import json as _json
        from pathlib import Path as _Path
        from app import iam
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = key.public_key().public_numbers()
        jwks = {'keys': [{'kty': 'RSA', 'kid': 'r1', 'alg': 'RS256',
                          'n': oidc.b64url_uint(numbers.n), 'e': oidc.b64url_uint(numbers.e)}]}
        jwks_path = _Path(folder) / 'jwks.json'
        jwks_path.write_text(_json.dumps(jwks))
        return key, iam.OidcProvider(jwks_path=str(jwks_path), issuer='https://idp.example',
                                     audience='company-agent', roster=roster), _Path(folder)

    def test_manual_offboard_is_not_overwritten_and_stale_token_is_rejected(self):
        import tempfile
        from app import iam
        now = time.time()
        with tempfile.TemporaryDirectory() as folder:
            roster = iam.RosterStore(Path(folder) / 'roster.sqlite3')
            roster.set('emp-7', 'left', source='offboard')
            key, provider, _ = self._provider(folder, roster)
            claims = {'iss': 'https://idp.example', 'aud': 'company-agent', 'sub': 'emp-7',
                      'employment_status': 'active', 'iat': now + 10, 'exp': now + 600}
            token = make_token(key, 'r1', 'RS256', claims, rsa_signer)
            result = provider.sync(token, now=now)
            self.assertFalse(result['synced'])                     # manual revocation wins
            self.assertEqual(provider.status('emp-7'), 'left')
            # a genuine leave sync is allowed to agree, an older active event is not
            leave = {'iss': 'https://idp.example', 'aud': 'company-agent', 'sub': 'emp-7',
                     'employment_status': 'active', 'iat': now - 3600, 'exp': now + 600}
            self.assertFalse(provider.sync(make_token(key, 'r1', 'RS256', leave, rsa_signer), now=now)['synced'])

    def test_offboard_survives_same_status_and_active_sync(self):
        import tempfile
        from app import iam
        now = time.time()
        with tempfile.TemporaryDirectory() as folder:
            roster = iam.RosterStore(Path(folder) / 'roster.sqlite3')
            roster.set('emp-7', 'left', source='offboard')
            key, provider, _ = self._provider(folder, roster)
            for status in ('left', 'active'):
                claims = {'iss': 'https://idp.example', 'aud': 'company-agent', 'sub': 'emp-7',
                          'employment_status': status, 'iat': now + 10, 'exp': now + 600}
                result = provider.sync(make_token(key, 'r1', 'RS256', claims, rsa_signer), now=now)
                self.assertFalse(result['synced'])
                self.assertEqual(result['effective_status'], 'left')   # actual state, not requested
            self.assertEqual(provider.status('emp-7'), 'left')
            self.assertEqual(roster._row('emp-7')['source'], 'offboard')
            self.assertTrue(roster.set('emp-7', 'left', source='offboard'))   # explicit re-offboard ok

    def test_standard_token_without_status_is_not_persisted(self):
        import tempfile
        from app import iam
        now = time.time()
        with tempfile.TemporaryDirectory() as folder:
            roster = iam.RosterStore(Path(folder) / 'roster.sqlite3')
            key, provider, _ = self._provider(folder, roster)
            claims = {'iss': 'https://idp.example', 'aud': 'company-agent', 'sub': 'emp-9',
                      'iat': now, 'exp': now + 600}
            result = provider.sync(make_token(key, 'r1', 'RS256', claims, rsa_signer), now=now)
            self.assertEqual(result['status'], 'unknown')
            self.assertFalse(result['synced'])
            self.assertEqual(provider.status('emp-9'), 'unknown')

    def test_jwt_strictness_rejections(self):
        now = time.time()
        base_claims = {'iss': self.issuer, 'aud': self.audience, 'sub': 'emp-42',
                       'exp': now + 600, 'iat': now - 5}
        bad_kid = make_token(self.rsa, 'unknown', 'RS256', base_claims, rsa_signer)
        with self.assertRaises(oidc.TokenError):
            self.verify(bad_kid)
        enc_jwks = {'keys': [{'kty': 'RSA', 'kid': 'r1', 'use': 'enc',
                              'n': self.jwks['keys'][0]['n'], 'e': self.jwks['keys'][0]['e']}]}
        with self.assertRaises(oidc.TokenError):
            oidc.verify_jwt(self.token(), enc_jwks, self.issuer, self.audience, now=self.now)
        alg_jwks = {'keys': [{'kty': 'RSA', 'kid': 'r1', 'alg': 'RS512',
                              'n': self.jwks['keys'][0]['n'], 'e': self.jwks['keys'][0]['e']}]}
        with self.assertRaises(oidc.TokenError):
            oidc.verify_jwt(self.token(), alg_jwks, self.issuer, self.audience, now=self.now)
        nan_payload = make_token(self.rsa, 'r1', 'RS256', dict(base_claims, exp=float('nan')), rsa_signer)
        with self.assertRaises(oidc.TokenError):
            self.verify(nan_payload)
        str_nbf = make_token(self.rsa, 'r1', 'RS256', dict(base_claims, nbf=str(now + 9999)), rsa_signer)
        with self.assertRaises(oidc.TokenError):
            self.verify(str_nbf)


class OidcRotationTests(OidcTests):
    def _serve(self, initial):
        holder = {'jwks': initial}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(holder['jwks']).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, holder

    @staticmethod
    def _jwks_for(key, kid, alg='RS256'):
        numbers = key.public_key().public_numbers()
        return {'keys': [{'kty': 'RSA', 'kid': kid, 'alg': alg,
                          'n': oidc.b64url_uint(numbers.n), 'e': oidc.b64url_uint(numbers.e)}]}

    def test_jwks_url_rotation_and_subject_mapping(self):
        import tempfile
        from app import iam
        key1 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        server, holder = self._serve(self._jwks_for(key1, 'k1'))
        try:
            now = time.time()
            with tempfile.TemporaryDirectory() as folder:
                roster = iam.RosterStore(Path(folder) / 'roster.sqlite3')
                provider = iam.OidcProvider(jwks_url='http://127.0.0.1:%d/jwks' % server.server_port,
                                            issuer=self.issuer, audience=self.audience,
                                            subject_map={'%s|sub-9' % self.issuer: 'emp-42'}, roster=roster)
                active = {'iss': self.issuer, 'aud': self.audience, 'sub': 'sub-9',
                          'employment_status': 'active', 'iat': now, 'exp': now + 600}
                self.assertTrue(provider.sync(make_token(key1, 'k1', 'RS256', active, rsa_signer), now=now)['synced'])
                self.assertEqual(provider.status('emp-42'), 'active')
                # the IdP rotates to k2; a new-kid token triggers one refetch
                holder['jwks'] = self._jwks_for(key2, 'k2')
                left = dict(active, employment_status='left', iat=now + 1)
                self.assertEqual(provider.sync(make_token(key2, 'k2', 'RS256', left, rsa_signer), now=now + 1)['status'], 'left')
                self.assertEqual(provider.status('emp-42'), 'left')
                stranger = dict(active, sub='stranger')
                with self.assertRaises(ValueError):
                    provider.sync(make_token(key2, 'k2', 'RS256', stranger, rsa_signer), now=now + 2)
        finally:
            server.shutdown(); server.server_close()


if __name__ == '__main__':
    unittest.main()
