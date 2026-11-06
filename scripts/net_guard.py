"""Keep the platform server reachable regardless of proxy TUN clients (assessment: network).

Route selection decides whether a proxy captures the platform; this checks which interface
reaches the server and can install a /32 host route so a proxy's 0/1 + 128/1 capture cannot win.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def parse_routes(text):
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] not in ('Routing', 'Internet', 'Destination', 'Netif'):
            rows.append({'dest': parts[0], 'gateway': parts[1], 'iface': parts[3]})
    return rows


def proxy_capture(routes):
    """Destinations a proxy TUN uses to capture everything (Clash 0/1 + 128/1 or default)."""
    return [row for row in routes if row['dest'] in ('0/1', '1', '128/1', '128.0/1', '0.0.0.0/1', '128.0.0.0/1')]


def host_route_command(server_ip, iface, system=None):
    system = system or sys.platform
    if system == 'darwin':
        return ['sudo', 'route', '-n', 'add', '-host', server_ip, '-interface', iface]
    return ['sudo', 'ip', 'route', 'add', server_ip + '/32', 'dev', iface]


def check(server, port=14222):
    from scripts.local_forwarder import candidates
    found = candidates(server, port)
    return {'server': server, 'port': port, 'reachable': bool(found), 'interfaces': found}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', required=True)
    parser.add_argument('--port', type=int, default=14222)
    parser.add_argument('--apply', action='store_true', help='install a /32 host route via the working interface')
    args = parser.parse_args()
    report = check(args.server, args.port)
    if report['interfaces']:
        iface = report['interfaces'][0]['device']
        report['recommended_host_route'] = ' '.join(host_route_command(args.server, iface))
        if args.apply:
            result = subprocess.run(host_route_command(args.server, iface), capture_output=True, text=True)
            report['applied'] = result.returncode == 0
            report['apply_output'] = (result.stdout + result.stderr).strip()[-300:]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['reachable'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
