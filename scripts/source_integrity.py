"""Hash release source independent of git, runtime credentials and generated evidence.

Ported from the public release candidate so this repo can commit a content-level digest
into release/RELEASE.json: a matching git commit is not enough when the manifest was built
from a dirty working tree.
"""
import hashlib
from pathlib import Path

DIRECTORIES = ('.github', 'app', 'scripts', 'schemas', 'config', 'web', 'packaging', 'tests', 'docs',
               'skills', 'infra', 'facts')
# `VERSIONS.md` is deliberately excluded: it describes the release itself (including the
# tag/commit), so hashing it would make every release note edit invalidate the digest.
# Generated release/ and offline-bundle/ metadata are excluded for the same reason.
# The offline Dockerfile is executable build source and is explicitly hashed below.
FILES = ('pyproject.toml', 'requirements.lock', 'uv.lock', 'README.md', '.gitignore',
         '.dockerignore', 'Dockerfile', 'compose.yaml', 'compose.lan.yaml', '.env.example',
         'NOTICE.md', 'offline-bundle/Dockerfile')
SKIP_SUFFIX = {'.pyc', '.key', '.log'}
SKIP_NAMES = {'nats-server', 'nats-server.exe', 'nats-server.sha256'}


def hashes(root):
    """Per-file SHA-256 of the release source, keyed by repo-relative POSIX path."""
    root = Path(root)
    paths = []
    for name in DIRECTORIES:
        directory = root / name
        if directory.is_dir():
            paths.extend(p for p in directory.rglob('*') if p.is_file()
                         and '__pycache__' not in p.parts and p.suffix not in SKIP_SUFFIX
                         and p.name not in SKIP_NAMES)
    paths.extend(root / name for name in FILES if (root / name).is_file())
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths)}
