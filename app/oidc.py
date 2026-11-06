"""Enterprise OIDC/JWT identity adapter (IdP-agnostic).

Works with any OIDC provider (Azure AD/Entra, Okta, Keycloak, ...): the caller supplies a JWKS
(document or URL-fetched dict), the expected issuer and audience. Verifies RS256/ES256 only,
rejects 'none' and unknown algorithms, and checks issuer/audience/expiry/not-before with leeway.
No network is required for verification when a JWKS document is supplied.
"""
import base64
import json
import math
import time
import urllib.request
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

SUPPORTED_ALGS = ('RS256', 'ES256')


class TokenError(ValueError):
    pass


def b64url_decode(segment):
    padding_needed = '=' * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding_needed)
    except (ValueError, TypeError) as error:
        raise TokenError('malformed base64url segment') from error


def b64url_uint(value):
    raw = value.to_bytes((value.bit_length() + 7) // 8, 'big')
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def jwk_to_public_key(jwk):
    kty = jwk.get('kty')
    if kty == 'RSA':
        n = int.from_bytes(b64url_decode(jwk['n']), 'big')
        e = int.from_bytes(b64url_decode(jwk['e']), 'big')
        return rsa.RSAPublicNumbers(e, n).public_key()
    if kty == 'EC' and jwk.get('crv') in ('P-256', 'P-384'):
        curve = ec.SECP256R1() if jwk['crv'] == 'P-256' else ec.SECP384R1()
        x = int.from_bytes(b64url_decode(jwk['x']), 'big')
        y = int.from_bytes(b64url_decode(jwk['y']), 'big')
        return ec.EllipticCurvePublicNumbers(x, y, curve).public_key()
    raise TokenError('unsupported JWK kty/crv: %r/%r' % (kty, jwk.get('crv')))


def select_jwk(jwks, kid, alg):
    """Exact kid match; a present-but-unknown kid is NEVER satisfied by a lone key."""
    keys = (jwks or {}).get('keys') or []
    if kid:
        matches = [jwk for jwk in keys if jwk.get('kid') == kid]
        if not matches:
            raise TokenError('no matching JWK for kid=%r' % (kid,))
        jwk = matches[0]
    elif len(keys) == 1:
        jwk = keys[0]
    else:
        raise TokenError('kid required when JWKS has %d keys' % len(keys))
    if jwk.get('use') not in (None, 'sig'):
        raise TokenError('JWK use=%r is not for signatures' % (jwk.get('use'),))
    if jwk.get('alg') and jwk['alg'] != alg:
        raise TokenError('JWK alg=%r does not match token alg=%r' % (jwk.get('alg'), alg))
    return jwk


def _verify_signature(jwk, alg, signing_input, signature):
    key = jwk_to_public_key(jwk)
    if alg == 'RS256':
        key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        return
    if alg == 'ES256':
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise TokenError('ES256 requires a P-256 key')
        if len(signature) != 64:
            raise TokenError('ES256 signature must be 64 bytes')
        der = encode_dss_signature(int.from_bytes(signature[:32], 'big'),
                                   int.from_bytes(signature[32:], 'big'))
        key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
        return
    raise TokenError('unsupported alg: %r' % (alg,))


def numeric_time(value, name):
    """Reject bool, non-numbers, NaN/inf and numeric strings for JWT time claims."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TokenError('invalid %s: %r' % (name, value))
    return float(value)


def verify_jwt(token, jwks, issuer, audience, now=None, leeway=60):
    now = time.time() if now is None else now
    if not isinstance(token, str) or token.count('.') != 2:
        raise TokenError('not a compact JWT')
    header_segment, payload_segment, signature_segment = token.split('.')
    try:
        header = json.loads(b64url_decode(header_segment))
        claims = json.loads(b64url_decode(payload_segment))
    except ValueError as error:
        raise TokenError('malformed JWT JSON') from error
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise TokenError('JWT header/claims must be JSON objects')
    alg = header.get('alg')
    if alg not in SUPPORTED_ALGS:
        raise TokenError('refusing alg: %r' % (alg,))
    jwk = select_jwk(jwks, header.get('kid'), alg)
    signing_input = (header_segment + '.' + payload_segment).encode()
    try:
        _verify_signature(jwk, alg, signing_input, b64url_decode(signature_segment))
    except InvalidSignature as error:
        raise TokenError('signature verification failed') from error
    if claims.get('iss') != issuer:
        raise TokenError('issuer mismatch')
    aud = claims.get('aud')
    audiences = aud if isinstance(aud, list) else [aud]
    if audience not in audiences:
        raise TokenError('audience mismatch')
    if 'exp' not in claims:
        raise TokenError('missing exp')
    if numeric_time(claims['exp'], 'exp') <= now - leeway:
        raise TokenError('token expired')
    if 'nbf' in claims and numeric_time(claims['nbf'], 'nbf') > now + leeway:
        raise TokenError('token not yet valid')
    if 'iat' in claims and numeric_time(claims['iat'], 'iat') > now + leeway:
        raise TokenError('token issued in the future')
    return claims


def identity_from_claims(claims, employee_claim='sub', status_claim='employment_status'):
    employee_id = claims.get(employee_claim)
    if not employee_id:
        raise TokenError('claim %r missing' % (employee_claim,))
    return {'employee_id': employee_id,
            'groups': claims.get('groups') or [],
            'employment_status': claims.get(status_claim) or 'unknown'}


def fetch_jwks(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode())
