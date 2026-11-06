"""Software bill of materials and artifact manifest for the offline bundle (plan section 17)."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _sha(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _license(meta):
    value = (meta.get('License') or '').strip()
    if value and value.upper() not in {'UNKNOWN', 'NONE'}:
        return value
    for classifier in meta.get_all('Classifier') or []:
        if classifier.startswith('License ::'):
            return classifier.split('::')[-1].strip()
    return 'UNKNOWN'


def components():
    rows = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata['Name']
        if not name:
            continue
        rows.append({'name': name, 'version': dist.version, 'license': _license(dist.metadata)})
    return sorted(rows, key=lambda row: row['name'].lower())


def _docker_digest(image='company-agent-offline'):
    try:
        out = subprocess.run(['docker', 'image', 'inspect', image, '--format', '{{json .RepoDigests}}'],
                             capture_output=True, text=True, timeout=20)
        digests = json.loads(out.stdout or '[]')
        return digests[0] if digests else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def build(root=ROOT, image='company-agent-offline'):
    root = Path(root)
    binary = root / '.runtime/bin/nats-server'
    version = None
    if binary.exists():
        try:
            version = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            version = None
    artifacts = {}
    for name, path in [('pyproject.toml', root / 'pyproject.toml'), ('offline Dockerfile', root / 'offline-bundle/Dockerfile'),
                       ('nats-server', binary), ('offline manifest', root / 'offline-bundle/manifest.json'),
                       ('site example', root / 'config/site.example.json')]:
        artifacts[name] = {'path': str(path), 'sha256': _sha(path)}
    return {'format': 1, 'generated_at': datetime.now(timezone.utc).isoformat(), 'python': platform.python_version(),
            'components': components(),
            'nats_server': {'version': version, 'sha256': _sha(binary)},
            'docker_image': {'name': image, 'digest': _docker_digest(image)},
            'artifacts': artifacts}


def verify(data, root=ROOT):
    from scripts.offline_bundle import verify as verify_binary
    return verify_binary(data, root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check', type=Path)
    args = parser.parse_args()
    if args.check:
        try:
            data = json.loads(args.check.read_text())
            ok = verify(data, args.root)
        except (OSError, ValueError):
            ok = False
        print(json.dumps({'sbom': str(args.check), 'nats_server_ok': ok}, ensure_ascii=False))
        return 0 if ok else 1
    data = build(args.root)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + '\n')
        print(json.dumps({'sbom': str(args.output), 'components': len(data['components']),
                          'nats_server_ok': verify(data, args.root)}, ensure_ascii=False))
    else:
        print(text)


if __name__ == '__main__':
    raise SystemExit(main())
