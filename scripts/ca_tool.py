"""Two-tier PoC certificate authority: offline root + issuing intermediate + OCSP.

Layout under --ca-dir:
  root/          offline root key/cert (only signs the intermediate)
  intermediate/  issuing CA used for leaves, openssl CA database, CRL, OCSP signer

Leaves carry an OCSP AIA URL so a NATS server with tls.ocsp_peer can reject revoked
certificates at the TLS handshake. This is a PoC CA (no HSM); production must keep
the root key offline and review policy.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OCSP_URL = 'http://127.0.0.1:2560'


def run(*args):
    return subprocess.run(['openssl', *map(str, args)], check=True, capture_output=True, text=True)


_CA_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def _ca_lock(ca_dir):
    """One (reentrant per thread) lock shared by issue/renew/revoke/revoke_serial/crl.

    It keeps a single index read-modify-write atomic and lets a caller hold it for a whole
    transaction (e.g. offboarding every serial and regenerating the CRL) while the inner
    ca_tool calls re-enter without deadlocking.
    """
    key = os.path.realpath(str(ca_dir))
    ident = threading.get_ident()
    with _LOCKS_GUARD:
        entry = _CA_LOCKS.get(key)
        reentrant = bool(entry and entry['owner'] == ident)
        if reentrant:
            entry['depth'] += 1
    if reentrant:
        try:
            yield
        finally:
            with _LOCKS_GUARD:
                current = _CA_LOCKS.get(key)
                if current:
                    current['depth'] -= 1
        return
    intermediate = Path(ca_dir) / 'intermediate'
    intermediate.mkdir(parents=True, exist_ok=True)
    lock = intermediate / '.ca.lock'
    with lock.open('a') as handle:
        try:
            os.chmod(lock, 0o600)
        except OSError:
            pass
        fcntl.flock(handle, fcntl.LOCK_EX)
        with _LOCKS_GUARD:
            _CA_LOCKS[key] = {'owner': ident, 'depth': 1}
        try:
            yield
        finally:
            with _LOCKS_GUARD:
                _CA_LOCKS.pop(key, None)
            fcntl.flock(handle, fcntl.LOCK_UN)


def _locked(function):
    @functools.wraps(function)
    def wrapper(ca_dir, *args, **kwargs):
        with _ca_lock(ca_dir):
            return function(ca_dir, *args, **kwargs)
    return wrapper


def crl_serials(ca_dir):
    """Serial numbers present in the current CRL (empty when the CRL is missing/broken)."""
    path = Path(ca_dir) / 'intermediate' / 'crl.pem'
    try:
        from cryptography import x509
        crl = x509.load_pem_x509_crl(path.read_bytes())
        return {entry.serial_number for entry in crl}
    except (OSError, ValueError):
        return set()


def csr_subject_cn(csr_path):
    """Common Name of a CSR subject, or None when the CSR is unreadable.

    The signed certificate inherits the CSR subject, so the approved device
    identity must be compared with what the requester actually put in the CSR.
    """
    result = subprocess.run(['openssl', 'req', '-in', str(csr_path), '-noout',
                             '-subject', '-nameopt', 'RFC2253'],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    match = re.search(r'CN\s*=\s*([^,\n]+)', result.stdout)
    return match.group(1).strip() if match else None


def _atomic_write(path, text):
    """Write atomically: a failure leaves the original file byte-for-byte intact.

    An in-place write_text truncates first, so a kernel write failure (e.g. EFBIG) can leave a
    partial, unparsable CA index. Write a sibling temp file, fsync, then replace.
    """
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if Path(name).exists():
            Path(name).unlink()


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chmod(path, 0o600)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _root_cert(ca_dir, root_ca=None):
    """Locate the root certificate: explicit path, local root/, else the offline-root marker."""
    if root_ca:
        return Path(root_ca)
    ca = Path(ca_dir)
    local = ca / 'root/root.crt'
    if local.exists():
        return local
    marker = ca / 'OFFLINE_ROOT.json'
    if marker.exists():
        candidate = Path(json.loads(marker.read_text())['offline_root_dir']) / 'root.crt'
        if candidate.exists():
            return candidate
    raise ValueError('Root certificate unavailable; keep the offline-root directory reachable for verification only')


def export_root(ca_dir, output, apply=False):
    """Move the root key/cert out of the issuing host to an offline directory and leave a marker."""
    ca = Path(ca_dir); root = ca / 'root'; intermediate = ca / 'intermediate'; out = Path(output)
    if not (root / 'root.key').exists() or not (root / 'root.crt').exists():
        raise ValueError('Root material missing')
    run('verify', '-CAfile', root / 'root.crt', intermediate / 'intermediate.crt')
    report = {'ca_dir': str(ca), 'output': str(out), 'applied': False,
              'note': 'Move the exported directory to offline media / the administrator computer; the issuing host must not keep it.'}
    if not apply:
        return report
    if (out / 'root').exists():
        raise ValueError('Destination already exists; refusing to overwrite')
    out.mkdir(parents=True, exist_ok=True); os.chmod(out, 0o700)
    shutil.move(str(root), str(out / 'root'))
    dest = out / 'root'; os.chmod(dest, 0o700)
    for item in dest.rglob('*'):
        os.chmod(item, 0o600)
    run('verify', '-CAfile', dest / 'root.crt', intermediate / 'intermediate.crt')
    marker = {'format': 1, 'offline_root_dir': str(dest), 'root_cert_sha256': _sha(dest / 'root.crt'),
              'exported_at': datetime.now(timezone.utc).isoformat(),
              'note': 'Root key moved off the issuing host. Signing a new intermediate needs this directory (admin machine).'}
    _write(ca / 'OFFLINE_ROOT.json', json.dumps(marker, ensure_ascii=False, indent=2))
    report.update(applied=True, offline_root=str(dest), marker=str(ca / 'OFFLINE_ROOT.json'))
    return report


def _root_dir(path):
    """Accept either the directory containing root.key/root.crt or its parent."""
    p = Path(path)
    if (p / 'root.key').exists():
        return p
    if (p / 'root/root.key').exists():
        return p / 'root'
    raise ValueError('Root key/cert not found under ' + str(p))


def bundle_root(offline_root, output):
    """Pack the offline root for removable media: root key/cert + checksum manifest + README."""
    root = _root_dir(offline_root); output = Path(output)
    if output.exists():
        raise ValueError('Output already exists; refusing to overwrite')
    with tempfile.TemporaryDirectory(prefix='root-bundle-') as folder:
        staging = Path(folder) / 'offline-root'; staging.mkdir()
        shutil.copytree(root, staging / 'root')
        for item in (staging / 'root').rglob('*'):
            os.chmod(item, 0o600)
        files = {str(p.relative_to(staging)): _sha(p) for p in sorted((staging / 'root').rglob('*')) if p.is_file()}
        manifest = {'format': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'root_cert_sha256': _sha(root / 'root.crt'), 'files': files,
                    'signer': 'file' if not os.environ.get('POC_HSM_PKCS11') else 'hsm-configured',
                    'note': 'Offline root for the issuing CA. Store on removable media or an HSM-backed machine; never copy to the issuing host.'}
        _write(staging / 'MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        (staging / 'README.txt').write_text(
            'Offline root bundle\n\n'
            '1) Keep this file offline (removable media / administrator machine).\n'
            '2) To sign a new issuing intermediate:\n'
            '   tar -xzf <this file> -C <workdir>\n'
            '   ca_tool init-intermediate --ca-dir <new-ca> --root-dir <workdir>/offline-root/root\n'
            '3) Verify with: ca_tool verify-bundle --bundle <this file> --intermediate <new-ca>/intermediate/intermediate.crt\n\n'
            'HSM: this PoC signs with a key file. Production should expose the root through a PKCS#11/HSM\n'
            'signer and never place the key on a general-purpose host.\n')
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, 'w:gz') as archive:
            archive.add(staging, arcname='.')
    os.chmod(output, 0o600)
    return {'bundle': str(output), 'files': len(files), 'root_cert_sha256': _sha(root / 'root.crt'), 'bytes': output.stat().st_size}


def verify_bundle(bundle, intermediate=None):
    bundle = Path(bundle)
    if not bundle.is_file():
        raise ValueError('Bundle not found')
    with tempfile.TemporaryDirectory(prefix='root-verify-') as folder:
        with tarfile.open(bundle, 'r:gz') as archive:
            archive.extractall(folder)
        staging = Path(folder)
        manifest_path = staging / 'MANIFEST.json'
        if not manifest_path.exists():
            raise ValueError('MANIFEST.json missing')
        manifest = json.loads(manifest_path.read_text())
        bad = []
        for relative, digest in manifest['files'].items():
            target = staging / relative
            if not target.is_file() or _sha(target) != digest:
                bad.append(relative)
        report = {'files': len(manifest['files']), 'checksums': 'ok' if not bad else 'mismatch', 'bad': bad,
                  'root_cert_sha256': manifest.get('root_cert_sha256'), 'signer': manifest.get('signer', 'file')}
        if bad:
            raise ValueError('Bundle checksum mismatch: ' + ', '.join(bad))
        if intermediate:
            roots = list(staging.rglob('root.crt'))
            if not roots:
                raise ValueError('Bundled root certificate not found')
            result = subprocess.run(['openssl', 'verify', '-CAfile', str(roots[0]), str(intermediate)],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                raise ValueError('Intermediate does not verify against the bundled root: ' + result.stderr.strip())
            report['intermediate'] = 'ok'
        return report


def audit_root(runtime):
    """Server-side audit: the issuing host must not hold the root key and must point at an offline root."""
    runtime = Path(runtime); ca_dir = _ca_dir_for(runtime)
    info = root_status(ca_dir)
    marker = info.get('marker') or {}
    offline = Path(marker['offline_root_dir']) if marker.get('offline_root_dir') else None
    findings = []
    if info['local_root']:
        findings.append('ROOT_KEY_PRESENT_ON_SERVER')
    if not marker:
        findings.append('OFFLINE_ROOT_MARKER_MISSING')
    if offline is not None and (offline == runtime or runtime in offline.parents):
        findings.append('OFFLINE_ROOT_INSIDE_RUNTIME')
    strays = [str(p) for p in runtime.rglob('root.key') if p.is_file()]
    if strays:
        findings.append('ROOT_KEY_FILE_FOUND')
    return {'status': 'pass' if not findings else 'review', 'findings': findings, 'ca_dir': str(ca_dir),
            'offline_root': str(offline) if offline else None,
            'offline_root_reachable': bool(offline and (offline / 'root.crt').exists()),
            'stray_root_keys': strays, 'signer': 'hsm-configured' if os.environ.get('POC_HSM_PKCS11') else 'file'}


def _ca_dir_for(runtime):
    marker = runtime / 'certs/ENTERPRISE_CA.json'
    if marker.exists():
        return Path(json.loads(marker.read_text())['ca_dir'])
    return runtime / 'enterprise-ca'


def root_status(ca_dir):
    ca = Path(ca_dir); marker = ca / 'OFFLINE_ROOT.json'
    info = {'local_root': (ca / 'root/root.key').exists()}
    if marker.exists():
        data = json.loads(marker.read_text()); info['marker'] = data
        info['offline_root_reachable'] = (Path(data['offline_root_dir']) / 'root.crt').exists()
    return info


def _ca_config(intermediate, days):
    return f'''[ca]
default_ca = CA_default
[CA_default]
dir = {intermediate}
database = $dir/index.txt
new_certs_dir = $dir/newcerts
certificate = $dir/intermediate.crt
private_key = $dir/intermediate.key
serial = $dir/serial
crlnumber = $dir/crlnumber
default_md = sha256
default_days = {days}
default_crl_days = 30
policy = policy_any
copy_extensions = none
unique_subject = no
[policy_any]
commonName = supplied
'''


def _ensure_intermediate_state(intermediate, days):
    (intermediate / 'newcerts').mkdir(parents=True, exist_ok=True)
    (intermediate / 'index.txt').touch()
    _write(intermediate / 'index.txt.attr', 'unique_subject = no\n')
    if not (intermediate / 'serial').exists():
        _write(intermediate / 'serial', '1000\n')
    if not (intermediate / 'crlnumber').exists():
        _write(intermediate / 'crlnumber', '1000\n')
    _write(intermediate / 'openssl.cnf', _ca_config(intermediate, days))


def _extensions(kind, name, ocsp_url, server_san):
    lines = ['[v3]', 'basicConstraints=critical,CA:FALSE']
    if kind == 'server':
        lines += ['keyUsage=critical,digitalSignature,keyEncipherment', 'extendedKeyUsage=serverAuth',
                  f'subjectAltName={server_san}']
    elif kind == 'ocsp':
        lines += ['keyUsage=critical,digitalSignature', 'extendedKeyUsage=OCSPSigning', f'subjectAltName=DNS:{name}']
    else:
        lines += ['keyUsage=critical,digitalSignature,keyEncipherment', 'extendedKeyUsage=clientAuth',
                  f'subjectAltName=DNS:{name}']
    lines.append(f'authorityInfoAccess=OCSP;URI:{ocsp_url}')
    return '\n'.join(lines) + '\n'


def init_root(ca_dir, days=3650, subject='/CN=PoC-Offline-Root-CA'):
    root = Path(ca_dir) / 'root'; root.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    if (root / 'root.key').exists():
        raise ValueError('Root key already exists; refusing to overwrite')
    run('req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', days, '-keyout', root / 'root.key',
        '-out', root / 'root.crt', '-subj', subject,
        '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:1', '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
        '-addext', 'subjectKeyIdentifier=hash')
    return {'root_cert': str(root / 'root.crt'), 'days': days}


def init_intermediate(ca_dir, days=1825, subject='/CN=PoC-Issuing-Intermediate-CA', root_dir=None):
    ca = Path(ca_dir); root = Path(root_dir) if root_dir else ca / 'root'; intermediate = ca / 'intermediate'; intermediate.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    if (intermediate / 'intermediate.key').exists():
        raise ValueError('Intermediate key already exists; refusing to overwrite')
    if not (root / 'root.crt').exists():
        raise ValueError('Root CA missing; run init-root first')
    run('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', intermediate / 'intermediate.key',
        '-out', intermediate / 'intermediate.csr', '-subj', subject)
    _write(intermediate / 'intermediate.ext',
           'basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n')
    run('x509', '-req', '-in', intermediate / 'intermediate.csr', '-CA', root / 'root.crt', '-CAkey', root / 'root.key',
        '-CAcreateserial', '-days', days, '-extfile', intermediate / 'intermediate.ext',
        '-out', intermediate / 'intermediate.crt')
    (intermediate / 'chain.crt').write_bytes((intermediate / 'intermediate.crt').read_bytes() + (root / 'root.crt').read_bytes())
    _ensure_intermediate_state(intermediate, days)
    return {'intermediate_cert': str(intermediate / 'intermediate.crt'), 'chain': str(intermediate / 'chain.crt')}


@_locked
def issue(ca_dir, name, kind='client', days=365, ocsp_url=DEFAULT_OCSP_URL, server_san='DNS:localhost,IP:127.0.0.1'):
    ca = Path(ca_dir); intermediate = ca / 'intermediate'
    if kind not in {'client', 'server', 'ocsp'}:
        raise ValueError('kind must be client, server or ocsp')
    if not (intermediate / 'intermediate.crt').exists():
        raise ValueError('Intermediate CA missing; run init-intermediate first')
    _ensure_intermediate_state(intermediate, days)
    key = intermediate / 'private' / f'{name}.key'; csr = intermediate / 'csr' / f'{name}.csr'
    key.parent.mkdir(parents=True, exist_ok=True); csr.parent.mkdir(parents=True, exist_ok=True)
    if key.exists():
        raise ValueError(f'Key for {name} already exists')
    run('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', csr, '-subj', f'/CN={name}')
    ext = intermediate / 'ext' / f'{name}.ext'; _write(ext, _extensions(kind, name, ocsp_url, server_san))
    cert = intermediate / 'certs' / f'{name}.crt'; cert.parent.mkdir(parents=True, exist_ok=True)
    run('ca', '-config', intermediate / 'openssl.cnf', '-batch', '-notext', '-extfile', ext, '-extensions', 'v3',
        '-days', days, '-in', csr, '-out', cert)
    os.chmod(cert, 0o644)
    return {'cert': str(cert), 'key': str(key), 'chain': str(intermediate / 'chain.crt')}


@_locked
def renew(ca_dir, name, kind, key_file, days=365, ocsp_url=DEFAULT_OCSP_URL, server_san='DNS:localhost,IP:127.0.0.1', authorize=None):
    """Issue a fresh certificate for an existing private key (new serial; old cert stays valid until revoked)."""
    ca = Path(ca_dir); intermediate = ca / 'intermediate'
    if kind not in {'client', 'server', 'ocsp'}:
        raise ValueError('kind must be client, server or ocsp')
    if not (intermediate / 'intermediate.crt').exists():
        raise ValueError('Intermediate CA missing')
    if not Path(key_file).is_file():
        raise ValueError('Existing key not found')
    _ensure_intermediate_state(intermediate, days)
    if authorize is not None and not authorize(name):
        # Re-check employment status AFTER acquiring the CA lock: a renewal computed before an
        # offboard must not issue a fresh certificate for a now-left identity.
        raise ValueError('RENEWAL_DENIED: %s is no longer active' % name)
    csr = intermediate / 'csr' / f'{name}.renew.csr'; csr.parent.mkdir(parents=True, exist_ok=True)
    run('req', '-new', '-key', key_file, '-out', csr, '-subj', f'/CN={name}')
    ext = intermediate / 'ext' / f'{name}.ext'; _write(ext, _extensions(kind, name, ocsp_url, server_san))
    cert = intermediate / 'certs' / f'{name}.crt'
    run('ca', '-config', intermediate / 'openssl.cnf', '-batch', '-notext', '-extfile', ext, '-extensions', 'v3',
        '-days', days, '-in', csr, '-out', cert)
    os.chmod(cert, 0o644)
    return {'cert': str(cert), 'key': str(key_file), 'chain': str(intermediate / 'chain.crt')}


@_locked
def sign_csr(ca_dir, name, kind, csr_path, days=365, ocsp_url=DEFAULT_OCSP_URL,
             server_san='DNS:localhost,IP:127.0.0.1'):
    """Sign a terminal-provided CSR; the private key never reaches the server."""
    ca = Path(ca_dir); intermediate = ca / 'intermediate'
    if kind not in {'client', 'server', 'ocsp'}:
        raise ValueError('kind must be client, server or ocsp')
    if not (intermediate / 'intermediate.crt').exists():
        raise ValueError('Intermediate CA missing')
    csr_path = Path(csr_path)
    if not csr_path.is_file():
        raise ValueError('CSR not found')
    subject_cn = csr_subject_cn(csr_path)
    if subject_cn is None:
        raise ValueError('Unreadable CSR subject')
    if subject_cn != name:
        raise ValueError("CSR subject CN %r does not match approved identity %r" % (subject_cn, name))
    _ensure_intermediate_state(intermediate, days)
    staged = intermediate / 'csr' / f'{name}.external.csr'
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(csr_path.read_bytes())
    ext = intermediate / 'ext' / f'{name}.ext'; _write(ext, _extensions(kind, name, ocsp_url, server_san))
    cert = intermediate / 'certs' / f'{name}.crt'
    run('ca', '-config', intermediate / 'openssl.cnf', '-batch', '-notext', '-extfile', ext, '-extensions', 'v3',
        '-days', days, '-in', staged, '-out', cert)
    os.chmod(cert, 0o644)
    return {'cert': str(cert), 'chain': str(intermediate / 'chain.crt')}


@_locked
def revoke(ca_dir, name, reason='keyCompromise'):
    ca = Path(ca_dir); intermediate = ca / 'intermediate'
    cert = intermediate / 'certs' / f'{name}.crt'
    if not cert.exists():
        raise ValueError(f'No certificate for {name}')
    run('ca', '-config', intermediate / 'openssl.cnf', '-batch', '-revoke', cert, '-crl_reason', reason)
    return {'revoked': name, 'reason': reason}


@_locked
def revoke_serial(ca_dir, serial, reason='cessationOfOperation'):
    """Revoke a certificate by serial directly in the CA index.

    A renewal overwrites certs/<name>.crt, so the historical certificate file no longer exists;
    marking its index row revoked is how openssl ca itself records a revocation. Idempotent.
    """
    intermediate = Path(ca_dir) / 'intermediate'
    index = intermediate / 'index.txt'
    if not index.exists():
        raise ValueError('CA index missing')
    try:
        target = int(str(serial), 16)
    except (TypeError, ValueError):
        raise ValueError('serial must be hexadecimal')
    now = datetime.now(timezone.utc).strftime('%y%m%d%H%M%SZ')
    found = changed = False
    out = []
    for line in index.read_text().splitlines():
        fields = line.split('\t')
        row_serial = None
        if len(fields) >= 6 and fields[3]:
            try:
                row_serial = int(fields[3], 16)
            except ValueError:
                row_serial = None
        if row_serial == target:
            found = True
            if fields[0] == 'R':
                out.append(line); continue
            fields[0] = 'R'; fields[2] = now
            out.append('\t'.join(fields)); changed = True
            continue
        out.append(line)
    if not found:
        raise ValueError('No certificate with serial %X' % target)
    if changed:
        _atomic_write(index, '\n'.join(out) + '\n')
    return {'revoked': '%X' % target, 'outcome': 'revoked' if changed else 'already_revoked'}


@_locked
def crl(ca_dir, days=30):
    ca = Path(ca_dir); intermediate = ca / 'intermediate'
    _ensure_intermediate_state(intermediate, 365)
    run('ca', '-config', intermediate / 'openssl.cnf', '-gencrl', '-crldays', days, '-out', intermediate / 'crl.pem')
    os.chmod(intermediate / 'crl.pem', 0o644)
    return {'crl': str(intermediate / 'crl.pem')}


def status(ca_dir):
    intermediate = Path(ca_dir) / 'intermediate'
    rows = []
    index = intermediate / 'index.txt'
    if index.exists():
        for line in index.read_text().splitlines():
            fields = line.split('\t')
            if len(fields) >= 6:
                rows.append({'state': fields[0], 'expires': fields[1], 'serial': fields[3], 'name': fields[5]})
    return {'issued': rows, 'crl': str(intermediate / 'crl.pem') if (intermediate / 'crl.pem').exists() else None}


def verify_chain(ca_dir, name, root_ca=None):
    ca = Path(ca_dir); intermediate = ca / 'intermediate'
    result = run('verify', '-CAfile', _root_cert(ca_dir, root_ca), '-untrusted', intermediate / 'intermediate.crt',
                 intermediate / 'certs' / f'{name}.crt')
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    root = sub.add_parser('init-root'); root.add_argument('--ca-dir', type=Path, required=True); root.add_argument('--days', type=int, default=3650)
    inter = sub.add_parser('init-intermediate'); inter.add_argument('--ca-dir', type=Path, required=True); inter.add_argument('--days', type=int, default=1825); inter.add_argument('--root-dir', type=Path, help='offline root directory (admin machine)')
    export_p = sub.add_parser('export-root'); export_p.add_argument('--ca-dir', type=Path, required=True); export_p.add_argument('--output', type=Path, required=True); export_p.add_argument('--apply', action='store_true')
    root_p = sub.add_parser('root-status'); root_p.add_argument('--ca-dir', type=Path, required=True)
    bundle_p = sub.add_parser('bundle-root'); bundle_p.add_argument('--offline-root', type=Path, required=True); bundle_p.add_argument('--output', type=Path, required=True)
    vbund_p = sub.add_parser('verify-bundle'); vbund_p.add_argument('--bundle', type=Path, required=True); vbund_p.add_argument('--intermediate', type=Path)
    audit_p = sub.add_parser('audit-root'); audit_p.add_argument('--runtime', type=Path, required=True)
    issue_p = sub.add_parser('issue'); issue_p.add_argument('--ca-dir', type=Path, required=True); issue_p.add_argument('--name', required=True)
    issue_p.add_argument('--kind', choices=['client', 'server', 'ocsp'], default='client'); issue_p.add_argument('--days', type=int, default=365)
    issue_p.add_argument('--ocsp-url', default=DEFAULT_OCSP_URL)
    issue_p.add_argument('--server-san', default='DNS:localhost,IP:127.0.0.1')
    renew_p = sub.add_parser('renew'); renew_p.add_argument('--ca-dir', type=Path, required=True); renew_p.add_argument('--name', required=True); renew_p.add_argument('--key', type=Path, required=True); renew_p.add_argument('--kind', choices=['client', 'server', 'ocsp'], default='client'); renew_p.add_argument('--days', type=int, default=365); renew_p.add_argument('--ocsp-url', default=DEFAULT_OCSP_URL); renew_p.add_argument('--server-san', default='DNS:localhost,IP:127.0.0.1')
    revoke_p = sub.add_parser('revoke'); revoke_p.add_argument('--ca-dir', type=Path, required=True); revoke_p.add_argument('--name', required=True); revoke_p.add_argument('--reason', default='keyCompromise')
    crl_p = sub.add_parser('crl'); crl_p.add_argument('--ca-dir', type=Path, required=True); crl_p.add_argument('--days', type=int, default=30)
    status_p = sub.add_parser('status'); status_p.add_argument('--ca-dir', type=Path, required=True)
    verify_p = sub.add_parser('verify'); verify_p.add_argument('--ca-dir', type=Path, required=True); verify_p.add_argument('--name', required=True); verify_p.add_argument('--root-ca', type=Path)
    args = parser.parse_args()
    if args.command == 'init-root': result = init_root(args.ca_dir, args.days)
    elif args.command == 'init-intermediate': result = init_intermediate(args.ca_dir, args.days, root_dir=args.root_dir)
    elif args.command == 'export-root': result = export_root(args.ca_dir, args.output, args.apply)
    elif args.command == 'root-status': result = root_status(args.ca_dir)
    elif args.command == 'bundle-root': result = bundle_root(args.offline_root, args.output)
    elif args.command == 'verify-bundle': result = verify_bundle(args.bundle, args.intermediate)
    elif args.command == 'audit-root': result = audit_root(args.runtime)
    elif args.command == 'issue': result = issue(args.ca_dir, args.name, args.kind, args.days, args.ocsp_url, args.server_san)
    elif args.command == 'renew': result = renew(args.ca_dir, args.name, args.kind, args.key, args.days, args.ocsp_url, args.server_san)
    elif args.command == 'revoke': result = revoke(args.ca_dir, args.name, args.reason)
    elif args.command == 'crl': result = crl(args.ca_dir, args.days)
    elif args.command == 'status': result = status(args.ca_dir)
    else: result = {'verify': verify_chain(args.ca_dir, args.name, args.root_ca)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
