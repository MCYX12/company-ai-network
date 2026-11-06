"""Administrator-owned enrollment. Agent messages cannot grant identity or rights."""
import json
import os
from pathlib import Path
import re
import time
from .common import ROOT, RUNTIME

ID = re.compile(r'^[a-z][a-z0-9-]{0,63}$')
# Built-in tool ids keep a default capability profile. Any other tool id is accepted
# when the enrollment declares its capabilities, so onboarding a self-developed agent
# never requires editing this file.
TOOLS = {'fixture', 'openclaw', 'hermes', 'codex', 'claude-code'}
ACTIONS = {'get_status', 'generate_text', 'ask_peer', 'get_project_progress'}
PROFILES = {'readonly': ['get_status', 'get_project_progress'],
            'dialogue': ['get_status', 'ask_peer', 'get_project_progress'],
            'worker': ['get_status', 'generate_text', 'ask_peer', 'get_project_progress']}
CAPABILITIES = {'fixture': list(PROFILES['readonly']),
                'codex': list(PROFILES['worker']), 'claude-code': list(PROFILES['worker'])}
BASE_FIELDS = {'agent_id', 'device_id', 'employee_id', 'tool', 'projects', 'enabled'}
OPTIONAL_FIELDS = {'capabilities'}
TALK_ACTIONS = {'ask_peer', 'generate_text'}

def registry_path():
    shared = os.environ.get('POC_AUTHZ_RUNTIME')
    if shared:
        # A configured authority is mandatory even when its file is temporarily missing.
        # Never substitute a stale local enrollment (including AGENT_REGISTRY).
        return Path(shared) / 'registry.json'
    if os.environ.get('AGENT_REGISTRY'):
        return Path(os.environ['AGENT_REGISTRY'])
    base = RUNTIME
    local = base/'registry.json'
    if local.exists():
        return local
    return RUNTIME/'registry.json' if (RUNTIME/'registry.json').exists() else ROOT/'config/registry.json'

def load_registry():
    rows=validate_registry(json.loads(registry_path().read_text()))
    states={}
    database=Path(os.environ.get('POC_AUTHZ_RUNTIME') or RUNTIME)/'accounts.sqlite3'
    if database.exists():
        import sqlite3
        try:
            with sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True) as db:
                states=dict(db.execute('SELECT id,state FROM members'))
            for row in rows.values():
                if row['employee_id'] in states and states[row['employee_id']]!='active':row['enabled']=False
        except sqlite3.Error:
            raise ValueError('Account registry unavailable')
    iam_problems=[]
    for row in rows.values():
        if row['employee_id'].startswith('member-') and states.get(row['employee_id'])!='active':row['enabled']=False
        blocked, problem = _iam_state(row['employee_id'])
        if problem:
            # The authority could not be read: fail closed and surface the reason instead of
            # silently admitting the identity (assessment 2026-10-06).
            row['enabled']=False; row['iam_problem']=problem
            iam_problems.append({'agent_id':row['agent_id'],'employee_id':row['employee_id'],'problem':problem})
        elif blocked:
            # A manual offboard or an OIDC-synced suspended/left status must gate business
            # admission without editing the registry by hand.
            row['enabled']=False
    _IAM_PROBLEMS[:] = iam_problems
    _persist_iam_problems(iam_problems)
    return rows


_IAM_PROBLEMS=[]


def _persist_iam_problems(problems):
    """Persist the admission diagnostic so a separate dashboard process can read it.

    Compare against the on-disk payload (not an in-process cache) so each runtime path is
    correct and a recovery writes an empty list with a fresh observed_at.
    """
    path = RUNTIME / 'health' / 'admission-problems.json'
    try:
        previous=admission_snapshot()
        if previous['observed_at'] is not None and not previous.get('snapshot_error') and previous['problems'] == problems:
            return
    except (OSError, ValueError):
        pass
    payload = {'observed_at': time.time(), 'problems': problems}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    except OSError:
        pass


def admission_snapshot(runtime=None):
    """Read the persisted admission diagnostic written by the Router process."""
    path = (runtime or RUNTIME) / 'health' / 'admission-problems.json'
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {'observed_at': None, 'problems': []}
    except (OSError, ValueError):
        value=None
    if (not isinstance(value,dict) or type(value.get('observed_at')) not in (int,float)
        or not 0<=value['observed_at']<=253402300799
        or not isinstance(value.get('problems'),list)
        or any(not isinstance(row,dict) or any(not isinstance(row.get(key),str) or not row[key]
               for key in ('agent_id','employee_id','problem')) for row in value['problems'])):
        return {'observed_at':None,'problems':[],'snapshot_error':'ADMISSION_SNAPSHOT_INVALID'}
    return {'observed_at':value['observed_at'],'problems':value['problems']}


def iam_problems():
    """Admission problems seen at the last load_registry(), for admin diagnostics."""
    return list(_IAM_PROBLEMS)


def _iam_state(employee_id):
    """(blocked, problem): a read error always fails closed.

    A legitimate 'unknown' (no roster row, no accounts row) is not blocked by default, but
    POC_IAM_STRICT=1 freezes a strict enterprise policy that also blocks unmanaged identities.
    """
    from . import iam
    try:
        state = iam.status(employee_id)
    except Exception:
        return True, 'IAM_DEPENDENCY_UNAVAILABLE'
    if state in iam.BLOCKED:
        return True, None
    if state == 'unknown' and os.environ.get('POC_IAM_STRICT') == '1':
        return True, 'IAM_UNKNOWN_NOT_MANAGED'
    return False, None

def validate_registry(value):
    if value.get('schema_version') != 1 or not isinstance(value.get('agents'), list):
        raise ValueError('Invalid registry')
    result = {}
    for row in value['agents']:
        if not isinstance(row, dict) or not BASE_FIELDS <= set(row) <= BASE_FIELDS | OPTIONAL_FIELDS:
            raise ValueError('Unexpected registry fields')
        for name in ['agent_id','device_id','employee_id']:
            if not isinstance(row[name], str) or not ID.fullmatch(row[name]):
                raise ValueError('Invalid identity')
        if row['agent_id'] in result or row['agent_id'] in {'router','server','unregistered'}:
            raise ValueError('Duplicate or reserved identity')
        if not isinstance(row['tool'], str) or not ID.fullmatch(row['tool']) or not isinstance(row['enabled'], bool):
            raise ValueError('Invalid tool or enabled flag')
        if 'capabilities' in row:
            claimed = row['capabilities']
            if (not isinstance(claimed, list) or not claimed
                    or not all(isinstance(c, str) and c in ACTIONS for c in claimed)
                    or len(set(claimed)) != len(claimed)):
                raise ValueError('Invalid capabilities')
        if not isinstance(row['projects'], list) or not all(isinstance(p,str) and ID.fullmatch(p) for p in row['projects']):
            raise ValueError('Invalid projects')
        result[row['agent_id']] = row
    return result

def capabilities(row):
    """Declared enrollment wins, otherwise the built-in tool profile."""
    declared = row.get('capabilities')
    if declared is not None:
        return list(declared)
    return list(CAPABILITIES.get(row['tool'], []))


def profile(name):
    if name not in PROFILES:
        raise ValueError('Unknown capability profile')
    return list(PROFILES[name])

def visible(source, target, registry):
    a,b = registry.get(source),registry.get(target)
    return bool(a and b and a['enabled'] and b['enabled'] and
                (source == target or set(a['projects']) & set(b['projects'])))
