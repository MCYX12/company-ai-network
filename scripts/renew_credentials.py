"""Extend or rotate the local PoC certificate validity without changing enrolled identities.

This manages the local *test* CA. It is not an enterprise CA: there is no CRL/OCSP,
no HSM, and the CA private key stays inside the runtime it protects.

Two CA modes:

* resign (default) -- re-sign ca.crt with the existing ca.key and a later notAfter.
  The CA public key is unchanged, so clients that already trust the old ca.crt keep
  working until they receive the refreshed ca.crt. This only extends expiry.
* rotate -- create a new CA key pair and reissue every leaf. ca.crt must be
  redistributed to every device before the old certificates can be discarded.
* none -- leave the CA untouched and only refresh leaf certificates.

Leaf private keys are reused unless --new-leaf-keys is given. Every generated
certificate is verified with `openssl verify -x509_strict` before anything is
written. Originals are copied to certs/history/<UTC stamp>/ and applied with
atomic os.replace. Running NATS/clients keep credentials in memory until restart,
so a restart is required to activate the new files.
"""
import argparse
import fcntl
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CA_SUBJECT = '/CN=Local-PoC-CA'
DEFAULT_SERVER_SAN = 'DNS:localhost,IP:127.0.0.1'


def _openssl(*args):
    subprocess.run(['openssl', *map(str, args)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _openssl_out(*args):
    return subprocess.check_output(['openssl', *map(str, args)], text=True, stderr=subprocess.STDOUT)


def cert_not_after(path):
    raw = _openssl_out('x509', '-in', path, '-noout', '-enddate').strip()
    value = raw.split('=', 1)[1]
    return ssl.cert_time_to_seconds(value), value


def cert_seconds_remaining(path, now=None):
    not_after, _ = cert_not_after(path)
    return not_after - (now if now is not None else datetime.now(timezone.utc).timestamp())


def cert_san(path):
    raw = _openssl_out('x509', '-in', path, '-noout', '-ext', 'subjectAltName')
    body = raw.split(':', 1)[1] if ':' in raw else ''
    entries = [part.strip() for part in body.replace('\n', ',').split(',') if part.strip()]
    mapped = []
    for entry in entries:
        if entry.lower().startswith('ip address:'):
            mapped.append('IP:' + entry.split(':', 1)[1].strip())
        elif entry.lower().startswith('dns:'):
            mapped.append('DNS:' + entry.split(':', 1)[1].strip())
    return ','.join(mapped)


def ca_public_key(path):
    return _openssl_out('x509', '-in', path, '-noout', '-pubkey')


def _purpose(name):
    return 'sslserver' if name == 'server' else 'sslclient'


def discover_leaves(certs):
    names = []
    for candidate in sorted(certs.glob('*.crt')):
        name = candidate.stem
        if name == 'ca' or name.startswith('ca.'):
            continue
        if not (certs / f'{name}.key').is_file():
            continue
        names.append(name)
    return names


def extension_for(name, server_san):
    if name == 'server':
        return ('basicConstraints=critical,CA:FALSE\n'
                'keyUsage=critical,digitalSignature,keyEncipherment\n'
                f'extendedKeyUsage=serverAuth\nsubjectAltName={server_san}\n')
    return ('basicConstraints=critical,CA:FALSE\n'
            'keyUsage=critical,digitalSignature,keyEncipherment\n'
            f'extendedKeyUsage=clientAuth\nsubjectAltName=DNS:{name}\n')


def _fsync_replace(source, target):
    with open(source, 'rb') as handle:
        os.fsync(handle.fileno())
    os.replace(source, target)
    directory = os.open(str(target.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _backup(certs, backup, relative_paths):
    backup.mkdir(parents=True, mode=0o700)
    for name in relative_paths:
        source = certs / name
        if source.is_file():
            shutil.copy2(source, backup / name)
    os.chmod(backup, 0o700)


def plan(runtime, days=30, ca_mode='resign', new_leaf_keys=False, server_san=None, identities=None):
    """Read-only description of what a renewal would change."""
    runtime = Path(runtime).resolve()
    certs = runtime / 'certs'
    if not certs.is_dir():
        raise ValueError(f'No certs directory at {certs}')
    if ca_mode not in {'resign', 'rotate', 'none'}:
        raise ValueError('ca_mode must be resign, rotate or none')
    if int(days) <= 0:
        raise ValueError('days must be a positive integer')
    discovered = discover_leaves(certs)
    if identities:
        unknown = [name for name in identities if name not in discovered]
        if unknown:
            raise ValueError(f'Unknown identities: {", ".join(sorted(unknown))}')
        discovered = [name for name in discovered if name in set(identities)]
    if not discovered:
        raise ValueError('No leaf identities selected')
    if (certs / 'ENTERPRISE_CA.json').is_file():
        raise ValueError('Runtime uses the two-tier enterprise CA; use scripts.renew_enterprise_ca or ca_tool renew, not this self-signed CA tool')
    if not (certs / 'ca.crt').is_file():
        raise ValueError('Missing ca.crt')
    if ca_mode in {'resign', 'none'} and not (certs / 'ca.key').is_file():
        raise ValueError('Missing ca.key required for the selected CA mode')
    detected = cert_san(certs / 'server.crt') if (certs / 'server.crt').is_file() else ''
    san = server_san or detected or DEFAULT_SERVER_SAN
    return {'runtime': runtime, 'certs': certs, 'leaves': discovered, 'ca_mode': ca_mode,
            'days': int(days), 'new_leaf_keys': bool(new_leaf_keys), 'server_san': san}


def renew(runtime, days=30, ca_mode='resign', new_leaf_keys=False, server_san=None,
          identities=None, dry_run=False):
    plan_data = plan(runtime, days, ca_mode, new_leaf_keys, server_san, identities)
    certs = plan_data['certs']
    leaves = plan_data['leaves']
    os.umask(0o077)
    lock_path = certs / '.renew.lock'
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _renew_locked(plan_data, dry_run)


def _renew_locked(plan_data, dry_run):
    certs = plan_data['certs']
    leaves = plan_data['leaves']
    days = plan_data['days']
    stage = Path(tempfile.mkdtemp(prefix='.renew-stage-', dir=certs))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    touched = []
    try:
        old_ca_not_after, old_ca_text = cert_not_after(certs / 'ca.crt')
        old_ca_pub = ca_public_key(certs / 'ca.crt')
        if plan_data['ca_mode'] == 'resign':
            _openssl('req', '-x509', '-key', certs / 'ca.key', '-days', days,
                     '-out', stage / 'ca.crt', '-subj', CA_SUBJECT,
                     '-addext', 'basicConstraints=critical,CA:TRUE',
                     '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
            ca_cert, ca_key = stage / 'ca.crt', certs / 'ca.key'
        elif plan_data['ca_mode'] == 'rotate':
            _openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                     '-keyout', stage / 'ca.key', '-out', stage / 'ca.crt', '-days', days,
                     '-subj', CA_SUBJECT, '-addext', 'basicConstraints=critical,CA:TRUE',
                     '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
            ca_cert, ca_key = stage / 'ca.crt', stage / 'ca.key'
        else:
            ca_cert, ca_key = certs / 'ca.crt', certs / 'ca.key'
        new_ca_not_after, new_ca_text = cert_not_after(ca_cert)
        new_ca_pub = ca_public_key(ca_cert)
        records = []
        for name in leaves:
            old_not_after, old_text = cert_not_after(certs / f'{name}.crt')
            if plan_data['new_leaf_keys']:
                _openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes',
                         '-keyout', stage / f'{name}.key', '-out', stage / f'{name}.csr',
                         '-subj', f'/CN={name}')
                leaf_key = stage / f'{name}.key'
            else:
                _openssl('req', '-new', '-key', certs / f'{name}.key',
                         '-out', stage / f'{name}.csr', '-subj', f'/CN={name}')
                leaf_key = certs / f'{name}.key'
            ext = stage / f'{name}.ext'
            ext.write_text(extension_for(name, plan_data['server_san']))
            _openssl('x509', '-req', '-in', stage / f'{name}.csr', '-CA', ca_cert, '-CAkey', ca_key,
                     '-CAcreateserial', '-days', days, '-extfile', ext, '-out', stage / f'{name}.crt')
            _openssl('verify', '-x509_strict', '-CAfile', ca_cert, '-purpose', _purpose(name),
                     stage / f'{name}.crt')
            new_not_after, new_text = cert_not_after(stage / f'{name}.crt')
            records.append({'name': name, 'old_not_after': old_text, 'new_not_after': new_text,
                            'old_seconds_remaining': old_not_after - datetime.now(timezone.utc).timestamp(),
                            'new_seconds_remaining': new_not_after - datetime.now(timezone.utc).timestamp(),
                            'new_key': bool(plan_data['new_leaf_keys']),
                            'server_san': plan_data['server_san'] if name == 'server' else None})
        report = {'runtime': str(plan_data['runtime']), 'applied': False, 'ca_mode': plan_data['ca_mode'],
                  'days': days, 'backup': None,
                  'ca': {'subject': _openssl_out('x509', '-in', ca_cert, '-noout', '-subject').strip(),
                         'old_not_after': old_ca_text, 'new_not_after': new_ca_text,
                         'public_key_changed': old_ca_pub != new_ca_pub,
                         'resigned': plan_data['ca_mode'] != 'none'},
                  'leaves': records, 'restart_required': True}
        if dry_run:
            return report
        backup = certs / 'history' / stamp
        names = [f'{name}.crt' for name in leaves]
        if plan_data['ca_mode'] != 'none':
            names.append('ca.crt')
        if plan_data['ca_mode'] == 'rotate':
            names.append('ca.key')
        if plan_data['new_leaf_keys']:
            names.extend(f'{name}.key' for name in leaves)
        _backup(certs, backup, names)
        if plan_data['ca_mode'] == 'rotate':
            _fsync_replace(stage / 'ca.key', certs / 'ca.key')
        if plan_data['ca_mode'] != 'none':
            _fsync_replace(stage / 'ca.crt', certs / 'ca.crt')
        for name in leaves:
            if plan_data['new_leaf_keys']:
                _fsync_replace(stage / f'{name}.key', certs / f'{name}.key')
            _fsync_replace(stage / f'{name}.crt', certs / f'{name}.crt')
        for name in [*(f'{name}.crt' for name in leaves), 'ca.crt']:
            if (certs / name).is_file():
                os.chmod(certs / name, 0o644)
        for name in [*(f'{name}.key' for name in leaves), 'ca.key']:
            if (certs / name).is_file():
                os.chmod(certs / name, 0o600)
        touched = names
        report['applied'] = True
        report['backup'] = str(backup)
        report['applied_at'] = datetime.now(timezone.utc).isoformat()
        (backup / 'renewal.json').write_text(json.dumps(report, indent=2, ensure_ascii=False))
        return report
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def check(runtime, warn_days=7):
    runtime = Path(runtime).resolve()
    certs = runtime / 'certs'
    if not certs.is_dir():
        raise ValueError(f'No certs directory at {certs}')
    now = datetime.now(timezone.utc).timestamp()
    rows = []
    for path in sorted(certs.glob('*.crt')):
        if not (certs / f'{path.stem}.key').is_file() and path.stem != 'ca':
            continue
        not_after, text = cert_not_after(path)
        rows.append({'name': path.stem, 'not_after': text,
                     'seconds_remaining': not_after - now,
                     'expiring': not_after - now < warn_days * 86400,
                     'expired': not_after < now})
    return rows


def main():
    parser = argparse.ArgumentParser(description='Renew or rotate local PoC certificates')
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--days', type=int, default=30, help='new validity in days (default 30)')
    parser.add_argument('--ca-mode', choices=['resign', 'rotate', 'none'], default='resign')
    parser.add_argument('--new-leaf-keys', action='store_true', help='issue fresh leaf key pairs')
    parser.add_argument('--server-san', help='override the server certificate SAN list')
    parser.add_argument('--identity', action='append', dest='identities', help='renew only this identity (repeatable)')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--check', action='store_true', help='report expiry only; change nothing')
    parser.add_argument('--warn-days', type=int, default=7)
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check(args.runtime, args.warn_days), ensure_ascii=False, indent=2))
        return
    print(json.dumps(renew(args.runtime, args.days, args.ca_mode, args.new_leaf_keys,
                           args.server_san, args.identities, args.dry_run),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.exit(main())
