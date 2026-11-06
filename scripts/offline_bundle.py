"""Offline bundle manifest: pinned versions and checksums the runtime must carry.

The platform must run with no public-network dependency. This records what a site
bundle contains (NATS binary, Python dependencies, CA material, offline root) and
where the remaining gaps are (internal model, offline IAM/DNS/NTP/artifact mirror).
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import platform
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DEPS = ['nats-py', 'jsonschema', 'cryptography']


def _sha(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _nats_version(binary):
    try:
        output = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=10)
        return output.stdout.strip() or output.stderr.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def manifest(root=ROOT):
    root = Path(root)
    binary = root / '.runtime/bin/nats-server'
    deps = {}
    for name in DEPS:
        try:
            deps[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            deps[name] = 'missing'
    return {
        'format': 1,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'python': platform.python_version(),
        'dependencies': deps,
        'nats_server': {'path': str(binary), 'version': _nats_version(binary), 'sha256': _sha(binary)},
        'pyproject_sha256': _sha(root / 'pyproject.toml'),
        'must_be_available_offline': [
            'NATS server binary (pinned version + checksum)',
            'Python dependencies (nats-py / jsonschema / cryptography)',
            'enterprise CA material on the issuing host and each terminal',
            'offline root on removable media or HSM (never on the issuing host)',
        ],
        'known_gaps': [
            'internal model / weights (answers currently use an external model API)',
            'offline IAM / SSO, DNS, NTP and artifact mirror',
        ],
    }


def valid_artifact_metadata(data):
    """A missing checksum is never evidence that a missing artifact matches."""
    if not isinstance(data, dict) or type(data.get('format')) is not int or data['format'] != 1:
        return False
    nats = data.get('nats_server')
    checksum = nats.get('sha256') if isinstance(nats, dict) else None
    return isinstance(checksum, str) and re.fullmatch(r'[0-9a-f]{64}', checksum) is not None


def verify(data, root=ROOT):
    if not valid_artifact_metadata(data):
        return False
    binary = Path(root) / '.runtime/bin/nats-server'
    return binary.is_file() and data['nats_server']['sha256'] == _sha(binary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check', type=Path, help='verify an existing manifest')
    args = parser.parse_args()
    if args.check:
        try:
            data = json.loads(args.check.read_text())
            ok = verify(data, args.root)
        except (OSError, ValueError):
            ok = False
        print(json.dumps({'manifest': str(args.check), 'nats_server_ok': ok}, ensure_ascii=False))
        return 0 if ok else 1
    data = manifest(args.root)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + '\n')
        print(json.dumps({'manifest': str(args.output), 'nats_server_ok': verify(data, args.root)}, ensure_ascii=False))
    else:
        print(text)


if __name__ == '__main__':
    raise SystemExit(main())
