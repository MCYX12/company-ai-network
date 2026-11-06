"""Render registry / NATS config / enrollment commands from a site profile.

One site profile describes a company: its message prefix, agent list (one per company
computer) and projects. This tool turns that profile into the runtime files so the same
code can be deployed at different companies without editing code.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import site as site_profile
from app.registry import TALK_ACTIONS, capabilities, validate_registry

def normalize_agents(profile):
    rows = []
    for agent in profile.get('agents', []):
        row = {'agent_id': agent['agent_id'], 'device_id': agent['device_id'],
               'employee_id': agent['employee_id'], 'tool': agent['tool'],
               'projects': list(agent['projects']), 'enabled': bool(agent.get('enabled', True))}
        if 'capabilities' in agent:
            row['capabilities'] = list(agent['capabilities'])
        rows.append(row)
    return rows


def render_registry(profile):
    value = {'schema_version': 1, 'agents': normalize_agents(profile)}
    validate_registry(value)
    return value


def render_nats(profile, certs_dir, listen=None):
    prefix = profile['prefix']
    users = []
    for agent in normalize_agents(profile):
        name = agent['agent_id']
        if set(capabilities(agent)) & TALK_ACTIONS:
            # Worker identities also consume the durable stream; client-only
            # identities never get JetStream subjects (see the ACL denial tests).
            users.append({'user': name, 'permissions': {
                'publish': {'allow': [f'{prefix}.ingress.{name}.request', f'{prefix}.agent.{name}.heartbeat',
                                      f'{prefix}.agent.{name}.reply', f'{prefix}.agent.{name}.result',
                                      '$JS.API.>', '$JS.ACK.>']},
                'subscribe': {'allow': [f'{prefix}.agent.{name}.query', f'{prefix}.client.{name}.response',
                                        f'{prefix}.agent.{name}.task', f'{prefix}.task.{name}',
                                        '_INBOX.>']}}})
        else:
            users.append({'user': name, 'permissions': {
                'publish': {'allow': [f'{prefix}.ingress.{name}.request']},
                'subscribe': {'allow': [f'{prefix}.client.{name}.response']}}})
    users.append({'user': 'router', 'permissions': {
        'publish': {'allow': [f'{prefix}.agent.*.query', f'{prefix}.client.*.response', f'{prefix}.agent.*.task',
                              f'{prefix}.task.*', '$JS.API.>', '$KV.poc_tasks.>', '$KV.poc_leases.>',
                              '$JS.SNAPSHOT.ACK.>', '$JS.SNAPSHOT.RESTORE.>']},
        'subscribe': {'allow': [f'{prefix}.ingress.*.request', f'{prefix}.agent.*.reply',
                                f'{prefix}.agent.*.heartbeat', f'{prefix}.agent.*.result', '_INBOX.>']}}})
    server = profile.get('server', {})
    listen = listen or f"127.0.0.1:{server.get('nats_port', 14222)}"
    certs_dir = Path(certs_dir)
    # Durable dispatch is the deployed default: the server config enables JetStream
    # under the runtime dir. The Router falls back to Core NATS if it is absent.
    jetstream = {'store_dir': str(certs_dir.parent / 'jetstream'),
                 'max_mem': int(server.get('jetstream_max_mem', 268435456)),
                 'max_file': int(server.get('jetstream_max_file', 1073741824))}
    return {'listen': listen, 'max_payload': 65536, 'jetstream': jetstream,
            'tls': {'cert_file': str(certs_dir / 'server.crt'), 'key_file': str(certs_dir / 'server.key'),
                    'ca_file': str(certs_dir / 'ca.crt'), 'verify_and_map': True},
            # A custom account needs JetStream enabled explicitly, not only the global block.
            'accounts': {'POC': {'jetstream': 'enabled', 'users': users}}}


def enroll_commands(profile, install_dir='<安装目录>', runtime='<运行目录>'):
    lines = []
    for agent in normalize_agents(profile):
        command = (f"{install_dir}/.venv/bin/python {install_dir}/scripts/enroll_agent.py --runtime {runtime} "
                   f"--agent {agent['agent_id']} --device {agent['device_id']} --employee {agent['employee_id']} "
                   f"--tool {agent['tool']}" + ''.join(f" --project {p}" for p in agent['projects']))
        lines.append(command)
    return lines


def build(profile, runtime=None, certs_dir=None, listen=None):
    normalized = site_profile.load() if profile is None else profile
    problems = site_profile.validate(normalized)
    if problems:
        raise ValueError('; '.join(problems))
    runtime = Path(runtime) if runtime else None
    certs = Path(certs_dir) if certs_dir else (runtime / 'certs' if runtime else ROOT / '.runtime/certs')
    return {'registry': render_registry(normalized), 'nats': render_nats(normalized, certs, listen),
            'enroll': enroll_commands(normalized), 'projects': normalized.get('projects', [])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--site', type=Path, help='site profile JSON (defaults to the resolved profile)')
    parser.add_argument('--check', action='store_true', help='validate only')
    parser.add_argument('--runtime', type=Path, help='write registry.json here')
    parser.add_argument('--nats-conf', type=Path, help='write the generated nats.conf here')
    parser.add_argument('--certs-dir', type=Path)
    parser.add_argument('--listen')
    args = parser.parse_args()
    profile = json.loads(args.site.read_text()) if args.site else None
    if profile is not None:
        problems = site_profile.validate({**site_profile.DEFAULTS, **profile})
        if problems:
            raise SystemExit('invalid site profile: ' + '; '.join(problems))
    rendered = build(profile, args.runtime, args.certs_dir, args.listen)
    if args.runtime:
        (args.runtime / 'registry.json').write_text(json.dumps(rendered['registry'], ensure_ascii=False, indent=2))
    if args.nats_conf:
        args.nats_conf.write_text(json.dumps(rendered['nats'], ensure_ascii=False, indent=2))
    summary = {'agents': len(rendered['registry']['agents']), 'projects': len(rendered['projects']),
               'enroll_commands': len(rendered['enroll'])}
    if args.runtime:
        summary['registry'] = str(args.runtime / 'registry.json')
    if args.nats_conf:
        summary['nats_conf'] = str(args.nats_conf)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
