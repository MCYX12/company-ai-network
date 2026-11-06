"""Capacity sample for plan section 14 / PERF-01/PERF-02 on an isolated stack.

Opens N mTLS client connections (a small pool of identities reused across
connections), drives read-only get_project_progress queries through the Router to
a pool of real agent processes, and reports success rate plus latency percentiles
and process memory. Isolated loopback port; the live runtime is untouched.

This is a single-host PoC sample, not a production capacity guarantee.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PREFIX = 'corp.dev.v1'


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def openssl(*args):
    subprocess.run(['openssl', *map(str, args)], check=True, capture_output=True)


def ca(certs):
    openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes', '-days', '7',
            '-keyout', certs / 'ca.key', '-out', certs / 'ca.crt', '-subj', '/CN=Load-CA',
            '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')


def leaf(certs, name, server=False):
    ext = certs / f'{name}.ext'
    if server:
        ext.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n')
    else:
        ext.write_text(f'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\nsubjectAltName=DNS:{name}\n')
    openssl('req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
            '-keyout', certs / f'{name}.key', '-out', certs / f'{name}.csr', '-subj', f'/CN={name}')
    openssl('x509', '-req', '-in', certs / f'{name}.csr', '-CA', certs / 'ca.crt', '-CAkey', certs / 'ca.key',
            '-CAcreateserial', '-days', '7', '-extfile', ext, '-out', certs / f'{name}.crt')


def build_runtime(base, port, clients, targets, router='router'):
    certs = base / 'certs'; certs.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    ca(certs); leaf(certs, 'server', server=True); leaf(certs, router)
    for name in [*clients, *targets]:
        leaf(certs, name)
    users = []
    for name in clients:
        users.append({'user': name, 'permissions': {
            'publish': {'allow': [f'{PREFIX}.ingress.{name}.request']},
            'subscribe': {'allow': [f'{PREFIX}.client.{name}.response']}}})
    for name in targets:
        users.append({'user': name, 'permissions': {
            'publish': {'allow': [f'{PREFIX}.agent.{name}.heartbeat', f'{PREFIX}.agent.{name}.reply', f'{PREFIX}.agent.{name}.result']},
            'subscribe': {'allow': [f'{PREFIX}.agent.{name}.query', f'{PREFIX}.client.{name}.response', f'{PREFIX}.agent.{name}.task']}}})
    users.append({'user': router, 'permissions': {
        'publish': {'allow': [f'{PREFIX}.agent.*.query', f'{PREFIX}.client.*.response', f'{PREFIX}.agent.*.task']},
        'subscribe': {'allow': [f'{PREFIX}.ingress.*.request', f'{PREFIX}.agent.*.reply', f'{PREFIX}.agent.*.heartbeat', f'{PREFIX}.agent.*.result']}}})
    (base / 'nats.conf').write_text(json.dumps({
        'listen': f'127.0.0.1:{port}', 'max_payload': 65536,
        'tls': {'cert_file': str(certs / 'server.crt'), 'key_file': str(certs / 'server.key'),
                'ca_file': str(certs / 'ca.crt'), 'verify_and_map': True},
        'accounts': {'POC': {'users': users}}}, indent=2, ensure_ascii=False))
    agents = [{'agent_id': name, 'device_id': 'load-device', 'employee_id': name, 'tool': 'fixture',
               'projects': ['demo-project'], 'enabled': True} for name in [*clients, *targets]]
    (base / 'registry.json').write_text(json.dumps({'schema_version': 1, 'agents': agents}, indent=2))


class Requester:
    def __init__(self, nc, identity):
        self.nc = nc; self.identity = identity; self.pending = {}

    async def start(self):
        await self.nc.subscribe(f'{PREFIX}.client.{self.identity}.response', cb=self._reply)
        await self.nc.flush()

    async def _reply(self, msg):
        try:
            from app.common import decode
            data = decode(msg.data)
        except Exception:
            return
        future = self.pending.pop(data.get('correlation_id'), None)
        if future and not future.done():
            future.set_result(data)

    async def ask(self, body, timeout=8):
        from app.common import encode
        future = asyncio.get_running_loop().create_future()
        self.pending[body['correlation_id']] = future
        await self.nc.publish(f'{PREFIX}.ingress.{self.identity}.request', encode(body))
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(body['correlation_id'], None)

    async def close(self):
        await self.nc.close()


def rss_mb(pid):
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def cpu_seconds(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().split()
        return round((int(fields[13]) + int(fields[14])) / os.sysconf('SC_CLK_TCK'), 3)
    except (OSError, ValueError, IndexError):
        return None


async def open_connections(clients, total, quiet, concurrent=False):
    from app.common import connect
    requesters = []
    pairs = [(clients[index % len(clients)],
              connect(clients[index % len(clients)], error_cb=quiet, disconnected_cb=quiet)) for index in range(total)]
    if concurrent:
        opened = await asyncio.gather(*[item for _, item in pairs], return_exceptions=True)
        for (identity, _), item in zip(pairs, opened):
            if isinstance(item, Exception):
                continue
            requester = Requester(item, identity)
            await requester.start()
            requesters.append(requester)
        return requesters
    batch = 100
    for start in range(0, total, batch):
        chunk = pairs[start:start + batch]
        opened = await asyncio.gather(*[item for _, item in chunk], return_exceptions=True)
        for (identity, _), item in zip(chunk, opened):
            if isinstance(item, Exception):
                continue
            requester = Requester(item, identity)
            await requester.start()
            requesters.append(requester)
    return requesters


async def apply_load(requesters, clients, targets, qps, duration, timeouts):
    from app.common import request
    latencies = []
    codes = {}
    interval = 1.0 / qps
    end = time.monotonic() + duration
    inflight = set()

    async def one(requester):
        body = request(requester.identity, random.choice(targets))
        started = time.perf_counter()
        try:
            result = await requester.ask(body, timeout=8)
            code = result.get('code')
            codes[code] = codes.get(code, 0) + 1
            if code == 'OK':
                latencies.append((time.perf_counter() - started) * 1000)
        except asyncio.TimeoutError:
            codes['TIMEOUT'] = codes.get('TIMEOUT', 0) + 1
        except Exception as error:
            key = 'EXC_' + type(error).__name__
            codes[key] = codes.get(key, 0) + 1

    next_at = time.monotonic()
    index = 0
    last_progress = time.monotonic()
    while time.monotonic() < end:
        now = time.monotonic()
        if now < next_at:
            await asyncio.sleep(next_at - now)
        next_at += interval
        requester = requesters[index % len(requesters)]
        index += 1
        task = asyncio.create_task(one(requester))
        inflight.add(task)
        task.add_done_callback(inflight.discard)
        if time.monotonic() - last_progress >= 60:
            last_progress = time.monotonic()
            print(f'progress: {index} sent, ok={codes.get("OK", 0)}, in-flight={len(inflight)}', flush=True)
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)
    return latencies, codes


# V1.2 计划书 §16 / docs/CAPACITY_REPORT.md 的验收口径（原文数值，不自行发明）：
#   PERF-01：1000 连接 + 100 次/秒 1 KiB 只读查询 ⇒ 成功率 ≥99.9%、P95 ≤500ms，
#            且必须同时报告总请求数、超时数与错误率（排除模型推理/策略拒绝/预设离线目标）。
#   PERF-02：1000 身份分批与同时重连 ⇒ 无进程崩溃、无界内存增长（报告稳定时间与排队上限）。
PERF01 = {'connections': 1000, 'qps': 100, 'success_rate_min': 0.999, 'p95_ms_max': 500.0}


def perf01_checks(scenario, success_rate_min=None, p95_ms_max=None, max_rss_growth_mb=None,
                  expected_connections=None, expected_qps=None):
    """把一次采样结果判成 PERF-01/02 的逐条结论（只判这次采样，不推断生产容量）。

    负载形状单独成一条：**小规模采样即使全绿也不等于 PERF-01 通过**，所以连接数/QPS 与
    计划书不一致时该条为 False——避免用 100 连接的样本冒充 1000 连接的验收。
    """
    rate_min = PERF01['success_rate_min'] if success_rate_min is None else success_rate_min
    p95_max = PERF01['p95_ms_max'] if p95_ms_max is None else p95_ms_max
    want_connections = PERF01['connections'] if expected_connections is None else expected_connections
    want_qps = PERF01['qps'] if expected_qps is None else expected_qps
    codes = scenario.get('codes') or {}
    latency = scenario.get('latency_ms') or {}
    rate = scenario.get('success_rate')
    p95 = latency.get('p95')
    before, after = scenario.get('router_rss_mb_before'), scenario.get('router_rss_mb_after')
    checks = [
        {'check': 'PERF-01 负载形状与计划书一致（%d 连接 @%d qps）' % (want_connections, want_qps),
         'pass': scenario.get('connections_requested') == want_connections and scenario.get('target_qps') == want_qps,
         'connections': scenario.get('connections_requested'), 'qps': scenario.get('target_qps'),
         'response_bytes': scenario.get('response_bytes')},
        {'check': 'PERF-01 成功率 ≥%.1f%%' % (rate_min * 100),
         'pass': rate is not None and rate >= rate_min, 'success_rate': rate, 'threshold': rate_min,
         'responses': scenario.get('responses'), 'timeouts': codes.get('TIMEOUT'),
         'errors': {k: v for k, v in codes.items() if k not in ('OK', 'TIMEOUT')}},
        {'check': 'PERF-01 P95 ≤%gms' % p95_max, 'pass': p95 is not None and p95 <= p95_max,
         'p50_ms': latency.get('p50'), 'p95_ms': p95, 'p99_ms': latency.get('p99'),
         'max_ms': latency.get('max'), 'threshold_ms': p95_max},
        {'check': 'PERF-02 采样期内路由进程未崩溃', 'pass': after is not None,
         'rss_before_mb': before, 'rss_after_mb': after},
    ]
    if max_rss_growth_mb is not None:
        growth = None if before is None or after is None else round(after - before, 1)
        checks.append({'check': 'PERF-02 路由内存增长 ≤%gMB' % max_rss_growth_mb,
                       'pass': growth is not None and growth <= max_rss_growth_mb,
                       'rss_growth_mb': growth, 'threshold_mb': max_rss_growth_mb})
    return checks


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * len(ordered) + 0.5)) - 1))
    return round(ordered[index], 2)


async def scenario(runtime, evidence, clients, targets, connections, qps, duration, router_pid, concurrent=False):
    async def quiet(*_):
        pass
    results = {'connections_requested': connections, 'target_qps': qps, 'duration_seconds': duration,
               'concurrent_connect': concurrent}
    started = time.perf_counter()
    cpu_before = cpu_seconds(router_pid)
    requesters = await open_connections(clients, connections, quiet, concurrent)
    results['connections_opened'] = len(requesters)
    results['connect_seconds'] = round(time.perf_counter() - started, 2)
    results['router_rss_mb_before'] = rss_mb(router_pid)
    try:
        latencies, codes = await apply_load(requesters, clients, targets, qps, duration, 8)
        total = sum(codes.values())
        results['responses'] = total
        results['ok'] = codes.get('OK', 0)
        results['success_rate'] = round(codes.get('OK', 0) / max(1, total), 4)
        results['codes'] = codes
        results['latency_ms'] = {'p50': percentile(latencies, 0.50), 'p95': percentile(latencies, 0.95),
                                 'p99': percentile(latencies, 0.99), 'max': round(max(latencies), 2) if latencies else None}
    finally:
        results['router_rss_mb_after'] = rss_mb(router_pid)
        cpu_after = cpu_seconds(router_pid)
        results['wall_seconds'] = round(time.perf_counter() - started, 2)
        if cpu_before is not None and cpu_after is not None:
            results['router_cpu_seconds'] = round(cpu_after - cpu_before, 3)
            results['router_cpu_percent_of_one_core'] = round((cpu_after - cpu_before) / max(0.001, results['wall_seconds']) * 100, 1)
        await asyncio.gather(*[r.close() for r in requesters], return_exceptions=True)
    return results


async def run(runtime, evidence, clients, targets, scenarios, router_pid, concurrent=False):
    output = []
    for connections, qps, duration in scenarios:
        print(f'== scenario {connections} connections @ {qps} qps for {duration}s concurrent={concurrent} ==', flush=True)
        output.append(await scenario(runtime, evidence, clients, targets, connections, qps, duration, router_pid, concurrent))
        print(json.dumps(output[-1], ensure_ascii=False), flush=True)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--scenarios', default='100:50:15,1000:100:15', help='connections:qps:seconds,...')
    parser.add_argument('--clients', type=int, default=20)
    parser.add_argument('--targets', type=int, default=10)
    parser.add_argument('--keep', action='store_true')
    parser.add_argument('--concurrent-connect', action='store_true')
    parser.add_argument('--response-bytes', type=int, default=0, help='pad fixture responses to ~N bytes (PERF-02 1 KiB)')
    parser.add_argument('--delay-ms', type=int, default=0, help='synthetic model-inference delay per fixture call')
    parser.add_argument('--threshold', action='store_true',
                        help='按 V1.2 PERF-01/02 阈值判定并作为退出码（默认只采样、不判定）')
    parser.add_argument('--success-rate-min', type=float, default=None, help='覆盖 PERF-01 成功率下限（默认 0.999）')
    parser.add_argument('--p95-max-ms', type=float, default=None, help='覆盖 PERF-01 P95 上限（默认 500ms）')
    parser.add_argument('--max-rss-growth-mb', type=float, default=None,
                        help='PERF-02 路由内存增长上限（默认不判；建议目标机上给站点值）')
    args = parser.parse_args()
    os.environ['POC_FIXTURE_RESPONSE_BYTES'] = str(args.response_bytes)
    os.environ['POC_FIXTURE_DELAY_MS'] = str(args.delay_ms)
    scenarios = [tuple(int(x) for x in item.split(':')) for item in args.scenarios.split(',')]
    clients = [f'load-c{i:02d}' for i in range(args.clients)]
    targets = ['ag-b'] + [f'load-t{i:02d}' for i in range(max(1, args.targets) - 1)]

    evidence = ROOT / 'evidence' / ('capacity-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    evidence.mkdir()
    work = Path(tempfile.mkdtemp(prefix='capacity-', dir=ROOT / '.runtime'))
    port = free_port()
    build_runtime(work, port, clients, targets)
    os.environ['POC_RUNTIME'] = str(work)
    os.environ['NATS_URL'] = f'tls://localhost:{port}'
    os.environ['NATS_SERVER'] = str(ROOT / '.runtime/bin/nats-server')

    from scripts.run_local import Stack
    stack = Stack(evidence, port=port)
    try:
        stack.boot()
        for name in targets[1:]:
            stack.start(name, [sys.executable, '-u', '-m', 'app.agent', name])
        time.sleep(2)
        router_pid = stack.children['router'].pid
        results = asyncio.run(run(work, evidence, clients, targets, scenarios, router_pid, args.concurrent_connect))
        (evidence / 'results.json').write_text(json.dumps(
            {'scope': 'isolated single-host capacity sample; not a production capacity guarantee',
             'response_bytes': args.response_bytes, 'delay_ms': args.delay_ms,
             'clients': clients, 'targets': targets, 'scenarios': results}, indent=2, ensure_ascii=False))
    finally:
        stack.close()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    print('Evidence:', evidence)
    code = 0
    if args.threshold:
        checks = []
        for item in results:
            item['response_bytes'] = args.response_bytes
            checks.extend(perf01_checks(item, args.success_rate_min, args.p95_max_ms,
                                        max_rss_growth_mb=args.max_rss_growth_mb))
        from tests import drill_gate
        code = drill_gate.finish('load_capacity (PERF-01/02 阈值)', checks, evidence)
    else:
        print('  采样模式：未按阈值判定（加 --threshold 启用 PERF-01/02 门禁；阈值见 docs/CAPACITY_REPORT.md）', flush=True)
    sys.exit(code)
