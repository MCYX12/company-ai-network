"""Field connectivity precheck for the two-VLAN acceptance drill (NET-01).

Runs a declared probe plan (allowed endpoints must be reachable, forbidden ones must
not) and writes a JSON report suitable for the acceptance record. Pure network
reachability; it does not change any firewall rule.
"""
import argparse
from datetime import datetime, timezone
import errno
import json
import socket
import sys

UNREACHABLE = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.ENETRESET}
EXPECTED = {'open', 'blocked'}


def classify(expect, result):
    if expect == 'open':
        return 'PASS' if result == 'open' else 'FAIL'
    if expect == 'blocked':
        return 'PASS' if result in {'refused', 'timeout', 'unreachable', 'error'} else 'FAIL'
    raise ValueError('expect must be open or blocked')


def probe(host, port, timeout=3.0, source_ip=None):
    if not isinstance(host, str) or not host or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('Invalid host/port')
    source = (source_ip, 0) if source_ip else None
    try:
        with socket.create_connection((host, port), timeout=timeout, source_address=source):
            return 'open'
    except (socket.timeout, TimeoutError):
        return 'timeout'
    except ConnectionRefusedError:
        return 'refused'
    except OSError as error:
        return 'unreachable' if error.errno in UNREACHABLE else 'error'


def local_context():
    host = socket.gethostname()
    addresses = []
    try:
        _, _, ips = socket.gethostbyname_ex(host)
        addresses = sorted(set(ips))
    except OSError:
        pass
    return {'hostname': host, 'addresses': addresses}


def run_plan(probes, timeout=3.0):
    if not isinstance(probes, list) or not 1 <= len(probes) <= 200:
        raise ValueError('Plan needs 1-200 probes')
    results = []
    for item in probes:
        if not isinstance(item, dict) or set(item) - {'host', 'port', 'expect', 'note'} or 'host' not in item or 'port' not in item or 'expect' not in item:
            raise ValueError('Each probe needs host, port, expect and optional note')
        if item['expect'] not in EXPECTED:
            raise ValueError('expect must be open or blocked')
        result = probe(item['host'], item['port'], timeout)
        results.append({'host': item['host'], 'port': item['port'], 'expect': item['expect'],
                        'result': result, 'verdict': classify(item['expect'], result), 'note': item.get('note', '')})
    passed = sum(1 for r in results if r['verdict'] == 'PASS')
    return {'generated_at': datetime.now(timezone.utc).isoformat(), 'source': local_context(),
            'probes': results, 'summary': {'total': len(results), 'pass': passed, 'fail': len(results) - passed}}


def resolve(host):
    try:
        infos = socket.getaddrinfo(host, None)
        return {'host': host, 'addresses': sorted({info[4][0] for info in infos}), 'result': 'resolved'}
    except socket.gaierror as error:
        return {'host': host, 'addresses': [], 'result': 'dns_error', 'error': str(error)}


def tls_probe(host, port, ca_file, timeout=5.0, cert_file=None, key_file=None, source_ip=None):
    """NATS sends a plaintext INFO first, then upgrades; mirror that before verifying the cert."""
    import ssl
    context = ssl.create_default_context(cafile=str(ca_file))
    if cert_file:
        context.load_cert_chain(str(cert_file), str(key_file) if key_file else None)
    source = (source_ip, 0) if source_ip else None
    raw = None
    try:
        raw = socket.create_connection((host, port), timeout=timeout, source_address=source)
        raw.settimeout(timeout)
        try:
            peek = raw.recv(1, socket.MSG_PEEK)
        except OSError:
            peek = b''
        if peek == b'I':
            buffer = b''
            while not buffer.endswith(b'\r\n'):
                chunk = raw.recv(1)
                if not chunk:
                    break
                buffer += chunk
        with context.wrap_socket(raw, server_hostname=host) as tls:
            cert = tls.getpeercert()
        raw = None
        subject = [f'{k}={v}' for rdn in (cert.get('subject') or ()) for k, v in rdn]
        san = [value for _, value in (cert.get('subjectAltName') or ())]
        return {'result': 'tls_ok', 'subject': subject, 'san': san, 'not_after': cert.get('notAfter')}
    except ssl.SSLCertVerificationError as error:
        return {'result': 'tls_error', 'error': 'verify failed: ' + str(getattr(error, 'verify_message', error))}
    except (socket.timeout, TimeoutError):
        return {'result': 'timeout'}
    except OSError as error:
        return {'result': 'error', 'error': str(error)}
    finally:
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass


def node_check(nodes, timeout=5.0):
    """NET-02: per-node DNS, port and certificate check; flags advertised-vs-resolved mismatch."""
    results = []
    for node in nodes:
        host, port, expected = node.get('host'), node.get('port'), node.get('address')
        entry = {'host': host, 'port': port, 'dns': resolve(host)}
        if expected and expected not in entry['dns']['addresses']:
            entry['result'] = 'MISMATCH'
            entry['detail'] = f'expected {expected}, resolved {entry["dns"]["addresses"]}'
            results.append(entry); continue
        if entry['dns']['result'] != 'resolved':
            entry['result'] = 'DNS_FAIL'; results.append(entry); continue
        entry['tcp'] = probe(host, port, timeout) if port else 'open'
        if entry['tcp'] != 'open':
            entry['result'] = 'TCP_FAIL'; results.append(entry); continue
        if node.get('ca'):
            entry['tls'] = tls_probe(host, port, node['ca'], timeout, node.get('cert'), node.get('key'))
            if entry['tls']['result'] != 'tls_ok':
                entry['result'] = 'TLS_FAIL'; results.append(entry); continue
        entry['result'] = 'OK'
        results.append(entry)
    passed = sum(1 for row in results if row['result'] == 'OK')
    return {'generated_at': datetime.now(timezone.utc).isoformat(), 'source': local_context(), 'nodes': results,
            'summary': {'total': len(results), 'pass': passed, 'fail': len(results) - passed}}


def load_plan(path):
    data = json.loads(open(path, encoding='utf-8').read())
    return data['probes'] if isinstance(data, dict) else data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', help='JSON file: list of probes or {"probes":[...]}')
    parser.add_argument('--node-plan', help='JSON file: list of nodes or {"nodes":[{host,port,ca,address}]} (NET-02)')
    parser.add_argument('--host'); parser.add_argument('--port', type=int)
    parser.add_argument('--expect', choices=sorted(EXPECTED)); parser.add_argument('--note', default='')
    parser.add_argument('--timeout', type=float, default=3.0)
    parser.add_argument('--out')
    args = parser.parse_args()
    if args.node_plan:
        data = json.loads(open(args.node_plan, encoding='utf-8').read())
        nodes = data['nodes'] if isinstance(data, dict) else data
        report = node_check(nodes, args.timeout)
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, 'w', encoding='utf-8') as handle:
                handle.write(text)
        print(text)
        return 1 if report['summary']['fail'] else 0
    if args.plan:
        probes = load_plan(args.plan)
    elif args.host and args.port and args.expect:
        probes = [{'host': args.host, 'port': args.port, 'expect': args.expect, 'note': args.note}]
    else:
        parser.error('provide --plan or --host/--port/--expect')
    report = run_plan(probes, args.timeout)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as handle:
            handle.write(text)
    print(text)
    return 1 if report['summary']['fail'] else 0


if __name__ == '__main__':
    sys.exit(main())
