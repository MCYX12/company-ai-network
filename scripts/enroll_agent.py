"""Offline administrator enrollment; never accepts privilege claims over NATS."""
import argparse
import fcntl
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.registry import ACTIONS, ID, PROFILES, profile as capability_profile, validate_registry
from app.common import PREFIX

def enroll(runtime, row, client_only=False):
    runtime=runtime.resolve()
    from app.common import authority_base
    with (Path(authority_base(runtime))/"admin-enrollment.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _enroll(runtime,row,client_only)

def _enroll(runtime, row, client_only=False):
    runtime = runtime.resolve()
    from app.common import authority_base
    authority = Path(authority_base(runtime)).resolve()
    registry_file = authority/'registry.json'
    if authority != runtime and not registry_file.is_file():
        raise ValueError('Shared authority registry must be provisioned before enrollment')
    value = json.loads((registry_file if registry_file.exists() else ROOT/'config/registry.json').read_text())
    value['agents'].append(row)
    validate_registry(value)
    identity = row['agent_id']
    config = json.loads((runtime/'nats.conf').read_text())
    users = config['accounts']['POC']['users']
    if any(u['user'] == identity for u in users):
        raise ValueError('NATS identity already exists')
    users.append({'user':identity, 'permissions': {
        'publish':{'allow':[f'{PREFIX}.ingress.{identity}.request',f'{PREFIX}.agent.{identity}.heartbeat',f'{PREFIX}.agent.{identity}.reply',f'{PREFIX}.agent.{identity}.result']},
        'subscribe':{'allow':[f'{PREFIX}.agent.{identity}.query',f'{PREFIX}.client.{identity}.response',f'{PREFIX}.agent.{identity}.task']}}})
    if client_only:
        users[-1]['permissions'] = {
            'publish': {'allow': [f'{PREFIX}.ingress.{identity}.request']},
            'subscribe': {'allow': [f'{PREFIX}.client.{identity}.response']}}
    os.umask(0o077)
    certs = runtime/'certs'
    if (certs/f'{identity}.key').exists():
        raise ValueError('Existing key preserved; choose a new identity')
    def run(*args):
        subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    enterprise = runtime/'certs/ENTERPRISE_CA.json'
    if enterprise.exists():
        # Issue from the two-tier enterprise CA (leaf + intermediate chain).
        from scripts import ca_tool
        ca_dir = Path(json.loads(enterprise.read_text())['ca_dir'])
        issued = ca_tool.issue(ca_dir, identity, 'client', 365, 'http://127.0.0.1:2560')
        chain = Path(issued['cert']).read_bytes() + (ca_dir/'intermediate/intermediate.crt').read_bytes()
        (certs/f'{identity}.crt').write_bytes(chain)
        os.chmod(certs/f'{identity}.crt', 0o644)
        shutil.copy2(issued['key'], certs/f'{identity}.key')
        os.chmod(certs/f'{identity}.key', 0o600)
    else:
        extension = certs/f'{identity}.ext'
        extension.write_text(f'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth\nsubjectAltName=DNS:{identity}\n')
        run('openssl','req','-new','-newkey','rsa:2048','-nodes','-keyout',str(certs/f'{identity}.key'),
            '-out',str(certs/f'{identity}.csr'),'-subj',f'/CN={identity}')
        run('openssl','x509','-req','-in',str(certs/f'{identity}.csr'),'-CA',str(certs/'ca.crt'),
            '-CAkey',str(certs/'ca.key'),'-CAcreateserial','-days','7','-extfile',str(extension),'-out',str(certs/f'{identity}.crt'))
        run('openssl','verify','-x509_strict','-CAfile',str(certs/'ca.crt'),'-purpose','sslclient',str(certs/f'{identity}.crt'))
    candidate = runtime/'nats.enrollment.conf'
    candidate.write_text(json.dumps(config,indent=2,ensure_ascii=False))
    binary = os.environ.get('NATS_SERVER',str(ROOT/'.runtime/bin/nats-server'))
    run(binary,'-t','-c',str(candidate))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    shutil.copy2(runtime/'nats.conf',runtime/f'nats.before-{stamp}.conf')
    if registry_file.exists():
        shutil.copy2(registry_file,authority/f'registry.before-{stamp}.json')
    candidate.replace(runtime/'nats.conf')
    staged = authority/'registry.enrollment.json'
    staged.write_text(json.dumps(value,indent=2,ensure_ascii=False))
    staged.replace(registry_file)
    print(f'Enrolled {identity} on {row["device_id"]}; restart the target NATS to activate its certificate ACL.')

def build_parser():
    parser=argparse.ArgumentParser()
    parser.add_argument('--authz-runtime', type=Path, help='shared registry authority; transport credentials stay local')
    parser.add_argument('--client-only', action='store_true', help='Issue an ordinary caller, without worker publish/subscribe rights')
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--agent', required=True)
    parser.add_argument('--device', required=True)
    parser.add_argument('--employee', required=True)
    parser.add_argument('--tool', required=True, help='Built-in tool id, or the kind id of a self-developed agent')
    parser.add_argument('--profile', choices=sorted(PROFILES), help='Capability profile shortcut for a self-developed agent')
    parser.add_argument('--capabilities', help='Explicit comma-separated capabilities: '+','.join(sorted(ACTIONS)))
    parser.add_argument('--project', action='append', required=True)
    parser.add_argument('--bundle', type=Path, help='also export a per-device enrollment zip here')
    return parser


def enrollment_row(args):
    """Build one registry row; a self-developed tool declares what it can do."""
    if not ID.fullmatch(args.tool):
        raise ValueError('Invalid tool id')
    claimed=None
    if args.profile:
        claimed=capability_profile(args.profile)
    if args.capabilities:
        claimed=[c.strip() for c in args.capabilities.split(',') if c.strip()]
    if claimed is not None and (not claimed or any(c not in ACTIONS for c in claimed)):
        raise ValueError('Invalid --capabilities')
    row=dict(agent_id=args.agent,device_id=args.device,employee_id=args.employee,
             tool=args.tool,projects=args.project,enabled=True)
    if claimed is not None:
        row['capabilities']=claimed
    return row


if __name__ == '__main__':
    parser=build_parser()
    args=parser.parse_args()
    if args.authz_runtime:
        os.environ['POC_AUTHZ_RUNTIME'] = str(args.authz_runtime.resolve())
    try:
        row=enrollment_row(args)
    except ValueError as error:
        parser.error(str(error))
    enroll(args.runtime,row,client_only=args.client_only)
    if args.bundle:
        from scripts.export_client_enrollment import export
        export(args.runtime, args.agent, args.bundle)
        print(json.dumps({'bundle': str(args.bundle)}, ensure_ascii=False))
