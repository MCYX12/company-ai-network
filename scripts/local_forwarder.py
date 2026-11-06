"""Proxy-isolated local forwarder.

Listens on loopback (never captured by a proxy TUN) and forwards to the platform server
through a socket pinned to a chosen interface, so Clash/aTrust route changes cannot capture
the platform traffic. If no interface is given it probes candidates and picks a working one.
"""
import argparse
import selectors
import socket
import struct
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

IP_BOUND_IF = 25  # macOS IPPROTO_IP / Linux SOL_SOCKET share this option number


def pin(sock, iface):
    """Force a socket out a specific interface, bypassing route lookup (and proxy TUNs)."""
    index = socket.if_nametoindex(iface)
    if sys.platform == 'darwin':
        sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, struct.pack('I', index))
    else:
        sock.setsockopt(socket.SOL_SOCKET, IP_BOUND_IF, iface.encode() + b'\0')


def candidates(host, port, timeout=2.0):
    """Interfaces (with a usable source address) that can reach the target, best first."""
    from scripts.net_scenarios import interfaces
    from scripts.net_precheck import probe
    found = []
    for row in interfaces():
        if row['status'] not in ('up', 'UP') or not row['addresses']:
            continue
        for ip in row['addresses']:
            if probe(host, port, timeout, source_ip=ip) == 'open':
                found.append({'device': row['device'], 'kind': row['kind'], 'source_ip': ip})
                break
    order = {'wired': 0, 'wifi': 1, 'vpn': 2, 'other': 3}
    return sorted(found, key=lambda item: order.get(item['kind'], 9))


def pick(host, port, prefer=None):
    found = candidates(host, port)
    if prefer:
        for item in found:
            if item['device'] == prefer:
                return item
    return found[0] if found else None


def _pump(client, target):
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, target)
    selector.register(target, selectors.EVENT_READ, client)
    try:
        while True:
            try:
                events = selector.select(timeout=30)
            except OSError:
                return
            for key, _ in events:
                try:
                    data = key.fileobj.recv(65536)
                    if not data:
                        return
                    key.data.sendall(data)
                except OSError:
                    return
    finally:
        selector.close()


def _connect(host, port, order, require_nats=True, timeout=3.0):
    """Try each interface in order; only accept one that really returns the NATS INFO line.

    A proxy TUN can make a TCP connect look successful while blackholing the traffic, so a
    plain connect is not enough: we peek for the NATS INFO byte before accepting the egress.
    """
    for device in order:
        sock = socket.socket()
        try:
            if device:
                pin(sock, device)
            sock.settimeout(timeout)
            sock.connect((host, port))
            if require_nats and sock.recv(1, socket.MSG_PEEK) != b'I':
                sock.close()
                continue
            sock.settimeout(None)
            return sock
        except OSError:
            sock.close()
    return None


def candidate_order(host, port, ifaces=None):
    """Explicit interface preference first; otherwise probe, then the default route as last resort."""
    if ifaces:
        return list(ifaces)
    order = [item['device'] for item in candidates(host, port)]
    order.append(None)
    return order


def _handle(client, host, port, ifaces, require_nats):
    """One thread per connection: a long-lived NATS session must not block new accepts."""
    target = _connect(host, port, candidate_order(host, port, ifaces), require_nats)
    if target is None:
        client.close(); return
    try:
        _pump(client, target)
    except OSError:
        pass
    finally:
        client.close(); target.close()


def forward(listen_port, host, port, ifaces=None, listen_host='127.0.0.1', once=False, require_nats=True):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((listen_host, listen_port))
    server.listen(64)
    while True:
        client, _ = server.accept()
        # Re-evaluate interfaces per connection so Wi-Fi/wired/VPN changes are handled.
        threading.Thread(target=_handle, args=(client, host, port, ifaces, require_nats), daemon=True).start()
        if once:
            return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--iface', action='append', help='egress interface preference (repeatable; default: auto-pick)')
    parser.add_argument('--listen-port', type=int, default=14222)
    parser.add_argument('--listen-host', default='127.0.0.1')
    parser.add_argument('--pick-only', action='store_true')
    args = parser.parse_args()
    if args.pick_only:
        import json
        print(json.dumps({'candidates': candidates(args.host, args.port)}, ensure_ascii=False, indent=2))
        return
    print('forwarding tls://%s:%d -> %s:%d via %s' % (args.listen_host, args.listen_port, args.host, args.port,
                                                      ','.join(args.iface) if args.iface else 'auto'), flush=True)
    forward(args.listen_port, args.host, args.port, args.iface, args.listen_host)


if __name__ == '__main__':
    main()
