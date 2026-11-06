"""Run the experimental A2A-compatible gateway.

    .venv/bin/python -m scripts.run_a2a_gateway --identity a2a-gateway --default-target mac-deepseek-01

The gateway identity must already be enrolled (it needs certs and the client
ACL). It binds loopback by default; set A2A_BEARER_TOKEN for a shared token, or
A2A_TLS_CERT/A2A_TLS_KEY (+ A2A_TLS_CA for mutual TLS) for transport security.
No new runtime dependency: stdlib HTTP + the existing nats client.
"""
import argparse
import os
import ssl
from http.server import ThreadingHTTPServer

from app.a2a_gateway import GatewayHTTPRequestHandler, NatsBridge


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=os.environ.get('A2A_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('A2A_PORT', '8099')))
    parser.add_argument('--identity', default=os.environ.get('A2A_IDENTITY', 'a2a-gateway'))
    parser.add_argument('--default-target', default=os.environ.get('A2A_DEFAULT_TARGET') or None)
    parser.add_argument('--ttl-seconds', type=int, default=int(os.environ.get('A2A_TASK_TTL_SECONDS', '120')))
    return parser


def tls_context():
    cert = os.environ.get('A2A_TLS_CERT')
    key = os.environ.get('A2A_TLS_KEY')
    if not (cert and key):
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    ca = os.environ.get('A2A_TLS_CA')
    if ca:
        context.load_verify_locations(ca)
        context.verify_mode = ssl.CERT_REQUIRED
    return context


def build_server(host, port, bridge, bearer_token=None):
    server = ThreadingHTTPServer((host, port), GatewayHTTPRequestHandler)
    server.bridge = bridge
    server.bearer_token = bearer_token
    context = tls_context()
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main():
    args = build_parser().parse_args()
    bridge = NatsBridge(args.identity, default_target=args.default_target, ttl_seconds=args.ttl_seconds)
    server = build_server(args.host, args.port, bridge, os.environ.get('A2A_BEARER_TOKEN') or None)
    scheme = 'https' if tls_context() is not None else 'http'
    print('A2A gateway on %s://%s:%d (identity=%s, target=%s)' %
          (scheme, args.host, server.server_address[1], args.identity, args.default_target or 'request-only'), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
