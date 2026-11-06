"""Client-side network scenario report.

Answers, for a terminal with wired / Wi-Fi / VPN interfaces: which link can reach the
platform server, and therefore whether it is inside the intranet or needs the VPN/proxy.
Probes bind to each interface address so the chosen route is explicit.
"""
import argparse
import json
import platform
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.net_precheck import probe, tls_probe

VPN_PREFIXES = ('utun', 'tun', 'tap', 'wg', 'ppp', 'ipsec')


def classify_ports(text):
    """macOS networksetup -listallhardwareports -> {device: wifi|wired|other}."""
    mapping = {}
    port = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('Hardware Port:'):
            port = stripped.split(':', 1)[1].strip()
        elif stripped.startswith('Device:'):
            device = stripped.split(':', 1)[1].strip()
            name = (port or '').lower()
            if 'wi-fi' in name or 'airport' in name:
                mapping[device] = 'wifi'
            elif 'ethernet' in name:
                mapping[device] = 'wired'
            else:
                mapping[device] = 'other'
    return mapping


def parse_ifconfig(text):
    """macOS ifconfig -> {device: {'addresses': [...], 'status': 'up'|'down'}}."""
    result = {}
    device = None
    for raw in text.splitlines():
        if raw and not raw[0].isspace():
            device = raw.split(':', 1)[0].split('%')[0]
            rest = raw.split(':', 1)[1]
            result[device] = {'addresses': [], 'status': 'up' if 'UP' in rest.split('<')[-1].split('>')[0] or ' UP' in rest else 'down'}
        elif device and device in result:
            line = raw.strip()
            match = re.match(r'inet (\d+\.\d+\.\d+\.\d+)', line)
            if match:
                result[device]['addresses'].append(match.group(1))
    return result


def classify_device(device, ports):
    if device in ports:
        return ports[device]
    return 'vpn' if device.startswith(VPN_PREFIXES) else 'other'


def interfaces():
    system = platform.system()
    if system == 'Darwin':
        ports = classify_ports(subprocess.run(['networksetup', '-listallhardwareports'],
                                              capture_output=True, text=True).stdout)
        data = parse_ifconfig(subprocess.run(['ifconfig'], capture_output=True, text=True).stdout)
        rows = []
        for device, info in data.items():
            if device.startswith('lo'):
                continue
            rows.append({'device': device, 'kind': classify_device(device, ports),
                         'status': info['status'], 'addresses': info['addresses']})
        return rows
    rows = []
    brief = subprocess.run(['ip', '-brief', 'addr'], capture_output=True, text=True).stdout
    for line in brief.splitlines():
        parts = line.split()
        if not parts:
            continue
        device = parts[0].split('@')[0]
        if device.startswith('lo'):
            continue
        addresses = [part.split('/')[0] for part in parts[2:] if re.match(r'^\d+\.\d+\.\d+\.\d+/\d+$', part)]
        kind = 'vpn' if device.startswith(VPN_PREFIXES) else 'other'
        rows.append({'device': device, 'kind': kind, 'status': parts[1] if len(parts) > 1 else 'unknown',
                     'addresses': addresses})
    return rows


def default_interface():
    try:
        if platform.system() == 'Darwin':
            text = subprocess.run(['route', '-n', 'get', 'default'], capture_output=True, text=True).stdout
            match = re.search(r'interface:\s*(\S+)', text)
        else:
            text = subprocess.run(['ip', 'route', 'show', 'default'], capture_output=True, text=True).stdout
            match = re.search(r'dev\s+(\S+)', text)
        return match.group(1) if match else None
    except OSError:
        return None


def endpoint_address(endpoint_file=None, runtime=None):
    if endpoint_file:
        text = Path(endpoint_file).read_text().strip()
    elif runtime:
        path = Path(runtime) / 'endpoint.txt'
        if not path.exists():
            return None
        text = path.read_text().strip()
    else:
        return None
    return text.replace('tls://', '').replace('nats://', '')


def verdict(reachable, interface_rows, default_dev):
    if not reachable:
        return {'status': 'unreachable',
                'hint': '服务器不可达：若终端不在内网，请连接 VPN/代理；再确认 endpoint 地址与证书 SAN'}
    kinds = {row['device']: row['kind'] for row in interface_rows}
    via_vpn = [d for d in reachable if kinds.get(d) == 'vpn']
    via_lan = [d for d in reachable if kinds.get(d) in ('wired', 'wifi')]
    if via_vpn and via_lan:
        status = 'both'
    elif via_vpn:
        status = 'via_vpn'
    elif via_lan:
        status = 'direct_intranet'
    else:
        status = 'reachable_other'
    return {'status': status, 'via': reachable, 'via_vpn': via_vpn, 'via_lan': via_lan,
            'default_interface': default_dev}


def scenarios(endpoint_file=None, runtime=None, ca=None, cert=None, key=None, timeout=3.0):
    rows = interfaces()
    default_dev = default_interface()
    address = endpoint_address(endpoint_file, runtime)
    report = {'platform': platform.system(), 'endpoint': address, 'default_interface': default_dev,
              'interfaces': rows, 'probes': [], 'verdict': None}
    if not address:
        report['verdict'] = {'status': 'no_endpoint', 'hint': '未找到 endpoint.txt（用 --runtime 或 --endpoint-file 指定）'}
        return report
    host, _, port = address.partition(':')
    port = int(port or 14222)
    for row in rows:
        for ip in row['addresses']:
            result = probe(host, port, timeout, source_ip=ip)
            entry = {'device': row['device'], 'kind': row['kind'], 'source_ip': ip, 'tcp': result}
            if ca and result == 'open':
                entry['tls'] = tls_probe(host, port, ca, timeout, cert, key, source_ip=ip)
            report['probes'].append(entry)
    reachable = sorted({entry['device'] for entry in report['probes'] if entry['tcp'] == 'open'})
    report['verdict'] = verdict(reachable, rows, default_dev)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--endpoint-file', type=Path)
    parser.add_argument('--ca', type=Path)
    parser.add_argument('--cert', type=Path)
    parser.add_argument('--key', type=Path)
    parser.add_argument('--timeout', type=float, default=3.0)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    report = scenarios(args.endpoint_file, args.runtime, args.ca, args.cert, args.key, args.timeout)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text)
    print(text)
    return 0 if report['verdict'].get('status') not in {'unreachable', 'no_endpoint'} else 1


if __name__ == '__main__':
    sys.exit(main())
