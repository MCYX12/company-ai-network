"""Renew runtime leaf certificates from the enterprise intermediate CA, reusing existing private keys."""
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
from scripts.migrate_enterprise_ca import detect_san


def renew_runtime(runtime, days=365, apply=False, skip=None, authorize=None):
    skip = set(skip or ())
    runtime = Path(runtime).resolve(); certs = runtime / 'certs'
    marker = certs / 'ENTERPRISE_CA.json'
    if not marker.exists():
        raise ValueError('Runtime is not on the enterprise CA (missing certs/ENTERPRISE_CA.json)')
    info = json.loads(marker.read_text())
    ca_dir = Path(info['ca_dir']); ocsp_url = info['ocsp_url']
    intermediate = ca_dir / 'intermediate'
    server_san = detect_san(certs / 'server.crt') or 'DNS:localhost,IP:127.0.0.1'
    names = [p.stem for p in sorted(certs.glob('*.crt'))
             if p.stem != 'ca' and not p.stem.startswith('ca.') and p.stem != 'server'
             and p.stem not in skip and (certs / f'{p.stem}.key').exists()]
    plan = {'runtime': str(runtime), 'ca_dir': str(ca_dir), 'ocsp_url': ocsp_url,
            'server_san': server_san, 'renew': [*names, 'server']}
    if not apply:
        plan['applied'] = False
        return plan
    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = certs / 'history' / f'enterprise-renew-{stamp}'
    backup.mkdir(parents=True, exist_ok=True)
    for name in [*names, 'server']:
        source = certs / f'{name}.crt'
        if source.exists():
            shutil.copy2(source, backup / f'{name}.crt')
    stage = Path(tempfile.mkdtemp(prefix='.renew-ent-', dir=certs))
    try:
        def build(name, kind, key, san=None):
            result = ca_tool.renew(ca_dir, name, kind, key, days, ocsp_url,
                                   san or 'DNS:localhost,IP:127.0.0.1', authorize=authorize)
            chain = Path(result['cert']).read_bytes() + (intermediate / 'intermediate.crt').read_bytes()
            (stage / f'{name}.crt').write_bytes(chain)
            os.chmod(stage / f'{name}.crt', 0o644)
        for name in names:
            build(name, 'client', certs / f'{name}.key')
        build('server', 'server', certs / 'server.key', server_san)
        config = json.loads((runtime / 'nats.conf').read_text())
        config['tls']['cert_file'] = str(stage / 'server.crt')
        config['tls']['key_file'] = str(certs / 'server.key')
        config['tls']['ca_file'] = str(certs / 'ca.crt')
        candidate = runtime / 'nats.renew.conf'
        candidate.write_text(json.dumps(config, indent=2, ensure_ascii=False))
        try:
            subprocess.run([str(ROOT / '.runtime/bin/nats-server'), '-t', '-c', str(candidate)], check=True, capture_output=True)
        finally:
            candidate.unlink(missing_ok=True)
        for path in sorted(stage.iterdir()):
            shutil.copy2(path, certs / path.name)
            os.chmod(certs / path.name, 0o644)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    plan.update(applied=True, backup=str(backup), restart_required=True, skipped=sorted(skip))
    return plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--days', type=int, default=365)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(renew_runtime(args.runtime, args.days, args.apply), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
