"""Explicit authorization/identity authority for multi-Router deployments.

A shared task ledger is not enough for HA. Conversation, directory, history and file grants, the
identity registry and replay state are read from local SQLite/JSON files; two Routers pointed at
different copies can disagree about a revoked grant, so an answer may be served from a stale
authorization view. This module makes the authority explicit instead of implicit:

* an HA deployment must declare one shared authorization runtime (POC_AUTHZ_SHARED=1); the
  health/ops path can then say whether that declaration is missing;
* authz_digest() computes a byte digest of the exact files a process reads, so an operator can
  prove two Routers read the same authoritative files (a copy with equal logical content but
  different bytes is still a different, unsynchronised authority);
* compare() reports divergence per file.

It does not rewrite the stores; it removes the ability to *believe* they are shared when they
are not.
"""
import hashlib
import json
import os
from pathlib import Path

# Everything an authz/identity decision depends on, in a fixed order.
STORE_FILES = ('registry.json', 'accounts.sqlite3', 'roster.sqlite3',
               'conversation-access.sqlite3', 'directory-requests.sqlite3',
               'history-access.sqlite3', 'file-access.sqlite3', 'replay.sqlite3')


def file_digest(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return '<missing>'
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return '<unreadable>'


def authz_digest(runtime):
    """One digest of the authorization/identity files a Router at runtime would read."""
    runtime = Path(runtime)
    digest = hashlib.sha256()
    for name in STORE_FILES:
        digest.update(name.encode())
        digest.update(file_digest(runtime / name).encode())
    return digest.hexdigest()


def authority(runtime, env=None):
    env = os.environ if env is None else env
    runtime = Path(runtime)
    return {'runtime': str(runtime),
            'shared_declared': env.get('POC_AUTHZ_SHARED') == '1',
            'digest': authz_digest(runtime),
            'files': {name: file_digest(runtime / name) for name in STORE_FILES}}


def ha_problems(runtime, env=None):
    """Reasons a POC_HA deployment has no single authorization authority, if any."""
    env = os.environ if env is None else env
    if env.get('POC_HA') != '1':
        return []
    problems = []
    shared_runtime = env.get('POC_AUTHZ_RUNTIME')
    if not shared_runtime:
        problems.append('POC_HA=1 但未设置 POC_AUTHZ_RUNTIME：'
                        '多 Router 没有共享授权权威目录，撤权/授权状态会不一致')
    elif not Path(shared_runtime).is_dir():
        problems.append('POC_AUTHZ_RUNTIME 指向的共享授权目录不存在或不可读：%s' % shared_runtime)
    if env.get('POC_AUTHZ_SHARED') != '1':
        problems.append('POC_HA=1 但未声明 POC_AUTHZ_SHARED=1：'
                        '多 Router 可能各自读取本地授权副本，撤权/授权状态会不一致')
    return problems


def compare(runtimes, env=None):
    """Compare the authorization view of two or more runtimes.

    Returns {'consistent': bool, 'digests': {runtime: digest}, 'divergent': [files]}.
    """
    views = [authority(runtime, env=env) for runtime in runtimes]
    digests = {view['runtime']: view['digest'] for view in views}
    divergent = set()
    missing = []
    for name in STORE_FILES:
        seen = {view['files'][name] for view in views}
        if len(seen) > 1:
            divergent.add(name)
        for view in views:
            state = view['files'][name]
            if state in ('<missing>', '<unreadable>'):
                missing.append({'runtime': view['runtime'], 'file': name, 'state': state})
    # A missing/unreadable authority file is NOT "consistent": absence is not agreement.
    return {'consistent': len(set(digests.values())) <= 1 and not divergent and not missing,
            'digests': digests, 'divergent': sorted(divergent), 'missing': missing,
            'views': views}
