"""Add required CA keyUsage, preserving CA key and issued leaf identities."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    args = parser.parse_args()
    certs = args.runtime.resolve()/'certs'
    os.umask(0o077)
    existing = subprocess.check_output(['openssl','x509','-in',str(certs/'ca.crt'),'-noout','-text'],text=True)
    if 'Certificate Sign, CRL Sign' in existing:
        print('CA already has required key usage; unchanged.')
        return
    candidate = certs/'ca.repaired.crt'
    subprocess.run(['openssl','req','-x509','-key',str(certs/'ca.key'),'-days','7',
                    '-out',str(candidate),'-subj','/CN=Local-PoC-CA',
                    '-addext','basicConstraints=critical,CA:TRUE',
                    '-addext','keyUsage=critical,keyCertSign,cRLSign'],check=True)
    try:
        for identity in ['server','router','ag-a','ag-b','ag-c','unregistered']:
            subprocess.run(['openssl','verify','-x509_strict','-CAfile',str(candidate),
                            '-purpose','sslserver' if identity=='server' else 'sslclient',
                            str(certs/f'{identity}.crt')],check=True)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        shutil.copy2(certs/'ca.crt',certs/f'ca.before-keyusage-{stamp}.crt')
        candidate.replace(certs/'ca.crt')
    finally:
        candidate.unlink(missing_ok=True)
    print('Repaired CA certificate; CA private key and all issued leaf identities preserved.')

if __name__=='__main__':
    main()
