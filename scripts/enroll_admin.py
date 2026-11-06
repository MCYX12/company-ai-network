"""Enroll an administrator mTLS identity for the admin channel (run on the issuing host)."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.common import PREFIX
from scripts import ca_tool


def enroll(runtime, identity, days=365, activation_note=True):
    runtime = Path(runtime).resolve()
    marker = runtime / 'certs/ENTERPRISE_CA.json'
    if not marker.exists():
        raise ValueError('Runtime is not on the enterprise CA')
    ca_dir = Path(json.loads(marker.read_text())['ca_dir'])
    certs = runtime / 'certs'
    issued = ca_tool.issue(ca_dir, identity, 'client', days, 'http://127.0.0.1:2560')
    chain = Path(issued['cert']).read_bytes() + (ca_dir / 'intermediate/intermediate.crt').read_bytes()
    os.umask(0o077)
    (certs / f'{identity}.crt').write_bytes(chain)
    os.chmod(certs / f'{identity}.crt', 0o644)
    shutil.copy2(issued['key'], certs / f'{identity}.key')
    os.chmod(certs / f'{identity}.key', 0o600)
    admin_path = runtime / 'admin-identities.json'
    data = json.loads(admin_path.read_text()) if admin_path.exists() else {'admins': []}
    if identity not in data['admins']:
        data['admins'].append(identity)
    admin_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    config = json.loads((runtime / 'nats.conf').read_text())
    users = config['accounts']['POC']['users']
    if not any(u.get('user') == identity for u in users):
        users.append({'user': identity, 'permissions': {'publish': {'allow': [f'{PREFIX}.ingress.{identity}.request']},
                                                        'subscribe': {'allow': [f'{PREFIX}.client.{identity}.response']}}})
    candidate = runtime / 'nats.admin.conf'
    candidate.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    binary = os.environ.get('NATS_SERVER', str(ROOT / '.runtime/bin/nats-server'))
    try:
        subprocess.run([binary, '-t', '-c', str(candidate)], check=True, capture_output=True, timeout=10)
    finally:
        candidate.unlink(missing_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    shutil.copy2(runtime / 'nats.conf', runtime / f'nats.before-admin-{stamp}.conf')
    (runtime / 'nats.conf').write_text(json.dumps(config, indent=2, ensure_ascii=False))
    report = {'identity': identity, 'admin': True, 'days': days, 'runtime': str(runtime),
              'transport_activation_required': activation_note}
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--identity', required=True)
    parser.add_argument('--days', type=int, default=365)
    parser.add_argument('--bind-admin', help='web administrator account name to bind this mTLS identity to')
    args = parser.parse_args()
    report = enroll(args.runtime, args.identity, args.days)
    if args.bind_admin:
        from app.accounts import Accounts
        report['bound_admin'] = Accounts(args.runtime / 'accounts.sqlite3').bind_identity('local-admin', args.bind_admin, args.identity)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
