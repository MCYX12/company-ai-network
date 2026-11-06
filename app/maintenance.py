"""Stateful maintenance drain: stop dispatch, confirm draining, settle, then stop.

The worker-side signal is still the existence of RUNTIME/maintenance; this module records
the maintenance run and makes the *operator* wait for real confirmation instead of setting
the flag and stopping the service. Waiting is bounded by a monotonic deadline: on expiry
the default is to defer maintenance, not to kill an in-flight answer. Only an explicit
force interrupts tasks, and the record keeps who/why and which tasks were affected.
"""
import json
import os
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

from .common import RUNTIME

FILE='maintenance.json'
MARKER='maintenance'
DEFAULT_MAX_WAIT=600
ACTIVE={'requested','draining','settling'}
TERMINAL={'succeeded','failed','expired','cancelled'}


def state_path(runtime=None):
    return Path(runtime or RUNTIME)/FILE


def marker_path(runtime=None):
    return Path(runtime or RUNTIME)/MARKER


def _write(state, runtime=None):
    path=state_path(runtime)
    path.parent.mkdir(parents=True,exist_ok=True)
    from .client_material import independent
    independent(path.parent,directory=True)
    if path.exists() or path.is_symlink():independent(path)
    # mkstemp creates a private, exclusive file on Windows too; no Linux-only flags
    # or predictable .tmp path which could truncate a linked target.
    fd,name=tempfile.mkstemp(prefix='.maintenance-',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(state,stream,ensure_ascii=False)
            stream.flush();os.fsync(stream.fileno())
        independent(path.parent,directory=True)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read(runtime=None):
    path=state_path(runtime)
    if path.is_symlink() or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError,ValueError):
        return None


def active(runtime=None):
    state=read(runtime)
    return bool(state and state.get('state') in ACTIVE)

def requested(identity, runtime=None):
    """A marker blocks only its selected targets. Invalid/legacy markers fail closed."""
    if not marker_path(runtime).exists():return False
    state=read(runtime)
    if not isinstance(state,dict):return True
    targets=state.get('targets')
    if not isinstance(targets,list) or not targets or not all(isinstance(t,str) and t for t in targets):return True
    return identity in targets


def _history(state,event,now,detail=None):
    entry={'event':event,'time':now}
    if detail:
        entry.update(detail)
    state.setdefault('history',[]).append(entry)


def begin(targets,max_wait=DEFAULT_MAX_WAIT,reason='',runtime=None,now=None):
    """Record a maintenance run and raise the workers' marker. Does not stop anything."""
    now=time.time() if now is None else now
    targets=sorted({t for t in (targets or []) if isinstance(t,str) and t})
    if not targets:
        raise ValueError('INVALID_TARGETS')
    max_wait=max(1,int(max_wait))
    state={'id':str(uuid.uuid4()),'targets':targets,'reason':str(reason)[:200],
           'created':now,'max_wait':max_wait,'deadline':now+max_wait,'state':'requested',
           'confirmed':[],'forced':False,'history':[]}
    _history(state,'requested',now,{'targets':targets})
    _write(state,runtime)
    marker=marker_path(runtime)
    marker.parent.mkdir(parents=True,exist_ok=True)
    marker.write_text(state['id'])
    return state


def pending_tasks(targets,runtime=None,path=None):
    """Active tasks per target, or None when the ledger is unreadable/not SQLite."""
    targets=sorted(set(targets or []))
    if not targets:
        return {}
    database=Path(path) if path else Path(runtime or RUNTIME)/'tasks.sqlite3'
    if not database.exists():
        return None
    try:
        db=sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True)
        placeholders=','.join('?'*len(targets))
        rows=db.execute(
            "SELECT target,count(*),min(deadline) FROM tasks WHERE state NOT IN (?,?,?,?) "
            "AND target IN ("+placeholders+") GROUP BY target",
            (*sorted(TERMINAL),*targets)).fetchall()
        return {row[0]:{'count':row[1],'deadline':row[2]} for row in rows}
    except sqlite3.Error:
        return None
    finally:
        try: db.close()
        except Exception: pass


def worker_pending(targets,runtime=None):
    """Local worker in-flight executions and unacknowledged results, per target.

    A worker whose result is persisted but not yet acknowledged by the Router still has
    work owed to the coordinator, so it must block maintenance settlement. Remote workers
    without a local ledger are simply not represented here.
    """
    runtime=Path(runtime or RUNTIME)
    out={}
    for target in sorted(set(targets or [])):
        path=runtime/('worker-'+target+'.sqlite3')
        if path.is_symlink() or not path.exists():
            continue
        db=None
        try:
            db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
            running=db.execute("SELECT count(*) FROM work WHERE state='running'").fetchone()[0]
            unacked=db.execute("SELECT count(*) FROM work WHERE state='done' AND acked=0").fetchone()[0]
            if running or unacked:
                out[target]={'running':running,'unacked':unacked}
        except sqlite3.Error:
            continue
        finally:
            try: db.close()
            except Exception: pass
    return out


def worker_unknown(targets,runtime=None,local_targets=None):
    """Local targets whose ledger cannot be read: unknown, NOT "no in-flight work".

    `local_targets` is the operator's discovery of agent processes for this runtime. A
    missing or unreadable ledger for one of them means the drain state cannot be confirmed,
    so it must block settlement instead of being silently skipped.
    """
    runtime=Path(runtime or RUNTIME)
    unknown={}
    for target in sorted(set(local_targets or [])):
        path=runtime/('worker-'+target+'.sqlite3')
        if path.is_symlink():
            unknown[target]='LEDGER_SYMLINK';continue
        if not path.exists():
            unknown[target]='LEDGER_MISSING';continue
        db=None
        try:
            db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
            db.execute("SELECT count(*) FROM work WHERE state='running'").fetchone()
            db.execute("SELECT count(*) FROM work WHERE state='done' AND acked=0").fetchone()
        except sqlite3.Error:
            unknown[target]='LEDGER_UNREADABLE'
        finally:
            try: db.close()
            except Exception: pass
    return unknown


def evaluate(state,draining_targets,runtime=None,now=None,pending=None,local_targets=None):
    """Advance one maintenance run from fresh observations; returns the state.

    draining_targets: identities currently advertising readiness=draining.
    pending: per-target active-task map (None = unknown ledger).
    """
    if state is None:
        return None
    now=time.time() if now is None else now
    draining_targets=sorted(set(draining_targets or []))
    if state.get('state') in ACTIVE:
        state['confirmed']=draining_targets
        if state['state']=='requested' and set(state['targets'])<=set(draining_targets):
            state['state']='draining'
            _history(state,'draining_confirmed',now,{'targets':draining_targets})
    if pending is None:
        pending=pending_tasks(state['targets'],runtime=runtime)
    workers=worker_pending(state['targets'],runtime)
    unknown=worker_unknown(state['targets'],runtime,local_targets)
    state['worker_pending']=workers
    state['worker_unknown']=unknown
    current=state.get('state')
    if current in {'draining','settling','deferred','forced'}:
        if pending=={} and not workers and not unknown:
            state['state']='settled'
            _history(state,'settled',now)
        elif state.get('forced'):
            # An explicit force eventually wins; it is the only path that interrupts work.
            if current!='forced':
                state['state']='forced'
                _history(state,'forced',now,{'pending':pending})
        elif now>=state['deadline'] and current in {'draining','settling'}:
            # Default is to defer maintenance, never to kill a running task.
            state['state']='deferred'
            _history(state,'deferred',now,{'pending':pending})
    state['updated']=now
    state['pending']=pending
    _write(state,runtime)
    return state


def mark_deferred(state,runtime=None,now=None,reason='MAINTENANCE_WAIT_EXCEEDED',blocking=None,unknown=None):
    """Persist a timeout as an explicit `deferred` decision (never inferred from silence)."""
    if state is None:
        raise ValueError('NO_MAINTENANCE')
    now=time.time() if now is None else now
    state['state']='deferred'
    state['deferred_reason']=reason
    state['blocking']=blocking if blocking is not None else state.get('pending')
    if unknown:
        state['worker_unknown']=unknown
    _history(state,'deferred',now,{'reason':reason,'blocking':state['blocking']})
    _write(state,runtime)
    return state


def mark_forced(state,runtime=None,now=None):
    if state is None:
        raise ValueError('NO_MAINTENANCE')
    state['forced']=True
    _history(state,'force_requested',time.time() if now is None else now)
    _write(state,runtime)
    return state


def close(state,runtime=None,final='done',now=None):
    """Finish or cancel a run: remove the marker so workers can accept tasks again."""
    now=time.time() if now is None else now
    if state is not None:
        state['state']=final
        _history(state,final,now)
        _write(state,runtime)
    marker=marker_path(runtime)
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    return state


def cancel(state=None,runtime=None,now=None):
    return close(state or read(runtime),runtime,final='cancelled',now=now)


def finish(state=None,runtime=None,now=None):
    return close(state or read(runtime),runtime,final='done',now=now)


def status(runtime=None,now=None):
    state=read(runtime)
    if state is None:
        return {'active':False,'state':'none'}
    now=time.time() if now is None else now
    state=dict(state)
    state['active']=state.get('state') in ACTIVE
    state['remaining_seconds']=max(0.0,float(state.get('deadline',now))-now)
    state['pending']=pending_tasks(state.get('targets') or [],runtime=runtime)
    state['worker_pending']=worker_pending(state.get('targets') or [],runtime)
    state['worker_unknown']=worker_unknown(state.get('targets') or [],runtime)
    return state
