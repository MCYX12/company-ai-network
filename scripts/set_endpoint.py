"""Point an existing deployment at a new reachable address (LAN change or VPN).

Re-issues the server certificate with a SAN for the new address (always keeping
localhost/127.0.0.1 for local checks), updates endpoint.txt and optionally the NATS
listen address, then validates the configuration. Does not restart services.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ca_tool


def san_for(address):
    try:
        ip = str(ipaddress.ip_address(address))
        return f'DNS:localhost,IP:127.0.0.1,IP:{ip}'
    except ValueError:
        return f'DNS:localhost,IP:127.0.0.1,DNS:{address}'


def existing_sans(cert_path):
    """Read DNS/IP SANs from an existing certificate so adding a VPN address keeps the LAN ones."""
    import re
    result = subprocess.run(['openssl', 'x509', '-in', str(cert_path), '-noout', '-ext', 'subjectAltName'],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return []
    block = result.stdout.split(':', 1)[-1]
    sans = []
    for part in block.split(','):
        match = re.match(r'^(DNS|IP Address):\s*(\S+)\s*$', part.strip())
        if match:
            sans.append(('IP:' if match.group(1) == 'IP Address' else 'DNS:') + match.group(2))
    return sans


def set_endpoint(runtime, address, port=14222, listen=None, days=365, dry_run=False, keep_sans=True, canonical=None):
    """address may be one address or a list (e.g. wired + wifi + VPN); the cert SAN covers all."""
    runtime = Path(runtime)
    certs = runtime / 'certs'
    addresses = [address] if isinstance(address, str) else [a for a in address if a]
    if not addresses:
        raise ValueError('at least one address is required')
    candidates = []
    for item in addresses:
        candidates += san_for(item).split(',')
    if keep_sans and (certs / 'server.crt').exists():
        candidates = existing_sans(certs / 'server.crt') + candidates
    merged = []
    for item in candidates:
        item = item.strip()
        if item and item not in merged:
            merged.append(item)
    san = ','.join(merged)
    primary = canonical or addresses[0]
    report = {'runtime': str(runtime), 'addresses': addresses, 'canonical': primary, 'port': port, 'san': san,
              'endpoint': f'tls://{primary}:{port}'}
    if listen:
        report['listen'] = listen
    if dry_run:
        report['dry_run'] = True
        return report
    marker = certs / 'ENTERPRISE_CA.json'
    if marker.exists():
        ca_dir = Path(json.loads(marker.read_text())['ca_dir'])
        key = certs / 'server.key'
        if not key.exists():
            raise ValueError('server.key not found; cannot re-issue the server certificate')
        issued = ca_tool.renew(ca_dir, 'server', 'server', key, days=days,
                               ocsp_url='http://127.0.0.1:2560', server_san=san)
        chain = Path(issued['cert']).read_bytes() + (ca_dir / 'intermediate/intermediate.crt').read_bytes()
        (certs / 'server.crt').write_bytes(chain)
        os.chmod(certs / 'server.crt', 0o644)
        report['server_cert'] = 're-issued from the enterprise CA'
    else:
        report['server_cert'] = 'skipped: no enterprise CA marker (a test-CA deployment must be re-bootstrapped)'
    (runtime / 'endpoint.txt').write_text(report['endpoint'] + '\n')
    config_path = runtime / 'nats.conf'
    if config_path.exists():
        config = json.loads(config_path.read_text())
        if listen:
            config['listen'] = listen
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        report['nats_listen'] = config.get('listen')
        binary = os.environ.get('NATS_SERVER', str(ROOT / '.runtime/bin/nats-server'))
        result = subprocess.run([binary, '-t', '-c', str(config_path)], capture_output=True, text=True)
        report['nats_conf_valid'] = result.returncode == 0
        if result.returncode != 0:
            report['nats_conf_error'] = (result.stderr + result.stdout).strip()[-400:]
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--address', action='append', required=True,
                        help='IP or DNS name clients use (repeat for wired + wifi + VPN)')
    parser.add_argument('--endpoint', help='canonical address to write to endpoint.txt (default: first --address)')
    parser.add_argument('--port', type=int, default=14222)
    parser.add_argument('--listen', help='override nats.conf listen, e.g. 0.0.0.0:14222')
    parser.add_argument('--days', type=int, default=365)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    report = set_endpoint(args.runtime, args.address, args.port, args.listen, args.days, args.dry_run,
                          canonical=args.endpoint)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
