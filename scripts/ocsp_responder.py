"""Loopback-only threaded OCSP responder with in-process signing.

The signing key and the CA index stay in memory: each request is answered by
cryptography directly (no per-request openssl fork), and the index is reloaded when
its mtime changes so revocation stays immediate. Falls back to an openssl subprocess
if anything about the in-memory path fails. Binds 127.0.0.1 only and supports the POST
form and the base64-in-path GET form used by Go/NATS.
"""
import argparse
import base64
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import urllib.parse

MAX_REQUEST = 65536
REASON = {'keyCompromise': 'key_compromise', 'caCompromise': 'ca_compromise', 'affiliationChanged': 'affiliation_changed',
          'superseded': 'superseded', 'cessationOfOperation': 'cessation_of_operation',
          'certificateHold': 'certificate_hold', 'privilegeWithdrawn': 'privilege_withdrawn', 'unspecified': 'unspecified'}
_REASON_ENUM = {}


def _reason_enum(name):
    if not name:
        return None
    if not _REASON_ENUM:
        from cryptography.x509 import ReasonFlags
        _REASON_ENUM.update({'key_compromise': ReasonFlags.key_compromise, 'ca_compromise': ReasonFlags.ca_compromise,
                             'affiliation_changed': ReasonFlags.affiliation_changed, 'superseded': ReasonFlags.superseded,
                             'cessation_of_operation': ReasonFlags.cessation_of_operation,
                             'certificate_hold': ReasonFlags.certificate_hold,
                             'privilege_withdrawn': ReasonFlags.privilege_withdrawn, 'unspecified': ReasonFlags.unspecified})
    return _REASON_ENUM.get(name)


def _parse_time(value):
    return datetime.strptime(value.strip(), '%y%m%d%H%M%SZ').replace(tzinfo=timezone.utc)


class OcspSigner:
    def __init__(self, ca_dir):
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        self.ca_dir = Path(ca_dir); self.intermediate = self.ca_dir / 'intermediate'
        self.signer_cert = x509.load_pem_x509_certificate((self.intermediate / 'certs/ocsp-signer.crt').read_bytes())
        self.signer_key = serialization.load_pem_private_key((self.intermediate / 'private/ocsp-signer.key').read_bytes(), password=None)
        self.index = self.intermediate / 'index.txt'
        self._mtime = None
        self._status = {}
        self.refresh()

    def refresh(self, force=False):
        try:
            mtime = self.index.stat().st_mtime_ns
        except OSError:
            return
        if not force and mtime == self._mtime:
            return
        status = {}
        for line in self.index.read_text(errors='replace').splitlines():
            fields = line.split('	')
            if len(fields) < 4:
                continue
            try:
                serial = int(fields[3], 16)
            except ValueError:
                continue
            if fields[0] == 'V':
                status[serial] = ('GOOD', None, None)
            elif fields[0] == 'R':
                info = fields[2].split(',')
                when = _parse_time(info[0]) if info and info[0] else None
                reason = REASON.get(info[1].strip()) if len(info) > 1 else None
                status[serial] = ('REVOKED', when, reason)
        self._status = status; self._mtime = mtime

    def _build(self, request):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509 import ocsp
        req = ocsp.load_der_ocsp_request(request)
        state = self._status.get(req.serial_number, ('UNKNOWN', None, None))
        now = datetime.now(timezone.utc)
        status = {'GOOD': ocsp.OCSPCertStatus.GOOD, 'REVOKED': ocsp.OCSPCertStatus.REVOKED, 'UNKNOWN': ocsp.OCSPCertStatus.UNKNOWN}[state[0]]
        builder = ocsp.OCSPResponseBuilder()
        builder = builder.add_response_by_hash(req.issuer_name_hash, req.issuer_key_hash, req.serial_number,
                                               req.hash_algorithm, status, now, now + timedelta(hours=1),
                                               state[1], _reason_enum(state[2]))
        builder = builder.responder_id(ocsp.OCSPResponderEncoding.HASH, self.signer_cert)
        builder = builder.certificates([self.signer_cert])
        return builder.sign(private_key=self.signer_key, algorithm=hashes.SHA256()).public_bytes(serialization.Encoding.DER)

    def sign(self, request, timeout=8.0):
        try:
            self.refresh()
            return self._build(request)
        except Exception:
            return sign_openssl(self.ca_dir, request, timeout)


def sign_openssl(ca_dir, request, timeout=8.0):
    ca = Path(ca_dir); intermediate = ca / 'intermediate'
    signer = intermediate / 'certs/ocsp-signer.crt'; key = intermediate / 'private/ocsp-signer.key'
    for path in [intermediate / 'index.txt', intermediate / 'intermediate.crt', signer, key]:
        if not path.exists():
            raise ValueError('OCSP material missing: ' + str(path))
    with tempfile.TemporaryDirectory(prefix='ocsp-') as folder:
        req = Path(folder) / 'req.der'; resp = Path(folder) / 'resp.der'
        req.write_bytes(request)
        subprocess.run(['openssl', 'ocsp', '-index', str(intermediate / 'index.txt'),
                        '-CA', str(intermediate / 'intermediate.crt'),
                        '-rsigner', str(signer), '-rkey', str(key),
                        '-reqin', str(req), '-respout', str(resp)],
                       check=True, capture_output=True, timeout=timeout)
        return resp.read_bytes()


class Handler(BaseHTTPRequestHandler):
    signer = None
    timeout = 8.0
    semaphore = threading.BoundedSemaphore(8)

    def _decode_get(self):
        encoded = urllib.parse.unquote(self.path[1:])
        if not encoded or len(encoded) > MAX_REQUEST * 2:
            raise ValueError('bad path')
        return base64.b64decode(encoded, validate=True)

    def _respond(self, request):
        if not request or len(request) > MAX_REQUEST:
            return self.send_error(400)
        try:
            with self.semaphore:
                response = self.signer.sign(request, self.timeout)
        except Exception:
            return self.send_error(503)
        self.send_response(200)
        self.send_header('Content-Type', 'application/ocsp-response')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            return self.send_error(400)
        if length <= 0 or length > MAX_REQUEST:
            return self.send_error(400)
        self._respond(self.rfile.read(length))

    def do_GET(self):
        try:
            request = self._decode_get()
        except Exception:
            return self.send_error(400)
        self._respond(request)

    def log_message(self, *args):
        pass


def serve(ca_dir, port=2560, bind='127.0.0.1', timeout=8.0, workers=8):
    Handler.signer = OcspSigner(ca_dir); Handler.timeout = timeout
    Handler.semaphore = threading.BoundedSemaphore(max(1, workers))
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ca-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=int(os.environ.get('OCSP_PORT', '2560')))
    parser.add_argument('--bind', default=os.environ.get('OCSP_BIND', '127.0.0.1'))
    parser.add_argument('--timeout', type=float, default=8.0)
    parser.add_argument('--workers', type=int, default=8)
    args = parser.parse_args()
    server = serve(args.ca_dir, args.port, args.bind, args.timeout, args.workers)
    print(f'OCSP responder on http://{args.bind}:{args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
