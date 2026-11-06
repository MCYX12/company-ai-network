"""Per-deployment site profile.

Everything company-specific (message prefix, identities, projects, ports, CA settings)
comes from one JSON profile so the same code can be deployed at different companies and
intranets. Resolution order: POC_SITE env -> <runtime>/site.json -> config/site.json.
With no profile the built-in defaults reproduce the current single-site behaviour.
"""
import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULTS = {
    'org': {'id': 'enterprise', 'name': '企业协作平台'},
    'prefix': 'corp.dev.v1',
    'server': {'address': 'localhost', 'nats_port': 14222, 'ocsp_port': 2560,
               'dashboard_port': 8787, 'dashboard_identity': 'ag-a'},
    'ca': {'subject_prefix': 'PoC', 'ocsp_url': 'http://127.0.0.1:2560'},
    'defaults': {'caller': 'ag-a', 'target': 'ag-b', 'project': 'company-ai-network'},
    'projects': [{'id': 'company-ai-network', 'name': '企业协作平台工程验收'}],
    'alerts': {'handlers': {}, 'queue_seconds_max': 60, 'run_seconds_max': 600, 'auth_denied_total': 50,
               'project_age_seconds_max': 2592000, 'renewal_failures': 0, 'disk_free_gib_min': 2},
    'retention': {'tasks_days': 30, 'audit_days': 180, 'archive_days': 365,
                  'history_access_days': 180, 'worker_days': 30, 'backups_days': 30},
    'agents': [],
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def site_path(runtime=None):
    if os.environ.get('POC_SITE'):
        return Path(os.environ['POC_SITE'])
    candidates = []
    if runtime:
        candidates.append(Path(runtime) / 'site.json')
    if os.environ.get('POC_RUNTIME'):
        candidates.append(Path(os.environ['POC_RUNTIME']) / 'site.json')
    candidates.append(ROOT / 'config/site.json')
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load(runtime=None):
    profile = copy.deepcopy(DEFAULTS)
    path = site_path(runtime)
    if path and path.exists():
        profile = _merge(profile, json.loads(path.read_text()))
    return profile


def prefix(runtime=None):
    return load(runtime).get('prefix') or DEFAULTS['prefix']


def server(runtime=None):
    return load(runtime).get('server', DEFAULTS['server'])


def defaults(runtime=None):
    return load(runtime).get('defaults', DEFAULTS['defaults'])


def validate(profile):
    """Return a list of human-readable problems (empty means the profile is usable)."""
    problems = []
    if not isinstance(profile, dict):
        return ['profile must be a JSON object']
    prefix = profile.get('prefix')
    if not isinstance(prefix, str) or not prefix or prefix.endswith('.') or ' ' in prefix:
        problems.append('prefix must be a non-empty dotted token without spaces')
    server = profile.get('server', {})
    for field in ['nats_port', 'ocsp_port', 'dashboard_port']:
        value = server.get(field)
        if not isinstance(value, int) or not 1 <= value <= 65535:
            problems.append('server.%s must be a port number' % field)
    seen = set()
    for agent in profile.get('agents', []):
        if not isinstance(agent, dict):
            problems.append('each agent must be an object')
            continue
        for field in ['agent_id', 'device_id', 'employee_id', 'tool']:
            if not isinstance(agent.get(field), str) or not agent[field]:
                problems.append('agent is missing %s' % field)
        if agent.get('agent_id') in seen:
            problems.append('duplicate agent_id ' + str(agent.get('agent_id')))
        seen.add(agent.get('agent_id'))
        projects = agent.get('projects')
        if not isinstance(projects, list) or (not projects and agent.get('enabled', True)):
            problems.append('agent %s needs a non-empty projects list' % agent.get('agent_id'))
    defaults = profile.get('defaults', {})
    for field in ['caller', 'target']:
        if defaults.get(field) and defaults[field] not in seen and profile.get('agents') and defaults[field] not in {a.get('agent_id') for a in profile.get('agents', [])}:
            problems.append('defaults.%s is not a listed agent' % field)
    return problems
