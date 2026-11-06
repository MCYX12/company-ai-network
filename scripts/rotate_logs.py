"""Size-based log rotation for the PoC runtime using copytruncate.

NATS/Router/agent processes keep their log file descriptors open, so renaming a
log would leave them writing to the old inode. copytruncate instead copies the
file to .1 and truncates the original in place, which those appenders handle.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys


def rotate(runtime, max_bytes=5 * 1024 * 1024, keep=5, dry_run=False):
    runtime = Path(runtime).resolve()
    if max_bytes <= 0:
        raise ValueError('max_bytes must be positive')
    if keep < 1:
        raise ValueError('keep must be positive')
    results = []
    with (runtime / '.logrotate.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for path in sorted(runtime.glob('*.log')):
            if path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
            if size <= max_bytes:
                continue
            entry = {'file': path.name, 'bytes': size, 'rotated': False}
            if dry_run:
                results.append(entry)
                continue
            if keep > 1:
                oldest = runtime / f'{path.name}.{keep}'
                if oldest.exists():
                    oldest.unlink()
                for index in range(keep - 1, 0, -1):
                    source = runtime / f'{path.name}.{index}'
                    if source.exists():
                        os.replace(source, runtime / f'{path.name}.{index + 1}')
            backup = runtime / f'{path.name}.1'
            shutil.copy2(path, backup)
            with open(path, 'r+b') as handle:
                handle.truncate(0)
                handle.flush()
                os.fsync(handle.fileno())
            entry['rotated'] = True
            entry['backup'] = backup.name
            results.append(entry)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--max-bytes', type=int, default=5 * 1024 * 1024)
    parser.add_argument('--keep', type=int, default=5)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    print(json.dumps(rotate(args.runtime, args.max_bytes, args.keep, args.dry_run), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.exit(main())
