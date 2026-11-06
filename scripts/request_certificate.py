"""Generate a private key and CSR on the terminal; the key never leaves the device."""
import argparse
import json
import os
from pathlib import Path


def generate(out_dir, identity, common_name=None, key_size=2048):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    key_path = out / f'{identity}.key'
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    os.chmod(key_path, 0o600)
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name or identity)]))
           .sign(key, hashes.SHA256()))
    csr_path = out / f'{identity}.csr'
    csr_path.write_bytes(csr.public_bytes(serialization.Encoding.PEM))
    return {'key': str(key_path), 'csr': str(csr_path)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--identity', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--common-name')
    args = parser.parse_args()
    print(json.dumps(generate(args.out, args.identity, args.common_name), ensure_ascii=False))


if __name__ == '__main__':
    main()
