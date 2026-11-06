"""Isolated JetStream vs SQLite ledger messaging experiment (plan 10 / selection).

Starts a throwaway nats-server with JetStream on loopback, measures 1 KiB publish
latency, end-to-end delivery latency, redelivery on missing ack and persistence
across a server restart, then runs the same volume through the current SQLite task
ledger for comparison. Writes a JSON report.
"""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def percentiles(values):
    if not values:
        return {}
    ordered = sorted(values)
    def pick(p):
        return round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 4)
    return {'p50': pick(.5), 'p95': pick(.95), 'p99': pick(.99), 'max': round(ordered[-1], 4)}


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def start_server(port, store_dir):
    proc = subprocess.Popen([str(ROOT / '.runtime/bin/nats-server'), '-js', '-sd', str(store_dir), '-p', str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc


async def wait_ready(port, timeout=10):
    import nats
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            nc = await nats.connect(f'nats://127.0.0.1:{port}', connect_timeout=1)
            await nc.close()
            return True
        except Exception:
            await asyncio.sleep(0.2)
    return False


async def jetstream_run(port, store_dir, n=200):
    import nats
    from nats.js.api import ConsumerConfig
    server = start_server(port, store_dir)
    try:
        assert await wait_ready(port), 'nats-server with JetStream did not start'
        nc = await nats.connect(f'nats://127.0.0.1:{port}')
        js = nc.jetstream()
        await js.add_stream(name='BENCH', subjects=['bench.>'])
        payload = b'x' * 1024
        publish_latency = []
        for _ in range(n):
            started = time.perf_counter()
            await js.publish('bench.data', payload)
            publish_latency.append(time.perf_counter() - started)
        # end-to-end delivery with explicit acks
        delivered = []
        sub = await js.subscribe('bench.>', durable='bench-dur', manual_ack=True)
        for _ in range(n):
            msg = await sub.next_msg(timeout=5)
            delivered.append(time.perf_counter())
            await msg.ack()
        await sub.unsubscribe()
        # redelivery when the first delivery is not acked
        await js.publish('bench.redeliver', payload)
        probe = await js.subscribe('bench.redeliver', durable='redeliver-probe', manual_ack=True, config=ConsumerConfig(ack_wait=1))
        first = await probe.next_msg(timeout=5)
        first_delivered = first.metadata.num_delivered
        await asyncio.sleep(1.5)
        second = await probe.next_msg(timeout=5)
        redelivered = second.metadata.num_delivered
        await second.ack()
        await probe.unsubscribe()
        # persistence across a server restart
        await js.publish('bench.marker', b'marker')
        await nc.close()
        server.terminate(); server.wait(timeout=10)
        server = start_server(port, store_dir)
        assert await wait_ready(port), 'nats-server did not restart'
        nc = await nats.connect(f'nats://127.0.0.1:{port}')
        js = nc.jetstream()
        marker = await js.pull_subscribe('bench.marker', durable='marker-probe')
        fetched = await marker.fetch(1, timeout=5)
        persisted = fetched[0].data == b'marker'
        await fetched[0].ack()
        await nc.close()
        return {'publish_latency_s': percentiles(publish_latency), 'messages': n,
                'publish_rate_per_s': round(n / sum(publish_latency), 1) if publish_latency else None,
                'first_delivery': first_delivered, 'redelivered_attempt': redelivered,
                'redelivery_ok': redelivered > first_delivered, 'persisted_after_restart': persisted}
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def sqlite_run(runtime, n=200):
    from app.task_store import TaskStore
    store = TaskStore(Path(runtime) / 'bench-tasks.sqlite3')
    latency = []
    for i in range(n):
        started = time.perf_counter()
        task = store.submit('ag-a', 'ag-b', f'bench-{i}', 'ask_peer', {'question': 'x'}, 300)
        chosen = store.plan(time.time(), lambda row: True, lambda target, op: True)
        store.finish(task['task_id'], {'code': 'OK'})
        latency.append(time.perf_counter() - started)
        assert chosen, 'ledger did not dispatch'
    return {'messages': n, 'cycle_latency_s': percentiles(latency),
            'cycle_rate_per_s': round(n / sum(latency), 1) if latency else None}


async def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    evidence = ROOT / 'evidence' / ('messaging-' + stamp)
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='js-', dir=ROOT / '.runtime') as folder:
        js = await jetstream_run(free_port(), Path(folder) / 'jsstore')
        with tempfile.TemporaryDirectory(prefix='ledger-', dir=ROOT / '.runtime') as runtime:
            ledger = sqlite_run(runtime)
    report = {'generated_at': stamp, 'payload': '1 KiB', 'note': 'single-host loopback sample, not a production benchmark',
              'jetstream': js, 'sqlite': ledger,
              'postgresql': 'not measured: no PostgreSQL image or server available in this environment'}
    (evidence / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print('Evidence:', evidence)


if __name__ == '__main__':
    asyncio.run(main())
