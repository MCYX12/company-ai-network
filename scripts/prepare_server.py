"""Extract pinned official Linux NATS binary. Does not run a container service."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'nats@sha256:777787bbb4c7c4083ec2213b0b8b14151617610a3e0d85776bd995cd46599c73'

def main():
    if platform.system() != 'Linux':
        raise SystemExit('This helper is for Linux only. On macOS use a verified native nats-server and NATS_SERVER.')
    target = ROOT/'.runtime/bin/nats-server'
    if target.exists():
        subprocess.run([str(target), '--version'], check=True)
        print('Existing binary preserved.')
        return
    os.umask(0o077)
    target.parent.mkdir(parents=True, exist_ok=True)
    name = 'company-ai-extract-'+uuid.uuid4().hex[:12]
    subprocess.run(['docker', 'pull', IMAGE], check=True)
    subprocess.run(['docker', 'create', '--name', name, IMAGE], check=True, stdout=subprocess.DEVNULL)
    try:
        subprocess.run(['docker', 'cp', name+':/usr/local/bin/nats-server', str(target)], check=True)
    finally:
        subprocess.run(['docker', 'rm', name], check=True, stdout=subprocess.DEVNULL)
    target.chmod(0o700)
    subprocess.run([str(target), '--version'], check=True)
    (ROOT/'.runtime/server-artifact.json').write_text(json.dumps({
        'source_image': IMAGE, 'binary_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        'architecture': platform.machine()}, indent=2))

if __name__ == '__main__':
    main()
