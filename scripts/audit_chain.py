"""Tamper-evident SHA-256 hash chain over a JSONL audit log (plan section 13)."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

GENESIS = '0' * 64


def _digest(previous, line):
    return hashlib.sha256((previous + '\n' + line).encode('utf-8')).hexdigest()


def _chain_path(path):
    path = Path(path)
    return path.with_suffix(path.suffix + '.chain.json')


def seal(path):
    path = Path(path)
    if not path.exists():
        raise ValueError('audit file not found: ' + str(path))
    previous = GENESIS
    records = 0
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        json.loads(line)
        previous = _digest(previous, line)
        records += 1
    chain = {'algorithm': 'sha256-chain', 'head': previous, 'records': records}
    _chain_path(path).write_text(json.dumps(chain, indent=2))
    return {'chain': str(_chain_path(path)), 'head': previous, 'records': records}


def verify(path, chain=None):
    path = Path(path)
    chain_path = Path(chain) if chain else _chain_path(path)
    expected = json.loads(chain_path.read_text())['head']
    previous = GENESIS
    records = 0
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        previous = _digest(previous, line)
        records += 1
    return {'head': previous, 'expected': expected, 'records': records, 'ok': previous == expected}


def export_anchor(path, out, chain=None):
    """Write the chain head to an independent anchor file (copy it off-host / to WORM)."""
    path = Path(path)
    chain_path = Path(chain) if chain else _chain_path(path)
    head = json.loads(chain_path.read_text())['head'] if chain_path.exists() else seal(path)['head']
    anchor = {'algorithm': 'sha256-chain', 'file': str(path.resolve()), 'head': head,
              'exported_at': datetime.now(timezone.utc).isoformat()}
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(anchor, indent=2))
    return {'anchor': str(out), 'head': head}


def check_anchor(path, anchor):
    """Recompute the chain head and compare it with the externally held anchor."""
    path = Path(path)
    expected = json.loads(Path(anchor).read_text())['head']
    previous = GENESIS
    records = 0
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        previous = _digest(previous, line)
        records += 1
    return {'head': previous, 'anchor': expected, 'records': records, 'ok': previous == expected}


def worm_log(worm_dir):
    return Path(worm_dir) / 'anchors.jsonl'


def _line_hash(line):
    return hashlib.sha256(line.encode('utf-8')).hexdigest()


def export_worm(path, worm_dir):
    """Append a chained anchor record to an append-only WORM log.

    Each record links to the previous raw line and stores the head over the first 'records'
    audit lines, so a later rewrite of those lines is detected even if the standalone anchor
    file is replaced.
    """
    path = Path(path)
    log = worm_log(worm_dir)
    log.parent.mkdir(parents=True, exist_ok=True)
    previous = GENESIS
    seq = 0
    if log.exists():
        for line in log.read_text(encoding='utf-8').splitlines():
            if line.strip():
                previous = _line_hash(line)
                seq += 1
    head = GENESIS
    records = 0
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        head = _digest(head, line)
        records += 1
    entry = {'seq': seq + 1, 'prev': previous, 'file': str(path.resolve()), 'head': head,
             'records': records, 'exported_at': datetime.now(timezone.utc).isoformat()}
    with log.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return {'worm': str(log), 'seq': entry['seq'], 'head': head, 'records': records}


def check_worm(path, worm_dir):
    """Verify the WORM chain and that no already-anchored audit prefix was rewritten."""
    path = Path(path)
    log = worm_log(worm_dir)
    entries = []
    previous = GENESIS
    problems = []
    if log.exists():
        for number, line in enumerate(log.read_text(encoding='utf-8').splitlines(), start=1):
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get('prev') != previous:
                problems.append('chain break at seq %s' % entry.get('seq'))
            if entry.get('seq') != number:
                problems.append('non-contiguous seq at line %s' % number)
            previous = _line_hash(line)
            entries.append(entry)
    mine = [entry for entry in entries if entry.get('file') == str(path.resolve())]
    ok = not problems and bool(mine)
    head = anchor_head = None
    if mine:
        # Verify EVERY historical commitment, not just the newest: appending a fresh anchor must
        # not be able to launder a rewrite of an earlier anchored prefix.
        current = path.read_text(encoding='utf-8').splitlines()
        head = GENESIS
        for entry in mine:
            prefix_head = GENESIS
            for line in current[:entry['records']]:
                if line.strip():
                    prefix_head = _digest(prefix_head, line)
            if prefix_head != entry['head']:
                ok = False
                problems.append('anchored prefix at seq %s was rewritten' % entry['seq'])
            if len(current) < entry['records']:
                ok = False
                problems.append('audit log truncated below seq %s' % entry['seq'])
        latest = mine[-1]
        anchor_head = latest['head']
        for line in current[:latest['records']]:
            if line.strip():
                head = _digest(head, line)
    return {'ok': ok, 'head': head, 'anchor': anchor_head, 'entries': len(entries),
            'problems': problems}


def push_command(target, path):
    return ['scp', str(path), target]


def push_anchor(anchor, target, timeout=30):
    """Send an anchor off-host (e.g. user@host:/var/anchor/). Keeps a local copy."""
    import subprocess
    try:
        result = subprocess.run(push_command(target, anchor), capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'target': target, 'returncode': None, 'error': 'timeout after %ss' % timeout}
    except OSError as error:
        return {'target': target, 'returncode': None, 'error': str(error)[:200]}
    return {'target': target, 'returncode': result.returncode,
            'error': (result.stderr or '').strip()[-200:] or None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['seal', 'verify', 'export', 'check', 'export-worm', 'check-worm'])
    parser.add_argument('--file', type=Path, required=True)
    parser.add_argument('--chain', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--anchor', type=Path)
    parser.add_argument('--push', help='scp the anchor off-host, e.g. user@host:/var/company-agent-anchors/')
    parser.add_argument('--worm', type=Path, help='append-only WORM anchor directory')
    args = parser.parse_args()
    if args.command == 'seal':
        result = seal(args.file)
    elif args.command == 'verify':
        result = verify(args.file, args.chain)
    elif args.command == 'export':
        result = export_anchor(args.file, args.out, args.chain)
        if args.push:
            result['pushed'] = push_anchor(result['anchor'], args.push)
    elif args.command == 'export-worm':
        result = export_worm(args.file, args.worm)
    elif args.command == 'check-worm':
        result = check_worm(args.file, args.worm)
    else:
        result = check_anchor(args.file, args.anchor)
    print(json.dumps(result, indent=2))
    pushed = result.get('pushed')
    push_failed = isinstance(pushed, dict) and pushed.get('returncode') != 0
    return 0 if (result.get('ok', True) and not push_failed) else 1


if __name__ == '__main__':
    raise SystemExit(main())
