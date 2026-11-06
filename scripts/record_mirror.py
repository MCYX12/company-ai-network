"""Mirror the published project record to remote devices, with a durable pending list.

run_record_refresh.sh used to print SKIP when scp or the remote placement failed and then forget
it: after the network recovered the remote copy stayed old until someone noticed. This keeps a
pending-sync state file, retries every cycle (even when the local record did not change), verifies
the remote sha256, and reports what is still unsynced, so a transient failure cannot silently
leave a device answering from an old snapshot.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def local_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def parse_targets(path):
    rows = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        host, _, directory = line.partition('|')
        host, directory = host.strip(), directory.strip()
        if host:
            rows.append((host, directory))
    return rows


def load_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    pending = data.get('pending') if isinstance(data, dict) else None
    return {'pending': pending if isinstance(pending, dict) else {}}


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2) + '\n'
    os.umask(0o077)
    handle, name = tempfile.mkstemp(prefix='.mirror-', dir=path.parent)
    with os.fdopen(handle, 'w') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(name, 0o600)
    os.replace(name, path)
    return path


def _run(command, timeout=30):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    return result.returncode, (result.stdout + result.stderr).strip()


def transfer(host, directory, record, runner):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    remote_tmp = f'/tmp/company-record-{stamp}.json'
    name = Path(record).name
    rc, out = runner(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                      str(record), f'{host}:{remote_tmp}'])
    if rc != 0:
        return False, 'scp failed: ' + out[-120:]
    script = (f"set -e; mkdir -p '{directory}'; cd '{directory}'; "
              f"[ -f {name} ] && cp -p {name} {name}.bak-{stamp} || true; "
              f"mv '{remote_tmp}' {name}; chmod 600 {name}")
    rc, out = runner(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', host, script])
    if rc != 0:
        return False, 'place failed: ' + out[-120:]
    return True, None


def remote_digest(host, directory, name, runner):
    command = f"sha256sum '{directory}/{name}' 2>/dev/null"
    rc, out = runner(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', host, command])
    if rc != 0 or not out.strip():
        return ''
    return out.splitlines()[-1].split()[0].strip()


def mirror(record, targets, state_path, runner=_run):
    """Push the record to every target, retrying and verifying; persist what is still pending."""
    record = Path(record)
    digest = local_digest(record)
    state = load_state(state_path)
    report = []
    for host, directory in targets:
        pending = state['pending'].get(host) or {}
        directory = directory or pending.get('dir')
        if not directory:
            report.append({'host': host, 'status': 'skipped', 'detail': 'no remote dir configured'})
            continue
        ok, detail = transfer(host, directory, record, runner)
        if not ok:
            state['pending'][host] = {'dir': directory, 'digest': digest, 'detail': detail}
            report.append({'host': host, 'status': 'pending', 'detail': detail})
            continue
        seen = remote_digest(host, directory, record.name, runner)
        if seen != digest:
            state['pending'][host] = {'dir': directory, 'digest': digest, 'detail': 'remote digest mismatch'}
            report.append({'host': host, 'status': 'pending', 'detail': 'remote digest mismatch'})
            continue
        state['pending'].pop(host, None)
        report.append({'host': host, 'status': 'pushed', 'digest': digest[:12]})
    save_state(state_path, state)
    return {'record': str(record), 'digest': digest, 'report': report,
            'pending': sorted(state['pending']), 'synced': not state['pending']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record', type=Path, required=True)
    parser.add_argument('--targets', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    targets = parse_targets(args.targets)
    if not targets:
        print(json.dumps({'synced': True, 'report': [], 'pending': []}, ensure_ascii=False))
        return 0
    result = mirror(args.record, targets, args.state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['synced'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
