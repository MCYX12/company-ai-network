"""Native JetStream stream/KV snapshots; restore only into an empty isolated account.

Protocol: https://docs.nats.io/reference/reference-protocols/nats_api_reference
Snapshots include consumers. Cross-stream/SQLite capture is NOT a global transaction.
Keep workers disconnected until restored authority and uncertain effects are reconciled.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STREAM = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
DEFAULT_STREAMS = ('TASKS', 'TASKSDLQ', 'KV_poc_tasks', 'KV_poc_leases')


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('Snapshot directory cannot be a symlink')
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('format') != 'jetstream-native-v1' or not manifest.get('streams'):
        raise ValueError('Invalid JetStream snapshot manifest')
    seen = set()
    for entry in manifest['streams']:
        name = entry['name']
        if not STREAM.fullmatch(name) or name in seen or entry['file'] != name + '.snap':
            raise ValueError('Invalid or duplicate stream path')
        seen.add(name)
        path = directory / entry['file']
        if path.is_symlink() or not path.is_file() or checksum(path) != entry['sha256']:
            raise ValueError('JetStream snapshot checksum mismatch')
        if path.stat().st_size != entry['bytes'] or entry['config'].get('name') != name:
            raise ValueError('JetStream snapshot metadata mismatch')
    return manifest


async def snapshot(nc, directory, streams=DEFAULT_STREAMS, timeout=60):
    directory = Path(directory)
    if directory.exists():
        raise ValueError('JetStream snapshot destination already exists')
    if not streams or len(set(streams)) != len(streams) or not all(STREAM.fullmatch(s) for s in streams):
        raise ValueError('Explicit unique stream names required')
    directory.mkdir(parents=True, mode=0o700)
    entries = []
    for name in streams:
        sub = await nc.subscribe(nc.new_inbox(), pending_bytes_limit=64 * 1024 * 1024)
        try:
            await nc.flush()
            response = await nc.request('$JS.API.STREAM.SNAPSHOT.' + name,
                                        json.dumps({'deliver_subject': sub.subject, 'chunk_size': 65536,
                                                    'jsck': True}).encode(), timeout=timeout)
            info = json.loads(response.data)
            if info.get('error'):
                raise ValueError('Stream snapshot failed: ' + str(info['error']))
            path = directory / (name + '.snap')
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                while True:
                    chunk = await sub.next_msg(timeout=timeout)
                    if chunk.data:
                        handle.write(chunk.data)
                    if chunk.reply:
                        await chunk.respond(b'')
                    if not chunk.data:
                        break
                handle.flush(); os.fsync(handle.fileno())
            entries.append({'name': name, 'file': path.name, 'config': info['config'],
                            'state': info['state'], 'sha256': checksum(path), 'bytes': path.stat().st_size})
        finally:
            await sub.unsubscribe()
    manifest = {'format': 'jetstream-native-v1', 'created_at': datetime.now(timezone.utc).isoformat(),
                'streams': entries, 'consistency': 'independent stream snapshots; reconcile cross-store changes'}
    fd = os.open(directory / 'manifest.json', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(json.dumps(manifest, indent=2) + '\n'); handle.flush(); os.fsync(handle.fileno())
    return verify(directory)


async def restore(nc, directory, isolated=False, timeout=60):
    if not isolated:
        raise ValueError('Explicit isolated target required; never restore into a live account')
    manifest = verify(directory)
    js = nc.jetstream()
    if await js.streams_info():
        raise ValueError('Restore target account must contain no streams')
    restored = []
    for entry in manifest['streams']:
        response = await nc.request('$JS.API.STREAM.RESTORE.' + entry['name'],
                                    json.dumps({'config': entry['config'], 'state': entry['state']}).encode(), timeout=timeout)
        info = json.loads(response.data)
        if info.get('error') or not info.get('deliver_subject'):
            raise ValueError('Stream restore refused: ' + str(info))
        subject = info['deliver_subject']
        with (Path(directory) / entry['file']).open('rb') as handle:
            for block in iter(lambda: handle.read(65536), b''):
                ack = await nc.request(subject, block, timeout=timeout)
                if ack.data:
                    raise ValueError('Stream restore chunk rejected: ' + ack.data.decode())
        completed = json.loads((await nc.request(subject, b'', timeout=timeout)).data)
        if completed.get('error'):
            raise ValueError('Stream restore failed: ' + str(completed['error']))
        actual = await js.stream_info(entry['name'])
        if actual.state.last_seq != entry['state']['last_seq']:
            raise ValueError('Restored stream sequence mismatch')
        restored.append(entry['name'])
    held = 0
    if 'KV_poc_tasks' in restored:
        kv = await js.key_value('poc_tasks')
        from nats.js.errors import NoKeysError
        try:
            keys = await kv.keys()
        except NoKeysError:
            keys = []
        for key in keys:
            if not key.startswith('t.'):
                continue
            row = json.loads((await kv.get(key)).value)
            if row.get('state') not in ('succeeded', 'failed', 'expired', 'cancelled'):
                row.update(state='failed', retry_pending=False,
                           result=json.dumps({'code': 'RESTORE_REVIEW_REQUIRED', 'data': None}))
                row.setdefault('audit', []).append({'event': 'restore_review_required',
                                                    'timestamp': __import__('time').time()})
                await kv.put(key, json.dumps(row).encode()); held += 1
    if 'KV_poc_leases' in restored:
        kv = await js.key_value('poc_leases')
        from nats.js.errors import NoKeysError
        try:
            keys = await kv.keys()
        except NoKeysError:
            keys = []
        for key in keys:
            row = json.loads((await kv.get(key)).value)
            row.update(expires=0, holder='restore-review', token='')
            await kv.put(key, json.dumps(row).encode())
    return {'restored_streams': restored, 'pending_tasks_held': held, 'automatic_replay': False,
            'note': 'Keep workers disconnected; reconcile current authorization and post-snapshot effects before reconnecting'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('snapshot', 'verify', 'restore'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--identity', default='router')
    parser.add_argument('--streams', default=','.join(DEFAULT_STREAMS))
    parser.add_argument('--isolated-target', action='store_true')
    args = parser.parse_args()
    async def run():
        from app.common import connect
        nc = await connect(args.identity, timeout=10)
        try:
            return await snapshot(nc, args.directory, args.streams.split(',')) if args.command == 'snapshot' else await restore(nc, args.directory, args.isolated_target)
        finally:
            await nc.close()
    try:
        result = verify(args.directory) if args.command == 'verify' else asyncio.run(run())
    except (ValueError, OSError, asyncio.TimeoutError) as error:
        print(json.dumps({'ok': False, 'error': str(error)})); return 1
    print(json.dumps(result, indent=2)); return 0


if __name__ == '__main__':
    raise SystemExit(main())
