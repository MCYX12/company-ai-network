"""Build a release manifest: version, git commit, pinned components and evidence levels."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VERSION = '0.9.0-poc'
EVIDENCE_LEVELS = ['设计', '已实现', '隔离验证', '实机验证', '现场验收']


def _git_commit(root):
    try:
        result = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=root,
                                capture_output=True, text=True, timeout=10)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def build(root=ROOT, version=VERSION):
    from scripts import sbom, source_integrity
    data = sbom.build(root)
    return {'product': 'company-agent-platform', 'version': version,
            'release_date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            'baseline': 'docs/BASELINE.md',
            'acceptance_matrix': 'docs/ACCEPTANCE_MATRIX.md',
            'remaining_work': 'docs/REMAINING_WORK.md',
            'python': data['python'],
            'nats_server': data['nats_server'],
            'dependencies': {c['name']: c['version'] for c in data['components']},
            'docker_image': data['docker_image'],
            'git_commit': _git_commit(root),
            'source_sha256': source_integrity.hashes(root),
            'evidence_levels': EVIDENCE_LEVELS}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--version', default=VERSION)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    data = build(args.root, args.version)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n')
        print(json.dumps({'release': str(args.output), 'version': data['version'], 'git_commit': data['git_commit']}, ensure_ascii=False))
    else:
        print(text)


if __name__ == '__main__':
    main()
