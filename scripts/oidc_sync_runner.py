"""Scheduled/one-shot OIDC identity sync with retries and an honest status snapshot.

    python -m scripts.oidc_sync_runner --runtime <runtime> --token-dir <dir> --apply

Reads every *.jwt in --token-dir, verifies/syncs each with retries, and writes
<runtime>/health/oidc-sync.json. Only a real roster write advances last_success; verification
uses last_verify; skipped (already applied / manual revocation wins / no status claim) and
failed are reported separately. A missing input directory is an explicit failure; an empty one
is 'empty'. Exit: 0 ok/verified/empty/skipped, 1 failed/partial, 2 no input directory.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.common import RUNTIME
from app.iam import OidcProvider, RosterStore
from app.oidc_status import valid as valid_status


def _atomic_write(path, text):
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if Path(name).exists():
            Path(name).unlink()


def run(runtime, token_dir, apply=False, retries=3, now=None):
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    token_dir = Path(token_dir)
    status_path = runtime / 'health' / 'oidc-sync.json'
    previous_error=None
    try:
        previous = json.loads(status_path.read_text())
        if not valid_status(previous):
            previous={};previous_error='OIDC_SNAPSHOT_INVALID'
    except FileNotFoundError:
        previous = {}
    except (OSError, ValueError):
        previous={};previous_error='OIDC_SNAPSHOT_INVALID'
    snapshot = {'checked_at': time.time(), 'mode': 'apply' if apply else 'verify',
                'token_dir': str(token_dir), 'tokens': 0, 'applied': 0, 'verified': 0,
                'skipped': 0, 'failed': 0, 'results': [], 'input_error': None,
                'last_success': previous.get('last_success'), 'last_verify': previous.get('last_verify'),
                'last_error': None,'previous_snapshot_error':previous_error}
    try:
        # is_dir() itself raises when a parent directory denies access, so both calls must be
        # inside the same handler; otherwise the CLI crashes with rc=1 instead of no_input.
        if not token_dir.is_dir():
            raise FileNotFoundError('token directory not found: %s' % token_dir)
        entries = list(token_dir.iterdir())
    except OSError as error:
        snapshot.update(status='no_input', input_error='token directory unavailable: %s' % error)
        snapshot['last_error'] = snapshot['input_error']
        _write_status(status_path, snapshot)
        return snapshot
    tokens = sorted(path for path in entries if path.suffix == '.jwt')
    snapshot['tokens'] = len(tokens)
    for path in tokens:
        try:
            token = path.read_text().strip()
        except OSError as error:
            snapshot['failed'] += 1
            snapshot['results'].append({'token': path.name, 'outcome': 'failed', 'error': str(error)[:120]})
            continue
        attempts = 0
        outcome = None
        while attempts < max(1, retries):
            attempts += 1
            try:
                shared = os.environ.get('POC_AUTHZ_RUNTIME')
                roster = Path(shared) / 'roster.sqlite3' if shared else runtime / 'roster.sqlite3'
                provider = OidcProvider.from_environment(roster=RosterStore(roster))
                if apply:
                    result = provider.sync(token, now=now)
                    if result.get('synced'):
                        outcome = {'token': path.name, 'outcome': 'applied', 'attempts': attempts, **result}
                    else:
                        outcome = {'token': path.name, 'outcome': 'skipped', 'attempts': attempts,
                                   'reason': result.get('reason') or 'NO_CHANGE',
                                   'effective_status': result.get('effective_status'), **result}
                else:
                    claims = provider.verify(token, now=now)
                    outcome = {'token': path.name, 'outcome': 'verified', 'attempts': attempts,
                               'subject': claims.get(provider.subject_claim)}
                break
            except Exception as error:      # noqa: BLE001 - every failure kind is reported
                outcome = {'token': path.name, 'outcome': 'failed', 'attempts': attempts,
                           'error': '%s: %s' % (type(error).__name__, str(error)[:120])}
        snapshot['results'].append(outcome)
        snapshot[outcome['outcome']] = snapshot.get(outcome['outcome'], 0) + 1
    if snapshot['applied'] > 0:
        snapshot['last_success'] = time.time()
    if snapshot['verified'] > 0:
        snapshot['last_verify'] = time.time()
    failures = [item for item in snapshot['results'] if item['outcome'] == 'failed']
    snapshot['last_error'] = failures[-1]['error'] if failures else None
    if snapshot['failed']:
        snapshot['status'] = 'failed' if snapshot['applied'] == 0 and snapshot['verified'] == 0 else 'partial'
    elif snapshot['applied']:
        snapshot['status'] = 'ok'
    elif snapshot['verified']:
        snapshot['status'] = 'verified'
    elif snapshot['skipped']:
        snapshot['status'] = 'skipped'
    else:
        snapshot['status'] = 'empty'
    _write_status(status_path, snapshot)
    return snapshot


def _write_status(path, snapshot):
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(snapshot, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=RUNTIME)
    parser.add_argument('--token-dir', type=Path, required=True)
    parser.add_argument('--retries', type=int, default=3)
    parser.add_argument('--apply', action='store_true', help='write the roster (otherwise verify only)')
    args = parser.parse_args()
    snapshot = run(args.runtime, args.token_dir, args.apply, args.retries)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    if snapshot['status'] == 'no_input':
        return 2
    return 1 if snapshot['status'] in ('failed', 'partial') else 0


if __name__ == '__main__':
    raise SystemExit(main())
