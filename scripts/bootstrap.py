"""Generate local-only test CA, client identities and least-privilege NATS config."""
import json
import os
from pathlib import Path
import subprocess
import shutil

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
PREFIX = 'corp.dev.v1'

def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

def main(listen=14222):
    os.umask(0o077)
    if (RUNTIME / 'nats.conf').exists():
        print('Existing runtime preserved; no credentials rotated.')
        return
    RUNTIME.mkdir(exist_ok=True, mode=0o700)
    certs = RUNTIME / 'certs'
    certs.mkdir(exist_ok=True, mode=0o700)
    run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '7',
        '-keyout', str(certs/'ca.key'), '-out', str(certs/'ca.crt'), '-subj', '/CN=Local-PoC-CA',
        '-addext', 'basicConstraints=critical,CA:TRUE',
        '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    for name in ['server', 'router', 'ag-a', 'ag-b', 'ag-c', 'unregistered']:
        ext = certs / f'{name}.ext'
        ext.write_text('basicConstraints=CA:FALSE\n' + (
            'extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n'
            if name == 'server' else f'extendedKeyUsage=clientAuth\nsubjectAltName=DNS:{name}\n'))
        run('openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(certs/f'{name}.key'), '-out', str(certs/f'{name}.csr'), '-subj', f'/CN={name}')
        run('openssl', 'x509', '-req', '-in', str(certs/f'{name}.csr'), '-CA', str(certs/'ca.crt'),
            '-CAkey', str(certs/'ca.key'), '-CAcreateserial', '-days', '7', '-extfile', str(ext),
            '-out', str(certs/f'{name}.crt'))
    users = []
    for agent in ['ag-a', 'ag-b', 'ag-c']:
        users.append({'user': agent, 'permissions': {
            'publish': {'allow': [f'{PREFIX}.ingress.{agent}.request',
                f'{PREFIX}.agent.{agent}.heartbeat', f'{PREFIX}.agent.{agent}.reply', f'{PREFIX}.agent.{agent}.result']},
            'subscribe': {'allow': [f'{PREFIX}.agent.{agent}.query', f'{PREFIX}.client.{agent}.response', f'{PREFIX}.agent.{agent}.task']}}})
    users.append({'user': 'router', 'permissions': {
        'publish': {'allow': [f'{PREFIX}.agent.*.query', f'{PREFIX}.client.*.response', f'{PREFIX}.agent.*.task']},
        'subscribe': {'allow': [f'{PREFIX}.ingress.*.request', f'{PREFIX}.agent.*.reply',
                              f'{PREFIX}.agent.*.heartbeat', f'{PREFIX}.agent.*.result']}}})
    config = {'listen': f'127.0.0.1:{listen}', 'max_payload': 65536,
              'tls': {'cert_file': str(certs/'server.crt'), 'key_file': str(certs/'server.key'),
                      'ca_file': str(certs/'ca.crt'), 'verify_and_map': True},
              'accounts': {'POC': {'users': users}}}
    (RUNTIME/'nats.conf').write_text(json.dumps(config, indent=2, ensure_ascii=False))
    (RUNTIME/'endpoint.txt').write_text(f'tls://127.0.0.1:{listen}\n')
    # Local test identities are explicit fixtures; enterprise enrollment starts empty.
    shutil.copy2(ROOT/'tests/fixtures/registry.demo.json', RUNTIME/'registry.json')
    print('Generated local-only mTLS PoC configuration, 7-day test certificates.')

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--listen', type=int, default=14222)
    main(parser.parse_args().listen)
