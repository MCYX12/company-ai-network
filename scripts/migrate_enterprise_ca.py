"""Migrate a runtime's mTLS material to the two-tier enterprise CA (offline root + intermediate).

Re-issues every NATS user identity and the server certificate with an OCSP AIA URL,
writes certificate chains (leaf + intermediate), sets ca.crt to the offline root and
rewrites nats.conf to use tls.ocsp_peer with caching disabled. Backs everything up
under certs/history/enterprise-<stamp>/ and never touches a running process: the
operator starts the OCSP responder and restarts the stack afterwards.

Default is a dry run; pass --apply to change files.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ca_tool

DEFAULT_OCSP_PORT = 2560


def detect_san(cert):
    if not cert.exists():
        return None
    out = subprocess.run(['openssl', 'x509', '-in', str(cert), '-noout', '-ext', 'subjectAltName'],
                         capture_output=True, text=True)
    body = out.stdout.split(':', 1)[1] if ':' in out.stdout else ''
    parts = [p.strip() for p in body.replace('\n', ',').split(',') if p.strip()]
    mapped = []
    for part in parts:
        if part.lower().startswith('ip address:'):
            mapped.append('IP:' + part.split(':', 1)[1].strip())
        elif part.lower().startswith('dns:'):
            mapped.append('DNS:' + part.split(':', 1)[1].strip())
    return ','.join(mapped) or None


def plan(runtime, ca_dir, ocsp_port, server_san):
    runtime = Path(runtime).resolve()
    conf = json.loads((runtime / 'nats.conf').read_text())
    users = [u['user'] for u in conf['accounts']['POC']['users'] if isinstance(u, dict) and 'user' in u]
    return {'runtime': str(runtime), 'ca_dir': str(ca_dir), 'ocsp_url': f'http://127.0.0.1:{ocsp_port}',
            'clients': users, 'server_san': server_san, 'existing_certs': sorted(p.name for p in (runtime / 'certs').glob('*.crt'))}


def migrate(runtime, ca_dir, ocsp_port=DEFAULT_OCSP_PORT, server_san=None, apply=False):
    runtime = Path(runtime).resolve()
    ca_dir = Path(ca_dir).resolve()
    certs = runtime / 'certs'
    if not (runtime / 'nats.conf').exists() or not certs.is_dir():
        raise ValueError('Runtime needs certs/ and nats.conf')
    if server_san is None:
        server_san = detect_san(certs / 'server.crt') or 'DNS:localhost,IP:127.0.0.1'
    ocsp_url = f'http://127.0.0.1:{ocsp_port}'
    info = plan(runtime, ca_dir, ocsp_port, server_san)
    if not apply:
        info['applied'] = False
        return info

    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = certs / 'history' / f'enterprise-{stamp}'
    backup.mkdir(parents=True, exist_ok=True)
    for path in [*certs.glob('*.crt'), *certs.glob('*.key')]:
        shutil.copy2(path, backup / path.name)
    shutil.copy2(runtime / 'nats.conf', backup / 'nats.conf')

    # Enterprise CA (offline root signs the issuing intermediate).
    # If the root has already been moved offline, never create a new one here.
    offline_root = (ca_dir / 'OFFLINE_ROOT.json').exists()
    if not offline_root and not (ca_dir / 'root/root.crt').exists():
        ca_tool.init_root(ca_dir, days=3650)
    if not (ca_dir / 'intermediate/intermediate.crt').exists():
        ca_tool.init_intermediate(ca_dir, days=1825)
    if not (ca_dir / 'intermediate/certs/ocsp-signer.crt').exists():
        ca_tool.issue(ca_dir, 'ocsp-signer', 'ocsp', 365, ocsp_url)

    stage = Path(tempfile.mkdtemp(prefix='.enterprise-', dir=certs))
    intermediate = ca_dir / 'intermediate'
    issued = []
    try:
        def issue_into(name, kind):
            result = ca_tool.issue(ca_dir, name, kind, 365, ocsp_url, server_san)
            chain = Path(result['cert']).read_bytes() + (intermediate / 'intermediate.crt').read_bytes()
            (stage / f'{name}.crt').write_bytes(chain)
            os.chmod(stage / f'{name}.crt', 0o644)
            shutil.copy2(result['key'], stage / f'{name}.key')
            os.chmod(stage / f'{name}.key', 0o600)
            issued.append(name)

        for name in info['clients']:
            issue_into(name, 'client')
        issue_into('server', 'server')
        shutil.copy2(ca_dir / 'root/root.crt', stage / 'ca.crt')
        os.chmod(stage / 'ca.crt', 0o644)

        config = json.loads((runtime / 'nats.conf').read_text())
        config['tls'].update({'cert_file': str(stage / 'server.crt'), 'key_file': str(stage / 'server.key'),
                              'ca_file': str(stage / 'ca.crt'), 'verify_and_map': True,
                              'ocsp_peer': {'verify': True, 'allow_when_ca_unreachable': False, 'warn_only': False}})
        config['ocsp_cache'] = {'type': 'none'}
        candidate = runtime / 'nats.enterprise.conf'
        candidate.write_text(json.dumps(config, indent=2, ensure_ascii=False))
        try:
            subprocess.run([str(ROOT / '.runtime/bin/nats-server'), '-t', '-c', str(candidate)], check=True, capture_output=True)
        finally:
            candidate.unlink(missing_ok=True)

        for path in sorted(stage.iterdir()):
            shutil.copy2(path, certs / path.name)
            os.chmod(certs / path.name, 0o600 if path.suffix == '.key' else 0o644)
        config['tls']['cert_file'] = str(certs / 'server.crt')
        config['tls']['key_file'] = str(certs / 'server.key')
        config['tls']['ca_file'] = str(certs / 'ca.crt')
        (runtime / 'nats.conf').write_text(json.dumps(config, indent=2, ensure_ascii=False))
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    (certs / 'ENTERPRISE_CA.json').write_text(json.dumps({'format': 1, 'ca_dir': str(ca_dir), 'ocsp_url': ocsp_url,
        'root': str(ca_dir / 'root/root.crt'), 'intermediate': str(intermediate / 'intermediate.crt'),
        'ocsp_port': ocsp_port}, indent=2))
    os.chmod(certs / 'ENTERPRISE_CA.json', 0o600)
    info.update(applied=True, issued=issued, backup=str(backup),
                note='start the OCSP responder on the AIA port, then restart the stack; on failure restore certs/nats.conf from backup')
    return info


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--ca-dir', type=Path, required=True)
    parser.add_argument('--ocsp-port', type=int, default=DEFAULT_OCSP_PORT)
    parser.add_argument('--server-san')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(migrate(args.runtime, args.ca_dir, args.ocsp_port, args.server_san, args.apply),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
