"""Render a cross-host HA deployment plan from a site profile (pure planner).

It validates the host roles and returns every per-host file (shared registry,
per-NATS-node config with cluster routes and JetStream, router/worker env and
systemd units) plus the commands to apply them. It never contacts a remote host;
an operator (or a later ssh step) copies the files. Keeping it pure makes the plan
unit-testable and reviewable before anything touches production.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import site as site_profile
from scripts import render_site

DEFAULT_INSTALL = '/opt/company-agent'


def validate_hosts(hosts):
    nats = [h for h in hosts if h.get('role') == 'nats']
    routers = [h for h in hosts if h.get('role') == 'router']
    workers = [h for h in hosts if h.get('role') == 'worker']
    known = {'nats', 'router', 'worker'}
    return [
        {'check': 'at least 3 NATS hosts for a replicated stream (R3)', 'ok': len(nats) >= 3,
         'detail': len(nats)},
        {'check': 'at least 2 Router hosts for leader failover', 'ok': len(routers) >= 2,
         'detail': len(routers)},
        {'check': 'every host has a name and an address',
         'ok': all(h.get('name') and h.get('address') for h in hosts)},
        {'check': 'every host role is known',
         'ok': all(h.get('role') in known for h in hosts)},
        {'check': 'every worker names an agent_id',
         'ok': all(h.get('agent_id') for h in workers), 'detail': len(workers)},
    ]


def systemd_unit(description, exec_start, environment_file, working_directory=None, writable_paths=None):
    """A unit that names its WorkingDirectory and its real writable paths.

    Without WorkingDirectory a service started outside the repo cannot import app.*, and a
    hardcoded ReadWritePaths=/opt/company-agent/runtime conflicts with a custom install_dir.
    """
    lines = ['[Unit]', f'Description={description}', 'After=network-online.target', '', '[Service]',
             'Type=simple', f'EnvironmentFile={environment_file}', f'ExecStart={exec_start}']
    if working_directory:
        lines.append(f'WorkingDirectory={working_directory}')
    lines += ['Restart=always', 'RestartSec=2', 'NoNewPrivileges=true', 'ProtectSystem=strict']
    if writable_paths:
        lines.append('ReadWritePaths=' + ' '.join(writable_paths))
    lines += ['', '[Install]', 'WantedBy=multi-user.target', '']
    return chr(10).join(lines)


def render_plan(profile, hosts, install_dir=DEFAULT_INSTALL, certs_dir=None, port=None, authz_runtime=None):
    authz_runtime = authz_runtime or f'{install_dir}/authz'
    for value in (install_dir, authz_runtime):
        if not Path(value).is_absolute() or any(c.isspace() or c in "'\";$`" for c in value):
            raise ValueError('Deployment paths must be absolute and contain no shell metacharacters')
    if authz_runtime == f'{install_dir}/runtime':
        raise ValueError('Shared authority must have a dedicated mount, separate from local runtime')
    merged = site_profile._merge(site_profile.DEFAULTS, profile)
    problems = site_profile.validate(merged)
    if problems:
        raise ValueError('; '.join(problems))
    checks = validate_hosts(hosts)
    failures = [c['check'] for c in checks if not c['ok']]
    if failures:
        raise ValueError('invalid host plan: ' + '; '.join(failures))
    nats = [h for h in hosts if h['role'] == 'nats']
    routers = [h for h in hosts if h['role'] == 'router']
    workers = [h for h in hosts if h['role'] == 'worker']
    listen_port = port or merged['server'].get('nats_port', 14222)
    certs = certs_dir or f'{install_dir}/runtime/certs'
    files = {'registry.json': json.dumps(render_site.render_registry(merged), ensure_ascii=False, indent=2),
             'site.json': json.dumps(merged, ensure_ascii=False, indent=2)}
    commands = {}
    for host in nats:
        conf = render_site.render_nats(merged, Path(certs), listen=f"{host['address']}:{listen_port}")
        conf['server_name'] = host['name']
        conf['cluster'] = {'name': 'POC', 'listen': f"{host['address']}:6222",
                           'routes': [f"nats-route://{other['address']}:6222" for other in nats
                                      if other['name'] != host['name']]}
        files[f"nats/{host['name']}/nats.conf"] = json.dumps(conf, ensure_ascii=False, indent=2)
        commands[host['name']] = [
            f"install -d -m700 {install_dir}/runtime",
            f"install -m600 nats/{host['name']}/nats.conf {install_dir}/runtime/nats.conf",
            f"{install_dir}/.venv/bin/nats-server -c {install_dir}/runtime/nats.conf",
        ]
    servers = ','.join(f"tls://{h['address']}:{listen_port}" for h in nats)
    common_env = {
        'NATS_URL': servers,
        'POC_RUNTIME': f'{install_dir}/runtime',
        'POC_SITE': f'{install_dir}/runtime/site.json',
        'POC_JETSTREAM': '1',
        'POC_JETSTREAM_REPLICAS': str(min(3, len(nats))),
        'POC_IAM_STRICT': '1',
        'POC_AUTHZ_RUNTIME': authz_runtime,
        'POC_AUTHZ_SHARED': '1',
    }
    router_env = {**common_env, 'POC_HA': '1', 'POC_LEASE_TTL': '15',
                  'POC_JETSTREAM_PUBLISH_TIMEOUT': '5'}
    # The worker only validates the shared KV dispatch lease when POC_HA=1, so leaving the
    # worker environment without it quietly disabled worker-side fencing on a multi-Router
    # deployment (assessment 2026-10-05). Mirror the Router switch onto the workers.
    worker_env = {**common_env, 'POC_HA': '1'}
    for host in routers:
        files[f"router/{host['name']}/env"] = ''.join(f'{k}={v}\n' for k, v in router_env.items())
        files[f"router/{host['name']}/company-agent-router.service"] = systemd_unit(
            f'Company Agent Router ({host["name"]})', f'{install_dir}/.venv/bin/python -u -m app.router',
            f'{install_dir}/runtime/router.env',
            working_directory=install_dir, writable_paths=[f'{install_dir}/runtime', authz_runtime])
        commands[host['name']] = [
            f"test -d {authz_runtime} && test -r {authz_runtime}/registry.json && test -w {authz_runtime}",
            f"install -d -m700 {install_dir}/runtime",
            f"install -m600 site.json {install_dir}/runtime/site.json",
            f"install -m600 router/{host['name']}/env {install_dir}/runtime/router.env",
            f"install -m600 router/{host['name']}/company-agent-router.service /etc/systemd/system/",
            'systemctl daemon-reload && systemctl enable --now company-agent-router',
        ]
    files['enroll.txt'] = '\n'.join(command + f' --authz-runtime {authz_runtime}'
                                  for command in render_site.enroll_commands(merged, install_dir=install_dir,
                                                                           runtime=f'{install_dir}/runtime')) + '\n'
    for host in workers:
        files[f"worker/{host['name']}/env"] = ''.join(f'{k}={v}\n' for k, v in worker_env.items())
        files[f"worker/{host['name']}/company-agent.service"] = systemd_unit(
            f'Company Agent worker ({host["agent_id"]})',
            f'{install_dir}/.venv/bin/python -u -m app.agent {host["agent_id"]}',
            f'{install_dir}/runtime/agent.env',
            working_directory=install_dir, writable_paths=[f'{install_dir}/runtime', authz_runtime])
        commands[host['name']] = [
            f"test -d {authz_runtime} && test -r {authz_runtime}/registry.json && test -w {authz_runtime}",
            f"install -d -m700 {install_dir}/runtime",
            f"install -m600 site.json {install_dir}/runtime/site.json",
            f"install -m600 worker/{host['name']}/env {install_dir}/runtime/agent.env",
            f"install -m600 worker/{host['name']}/company-agent.service /etc/systemd/system/",
            'systemctl daemon-reload && systemctl enable --now company-agent',
        ]
    files['AUTHORITY_SETUP.md'] = (
        f'All Router/worker hosts must mount the SAME authorization authority at {authz_runtime}.\n'
        'Do not create independent directories to satisfy the startup check.\n'
        'Install registry.json and enroll against this authority once, preserving existing ledgers.\n'
        'Verify shared locking/durability/partition behavior before production acceptance.\n'
        'Generated files validate configuration shape; they do not provision or certify the shared mount.\n')
    return {'checks': checks, 'files': files, 'commands': commands, 'authority_runtime': authz_runtime,
            'topology': {'nats': [h['name'] for h in nats], 'routers': [h['name'] for h in routers],
                         'workers': [h['name'] for h in workers]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', type=Path, help='site profile JSON (defaults to the resolved profile)')
    parser.add_argument('--hosts', type=Path, required=True, help='host plan JSON')
    parser.add_argument('--out', type=Path, required=True, help='output directory')
    parser.add_argument('--install-dir', default=DEFAULT_INSTALL)
    parser.add_argument('--certs-dir')
    parser.add_argument('--authz-runtime', help='absolute path of the common authority mount on all hosts')
    args = parser.parse_args()
    profile = json.loads(args.site.read_text()) if args.site else site_profile.load()
    raw_hosts = json.loads(args.hosts.read_text())
    hosts = raw_hosts['hosts'] if isinstance(raw_hosts, dict) else raw_hosts
    plan = render_plan(profile, hosts, install_dir=args.install_dir, certs_dir=args.certs_dir,
                       authz_runtime=args.authz_runtime)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'deployment.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    for name, content in plan['files'].items():
        path = args.out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    print(json.dumps({'topology': plan['topology'], 'files': len(plan['files']),
                      'checks': plan['checks']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
