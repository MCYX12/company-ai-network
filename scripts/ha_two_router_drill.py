"""Two-process shared-authority drill (local, no second host).

    python -m scripts.ha_two_router_drill [--workdir DIR]

A stale LOCAL approval is written into Router A's own runtime. Process B approves then revokes on
the shared POC_AUTHZ_RUNTIME. A fresh process C, with BOTH POC_RUNTIME=<local> and
POC_AUTHZ_RUNTIME=<shared>, must read DENY from the Router policy store, the authz facade and the
approved() helper: the shared authority wins over the local copy. This is the property that was
missing (a local stale approval could still allow). It is not a two-host/partition drill.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DRIVER = textwrap.dedent('''
    import json, os, sys
    sys.path.insert(0, {root!r})
    from app import authz, conversation_access
    action = sys.argv[1]
    store = conversation_access.AccessStore()
    def snapshot():
        return {{'policy': store.permits('mac-codex-01', 'ag-a', ['demo-project']),
                 'facade': authz.decide('mac-codex-01', 'ag-a', 'conversation',
                                        projects=['demo-project'])['allowed'],
                 'approved': conversation_access.approved('mac-codex-01', 'ag-a', ['demo-project'])}}
    if action in ('approve', 'approve-local'):
        row = store.request('mac-codex-01', 'ag-a', ['demo-project'])
        store.decide(row['approval_id'], 'approved', 3600, 'drill')
    elif action == 'revoke':
        row = store.request('mac-codex-01', 'ag-a', ['demo-project'])
        store.decide(row['approval_id'], 'revoked', 0, 'drill')
    print(json.dumps(snapshot()))
''')


def _run(action, local, shared=None):
    env = dict(os.environ)
    env['POC_RUNTIME'] = str(local)
    if shared is None:
        env.pop('POC_AUTHZ_RUNTIME', None)
    else:
        env['POC_AUTHZ_RUNTIME'] = str(shared)
    result = subprocess.run([sys.executable, '-c', DRIVER.format(root=str(ROOT)), action],
                            capture_output=True, text=True, env=env, timeout=60)
    if result.returncode != 0:
        raise RuntimeError('driver %s failed: %s' % (action, result.stderr.strip()[-300:]))
    return json.loads(result.stdout.strip().splitlines()[-1])


def run(workdir=None):
    temporary = workdir is None
    workdir = Path(workdir or tempfile.mkdtemp(prefix='ha-drill-'))
    local = workdir / 'local-A'
    shared = workdir / 'shared'
    local.mkdir(parents=True, exist_ok=True)
    shared.mkdir(parents=True, exist_ok=True)
    stale = _run('approve-local', local)                 # approvals only in Router A's runtime
    _run('approve', local, shared)
    revoked = _run('revoke', local, shared)
    observed = _run('observe', local, shared)            # both env vars set, as a real Router would
    result = {'local': str(local), 'shared': str(shared),
              'stale_local_policy': stale['policy'],
              'shared_revoked_policy': revoked['policy'],
              'observed_policy': observed['policy'],
              'observed_facade': observed['facade'],
              'observed_approved': observed['approved'],
              'agreement': not (observed['policy'] or observed['facade'] or observed['approved']),
              'note': 'local two-process drill; a physical two-host / partition drill is still external'}
    if temporary:
        shutil.rmtree(workdir, ignore_errors=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', type=Path)
    args = parser.parse_args()
    result = run(args.workdir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (result['agreement'] and result['stale_local_policy']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
