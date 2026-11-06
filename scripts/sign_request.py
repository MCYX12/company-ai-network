"""Admin side: sign a terminal CSR and export the certificate chain (no private key)."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ca_tool


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ca-dir', type=Path, required=True)
    parser.add_argument('--csr', type=Path, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--kind', choices=['client', 'server', 'ocsp'], default='client')
    parser.add_argument('--days', type=int, default=365)
    parser.add_argument('--server-san', default='DNS:localhost,IP:127.0.0.1')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    result = ca_tool.sign_csr(args.ca_dir, args.name, args.kind, args.csr, days=args.days,
                              server_san=args.server_san)
    leaf = Path(result['cert']).read_bytes()
    intermediate = (args.ca_dir / 'intermediate/intermediate.crt').read_bytes()
    args.out.mkdir(parents=True, exist_ok=True)
    cert_path = args.out / f'{args.name}.crt'
    chain_path = args.out / f'{args.name}-chain.crt'
    cert_path.write_bytes(leaf)
    chain_path.write_bytes(leaf + intermediate)
    print(json.dumps({'cert': str(cert_path), 'chain': str(chain_path)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
