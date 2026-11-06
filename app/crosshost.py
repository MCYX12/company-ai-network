"""Physical cross-host acceptance gate: a same-host drill is not cross-host evidence.

The multirouter failover / partition / KV smoke drills run several processes (sometimes
containers) on ONE machine. They are valid fast regression tests, but they cannot prove physical
fault-domain separation: one power, network or disk failure takes every "host" down together.
This module turns "is this really cross-host?" into an explicit, checkable claim, so a result
cannot be labelled physical cross-host acceptance from a single machine.
"""
import json
from pathlib import Path


def machine_identity(facts):
    """Strongest available machine identity: machine-id, then boot-id, then hostname."""
    for key in ('machine_id', 'boot_id', 'hostname'):
        value = (facts or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _distinct(names, facts):
    identities = [machine_identity((facts or {}).get(name, {})) for name in names]
    present = [identity for identity in identities if identity]
    return len(present) == len(names) and len(set(present)) == len(names), identities


def assess(hosts, nats=(), routers=(), workers=(), require_ha=True):
    """Return [{'check','ok','detail'}] for whether a topology is physically multi-host.

    hosts: {name: {'machine_id'|'boot_id'|'hostname', 'runtime_dir', 'addresses': [...]}}
    """
    hosts = hosts or {}
    checks = []

    def add(check, ok, detail=None):
        checks.append({'check': check, 'ok': bool(ok), 'detail': detail})

    ok, identities = _distinct(nats, hosts)
    add('NATS 节点分布在 ≥3 台不同物理机（R3 前提）', len(nats) >= 3 and ok,
        {'nodes': list(nats), 'machine_identities': identities})
    ok, identities = _distinct(routers, hosts)
    add('Router 分布在 ≥2 台不同物理机（故障切换前提）', len(routers) >= 2 and ok,
        {'routers': list(routers), 'machine_identities': identities})

    router_machines = {machine_identity(hosts.get(name, {})) for name in routers}
    router_machines.discard(None)
    worker_machines = {machine_identity(hosts.get(name, {})) for name in workers}
    worker_machines.discard(None)
    independent = worker_machines - router_machines if routers and workers else set()
    add('存在与所有 Router 不同机的 Worker（独立主机故障验收前提）', bool(independent),
        {'router_machines': sorted(router_machines), 'worker_machines': sorted(worker_machines),
         'independent_worker_machines': sorted(independent)})

    runtime_dirs = [hosts.get(name, {}).get('runtime_dir') for name in list(nats) + list(routers) + list(workers)]
    present = [value for value in runtime_dirs if value]
    add('各主机运行目录不同（没有共用同一挂载）', bool(present) and len(set(present)) == len(present),
        {'runtime_dirs': runtime_dirs})

    addresses = [address for name in list(nats) + list(routers) + list(workers)
                 for address in (hosts.get(name, {}).get('addresses') or [])]
    loopback = [address for address in addresses if str(address).startswith(('127.', '::1', 'localhost'))]
    add('没有把回环地址当作跨机地址', not loopback and bool(addresses),
        {'addresses': addresses, 'loopback': loopback})

    if require_ha:
        shared = all(hosts.get(name, {}).get('authz_shared') is True for name in routers) if routers else False
        add('每个 Router 都声明共享授权控制面（POC_AUTHZ_SHARED=1）', bool(routers) and shared,
            {'routers': list(routers),
             'authz_shared': {name: hosts.get(name, {}).get('authz_shared') for name in routers}})
    return checks


def summarize(checks):
    return {'total': len(checks), 'passed': sum(1 for c in checks if c['ok']),
            'failed': sum(1 for c in checks if not c['ok'])}


def load_facts(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return data.get('hosts', data) if isinstance(data, dict) else {}
