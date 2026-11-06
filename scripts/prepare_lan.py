"""Prepare an isolated LAN PoC and a least-privilege ag-b client bundle."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import zipfile
import bootstrap

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ip', required=True)
    args = parser.parse_args()
    address = str(ipaddress.IPv4Address(args.ip))
    os.umask(0o077)
    runtime = ROOT/'.runtime-lan'
    if runtime.exists():
        raise SystemExit('Existing LAN runtime preserved. Use a new directory or archive it before regeneration.')
    bootstrap.RUNTIME = runtime
    bootstrap.main()
    certs = runtime/'certs'
    extension = certs/'server.ext'
    extension.write_text(f'basicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:{address}\n')
    bootstrap.run('openssl', 'x509', '-req', '-in', str(certs/'server.csr'), '-CA', str(certs/'ca.crt'),
                  '-CAkey', str(certs/'ca.key'), '-CAcreateserial', '-days', '7', '-extfile', str(extension),
                  '-out', str(certs/'server.crt'))
    config = json.loads((runtime/'nats.conf').read_text())
    config['listen'] = f'{address}:14222'
    (runtime/'nats.conf').write_text(json.dumps(config, ensure_ascii=False, indent=2))
    (runtime/'endpoint.txt').write_text(f'tls://{address}:14222\n')
    subprocess.run([str(ROOT/'.runtime/bin/nats-server'), '-t', '-c', str(runtime/'nats.conf')], check=True)
    dist = ROOT/'dist-private'
    dist.mkdir(mode=0o700, exist_ok=True)
    bundle = dist/'mac-agent-b.zip'
    with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
        for file in sorted((ROOT/'app').glob('*.py')):
            archive.write(file, 'mac-agent-b/app/'+file.name)
        archive.write(ROOT/'requirements.lock', 'mac-agent-b/requirements.lock')
        registry = json.loads((ROOT/'tests/fixtures/registry.demo.json').read_text())
        registry['agents'] = [row for row in registry['agents'] if row['agent_id'] == 'ag-b']
        archive.writestr('mac-agent-b/.runtime/registry.json', json.dumps(registry))
        for name in ['ca.crt', 'ag-b.crt', 'ag-b.key']:
            archive.write(certs/name, 'mac-agent-b/.runtime/certs/'+name)
        archive.writestr('mac-agent-b/endpoint.txt', f'tls://{address}:14222\n')
        archive.writestr('mac-agent-b/start-mac.sh', '''#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
chmod 700 .runtime .runtime/certs
chmod 600 .runtime/certs/ag-b.key
export POC_RUNTIME="$PWD/.runtime"
export NATS_URL="$(cat endpoint.txt)"
exec .venv/bin/python -m app.agent ag-b
''')
        archive.writestr('mac-agent-b/query-ubuntu.sh', '''#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export POC_RUNTIME="$PWD/.runtime"
export NATS_URL="$(cat endpoint.txt)"
exec .venv/bin/python -m app.client --identity ag-b --target ag-a --project demo-project
''')
    print('LAN config prepared; no network service started.')
    print('Mac private bundle:', bundle)

if __name__ == '__main__':
    main()
