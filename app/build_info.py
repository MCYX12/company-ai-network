"""Which build is actually running, surfaced in heartbeats and the directory.

Three versions were conflated before: the git source revision, the released artifact version
(release/RELEASE.json) and the build actually deployed on a device. A heartbeat carried none of
them, so the dashboard could not tell "source 223cd88" from "deployed 1e22573" from
"0.9.0-poc". This reads the existing release manifest (no second version system) and hashes the
running app code, so a heartbeat can carry a real fingerprint.
"""
import hashlib
import json
import subprocess
from pathlib import Path

from .common import ROOT
from .worker_kernel import CONTRACT_VERSION

# Semantics version of the authorization decision vocabulary (history tri-state, deny/scope
# recheck on stored results). Kept next to the contract version so one heartbeat shows both.
AUTHZ_VERSION = 'authz-2026-10-05'

_CACHE = {}


def source_revision(root=ROOT):
    try:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--short', 'HEAD'],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def released_version(root=ROOT):
    try:
        data = json.loads((Path(root) / 'release/RELEASE.json').read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    version = data.get('version')
    return version if isinstance(version, str) and version else None


def running_digest(root=ROOT):
    """Content digest of the running app code: a deployed build different from the source."""
    root = Path(root)
    digest = hashlib.sha256()
    files = sorted(path for path in (root / 'app').rglob('*.py') if '__pycache__' not in path.parts)
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b'<unreadable>')
    return digest.hexdigest()


def summary(root=ROOT):
    """One mapping for heartbeats/dashboard, cached because running code does not change."""
    root = Path(root)
    key = str(root)
    if key not in _CACHE:
        _CACHE[key] = {'source_revision': source_revision(root),
                       'released_version': released_version(root),
                       'running_digest': running_digest(root)[:16],
                       'contract_version': CONTRACT_VERSION,
                       'authz_version': AUTHZ_VERSION}
    return dict(_CACHE[key])
