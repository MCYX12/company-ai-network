"""HA seed drill: two Routers share a dispatch lease + replay ledger; kill the holder and
the survivor takes over with no duplicate execution."""
import asyncio
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def holder_pid(name):
    """PID embedded in a lease holder name built by app/router.py.

    The router names its holder ``<hostname>-<pid>-<random token>`` (see
    ``app/router.py``). The token is hex, so parsing the **last** segment as the PID
    fails silently (``int('ace99d8d')``); the drill then killed nothing and reported a
    missing takeover for the wrong reason. Parse the middle segment instead, and return
    None whenever the name does not carry a PID, so the caller can fail loudly.
    """
    if not isinstance(name, str):
        return None
    parts = name.rsplit('-', 2)
    if len(parts) != 3 or not parts[0]:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def holder(runtime):
    path = Path(runtime) / 'leases.sqlite3'
    if not path.exists():
        return None
    with sqlite3.connect(path) as db:
        try:
            row = db.execute("SELECT holder FROM leases WHERE name='dispatch'").fetchone()
        except sqlite3.OperationalError:
            return None
    return row[0] if row else None


async def main(runtime, evidence, port):
    from app.common import connect, query, request, encode, PREFIX
    from app.conversation_access import AccessStore
    from scripts.enroll_agent import enroll

    enroll(runtime, dict(agent_id='peer-x', device_id='mac-demo', employee_id='demo-b', tool='codex',
                         projects=['demo-project'], enabled=True))
    access = AccessStore(runtime / 'conversation-access.sqlite3')
    row = access.request('ag-a', 'peer-x', ['demo-project'])
    access.decide(row['approval_id'], 'approved', 3600, 'failover')

    env = {**os.environ, 'POC_RUNTIME': str(runtime), 'NATS_URL': f'tls://localhost:{port}',
           'POC_DISPATCH_LEASE': '1', 'POC_SHARED_REPLAY': '1', 'NATS_SERVER': str(ROOT / '.runtime/bin/nats-server')}
    nats_proc = subprocess.Popen([env['NATS_SERVER'], '-c', str(runtime / 'nats.conf')],
                                 stdout=(evidence / 'nats.log').open('w'), stderr=subprocess.STDOUT)
    time.sleep(2)
    routers = [subprocess.Popen([sys.executable, '-u', '-m', 'app.router'], cwd=ROOT, env=env,
                                stdout=(evidence / f'router{i}.log').open('w'), stderr=subprocess.STDOUT) for i in (1, 2)]
    time.sleep(3)

    async def quiet(*_):
        pass

    nc = await connect('ag-a', error_cb=quiet, disconnected_cb=quiet)
    peer = await connect('peer-x', error_cb=quiet, disconnected_cb=quiet)
    executed = {}

    async def on_task(message):
        body = __import__('app.common', fromlist=['decode']).decode(message.data)
        task_id = body['task_id']
        executed[task_id] = executed.get(task_id, 0) + 1
        await peer.publish(f'{PREFIX}.agent.peer-x.result', encode({'task_id': task_id, 'code': 'OK', 'data': {'text': 'ok'}}))

    await peer.subscribe(f'{PREFIX}.agent.peer-x.task', cb=on_task)

    async def heartbeat():
        while True:
            try:
                await peer.publish(f'{PREFIX}.agent.peer-x.heartbeat', encode({
                    'agent_id': 'peer-x', 'device_id': 'mac-demo', 'tool': 'codex', 'status': 'online',
                    'capabilities': ['get_status', 'ask_peer'], 'adapter_status': 'ready', 'task_actions': ['ask_peer']}))
            except Exception:
                pass
            await asyncio.sleep(1)

    beat = asyncio.create_task(heartbeat())

    async def submit(key):
        body = request('ag-a', 'peer-x'); body['action'] = 'submit_task'
        body['payload'] = {'operation': 'ask_peer', 'question': 'failover', 'idempotency_key': key, 'ttl_seconds': 120}
        answer = await query(nc, 'ag-a', body, timeout=10)
        return answer['data']['task_id']

    async def wait_done(task_id, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            body = request('ag-a'); body.pop('target_agent_id'); body['action'] = 'get_task'; body['payload'] = {'task_id': task_id}
            data = (await query(nc, 'ag-a', body, timeout=10))['data']
            if data['state'] in {'succeeded', 'failed', 'expired', 'cancelled'}:
                return data
            await asyncio.sleep(1)
        return data

    checks = []
    for _ in range(20):
        if holder(runtime):
            break
        await asyncio.sleep(1)
    first_holder = holder(runtime)
    checks.append({'check': 'a dispatch lease holder exists', 'holder': first_holder, 'pass': bool(first_holder)})

    first = await submit('fo-1')
    checks.append({'check': 'task 1 under holder', 'state': (await wait_done(first))['state']})

    pid = holder_pid(first_holder)
    if pid:
        os.kill(pid, signal.SIGKILL)
    # Reap through the Popen handle: a killed child stays a zombie until its parent
    # collects it, and os.kill(pid, 0) keeps succeeding for a zombie.
    holder_proc = next((proc for proc in routers if proc.pid == pid), None)
    gone = False
    if holder_proc is not None:
        try:
            holder_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        gone = holder_proc.poll() is not None
    # A holder name we cannot parse as a PID means the drill never killed anything; that
    # must be a failure in its own right, not a silent pass.
    checks.append({'check': 'killed holder', 'pid': pid, 'pass': bool(pid)})
    checks.append({'check': 'holder process is gone', 'pid': pid, 'pass': gone})

    takeover = None
    deadline = time.time() + 30
    while time.time() < deadline:
        current = holder(runtime)
        if current and current != first_holder and holder_pid(current) != pid:
            takeover = current
            break
        await asyncio.sleep(1)
    checks.append({'check': 'survivor took over the lease', 'holder': takeover, 'pass': bool(takeover)})

    second = await submit('fo-2')
    checks.append({'check': 'task 2 after takeover', 'state': (await wait_done(second))['state']})
    checks.append({'check': 'exactly one execution each', 'counts': executed,
                   'pass': executed.get(first) == 1 and executed.get(second) == 1})

    beat.cancel()
    await nc.close(); await peer.close()
    for proc in routers:
        if proc.poll() is None:
            proc.terminate()
    nats_proc.terminate()
    return checks


if __name__ == '__main__':
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    evidence = ROOT / 'evidence' / ('router-failover-' + stamp); evidence.mkdir(parents=True)
    work = Path(tempfile.mkdtemp(prefix='fo-', dir=ROOT / '.runtime')); runtime = work / 'runtime'
    from scripts import bootstrap
    port = free_port()
    bootstrap.RUNTIME = runtime
    bootstrap.main(listen=port)
    (runtime / 'endpoint.txt').write_text(f'tls://localhost:{port}\n')
    os.environ['POC_RUNTIME'] = str(runtime)
    os.environ['NATS_URL'] = f'tls://localhost:{port}'
    os.environ['NATS_SERVER'] = str(ROOT / '.runtime/bin/nats-server')
    try:
        checks = asyncio.run(main(runtime, evidence, port))
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    record = {'scope': 'two Router processes sharing a SQLite dispatch lease + shared replay ledger', 'checks': checks}
    (evidence / 'results.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(json.dumps(record, ensure_ascii=False, indent=2))
    print('Evidence:', evidence)
    from tests import drill_gate
    raise SystemExit(drill_gate.finish('router_failover_drill', checks, evidence))
