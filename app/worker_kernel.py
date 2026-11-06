"""Reliable-execution kernel shared by the built-in and standalone device workers.

Both worker paths must apply one contract: validate the envelope, refuse a stale dispatch
epoch, refuse an expired deadline, claim the task exactly once, check that every supplied data
grant is still valid *before* reading any material, persist the result, and keep republishing
it until the Router acknowledges it. The standalone client originally subscribed to Core NATS
and answered in one shot, so a Router outage during execution lost the result and a redelivery
could re-run the model; the built-in worker already had the durable core. This module is that
core, extracted so the client imports the same code instead of a second implementation.

It is stdlib-only on purpose: scripts/build_client_installer.py ships it inside the client
package (CLIENT_MODULES), and app/task_worker.py imports the same file on the server.
"""
import asyncio
import hashlib
import sys
import inspect
import json
import os
import signal
import subprocess
import sqlite3
import time
from pathlib import Path

# Bumped when the worker wire/behavior contract changes, so a heartbeat can show which
# contract a device actually runs instead of inferring it from the release number.
CONTRACT_VERSION = 'worker-kernel-1'


def process_options():
    """Keep an answering command in its own process group on either OS."""
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def terminate_process_tree(process):
    """Timeouts must terminate descendants as well as the answering process."""
    if os.name == 'nt':
        taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32'/'taskkill.exe'
        result = subprocess.run([str(taskkill), '/PID', str(process.pid), '/T', '/F'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode and process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

REQUIRED_FIELDS = frozenset({'task_id', 'target', 'operation', 'payload', 'deadline'})
OPTIONAL_FIELDS = frozenset({'notes_grants', 'history_grants', 'lease_epoch'})


class EnvelopeError(ValueError):
    """The dispatch envelope is not a valid task for this identity."""


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def open_db(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    return db


def validate_envelope(body, identity):
    """Return the envelope when it is a well-formed task for identity, else raise EnvelopeError.

    The key set is exact: a Router and both worker implementations agree on what a task is, so
    an unexpected field cannot smuggle a different meaning past one of them.
    """
    if not isinstance(body, dict):
        raise EnvelopeError('not an object')
    keys = set(body)
    if not REQUIRED_FIELDS <= keys or not keys <= (REQUIRED_FIELDS | OPTIONAL_FIELDS):
        raise EnvelopeError('unexpected fields')
    if body.get('target') != identity:
        raise EnvelopeError('wrong target')
    if not isinstance(body.get('task_id'), str) or not body['task_id']:
        raise EnvelopeError('task_id')
    if not isinstance(body.get('operation'), str) or not body['operation']:
        raise EnvelopeError('operation')
    if not isinstance(body.get('payload'), dict):
        raise EnvelopeError('payload')
    deadline = body.get('deadline')
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise EnvelopeError('deadline')
    for key in ('notes_grants', 'history_grants'):
        if key in body and not isinstance(body[key], list):
            raise EnvelopeError(key)
    return body


def sandbox_prefix(entry):
    """Validated argv prefix for an optional OS sandbox wrapper, or [].

    The platform cannot create one portable OS identity boundary on every device, so a device
    owner that needs one configures the wrapper (bwrap, systemd-run, sandbox-exec, runas, ...)
    as a string list under "sandbox". An invalid value is refused rather than silently ignored,
    because running unsandboxed while believing otherwise is worse than not running at all.
    """
    sandbox = entry.get('sandbox') if isinstance(entry, dict) else None
    if sandbox is None:
        return []
    if (not isinstance(sandbox, list) or not sandbox
            or not all(isinstance(item, str) and item for item in sandbox)):
        raise ValueError('SANDBOX_INVALID')
    return list(sandbox)


def expired(deadline, now=None):
    now = time.time() if now is None else now
    return float(deadline) <= now


def active_grants(grants, now=None, require_root=True):
    """Split grants into (usable, rejected) checking approval and expiry *before* any read.

    A grant that is not approved, or whose expires has already passed, is never handed to the
    executor; it is reported so the caller sees why material was withheld instead of silently
    receiving an answer built from a revoked/expired permission. History grants carry a scope
    rather than a root, so require_root=False for them.
    """
    now = time.time() if now is None else now
    usable, rejected = [], []
    for grant in grants or []:
        if not isinstance(grant, dict):
            continue
        if require_root and not isinstance(grant.get('root'), str):
            continue
        state = grant.get('state')
        if state is not None and state != 'approved':
            rejected.append({'id': grant.get('id'), 'root': grant.get('root'), 'reason': 'NOT_APPROVED'})
            continue
        expires = grant.get('expires')
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires <= now:
            rejected.append({'id': grant.get('id'), 'root': grant.get('root'), 'reason': 'EXPIRED'})
            continue
        usable.append(grant)
    return usable, rejected


class WorkerLedger:
    """Durable per-worker state: exactly-once claim, result outbox, ack, epoch fencing."""

    def __init__(self, path):
        self.db = open_db(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS work(task_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,'
                        'state TEXT NOT NULL,result TEXT,acked INTEGER NOT NULL DEFAULT 0,updated_at REAL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS worker_state(key TEXT PRIMARY KEY, value TEXT)')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(work)')}
        if 'updated_at' not in columns:
            self.db.execute('ALTER TABLE work ADD COLUMN updated_at REAL')
        # A read-only operation may be retried explicitly with a new task ID; never silently repeat
        # an uncertain job. A worker that died mid-execution reports WORKER_INTERRUPTED on restart.
        with self.db:
            self.db.execute("UPDATE work SET state='done',result=?,updated_at=? WHERE state='running'",
                            (json.dumps({'code': 'WORKER_INTERRUPTED', 'data': None}), time.time()))
            self.db.execute('UPDATE work SET updated_at=? WHERE updated_at IS NULL', (time.time(),))

    def claim(self, envelope):
        """True exactly once per task_id; False for a repeat; ValueError on a conflicting envelope."""
        task_id = envelope['task_id']
        fingerprint = digest(envelope)
        with self.db:
            row = self.db.execute('SELECT * FROM work WHERE task_id=?', (task_id,)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    raise ValueError('TASK_CONFLICT')
                return False
            self.db.execute("INSERT INTO work(task_id,fingerprint,state,updated_at) VALUES(?,?,'running',?)",
                            (task_id, fingerprint, time.time()))
            return True

    def finish(self, task_id, result):
        with self.db:
            self.db.execute("UPDATE work SET state='done',result=?,updated_at=? WHERE task_id=?",
                            (json.dumps(result), time.time(), task_id))

    def outbox(self, limit=20):
        return [(row['task_id'], json.loads(row['result'])) for row in self.db.execute(
            "SELECT task_id,result FROM work WHERE state='done' AND acked=0 LIMIT ?", (limit,))]

    def pending(self):
        """Unacked results still owed to the coordinator (outbox backlog)."""
        row = self.db.execute("SELECT count(*),min(updated_at) FROM work WHERE state='done' AND acked=0").fetchone()
        return {'unacked': row[0], 'oldest_updated_at': row[1]}

    def acknowledge(self, task_id):
        with self.db:
            self.db.execute('UPDATE work SET acked=1 WHERE task_id=?', (task_id,))

    def stale_epoch(self, epoch):
        """Local monotonic fencing for a worker without the shared KV lease.

        True (and unrecorded) when epoch is older than the newest accepted dispatch, so a
        delayed message from a previous Router lease is never executed. None is not stale.
        """
        if epoch is None:
            return False
        try:
            epoch = int(epoch)
        except (TypeError, ValueError):
            return True
        row = self.db.execute("SELECT value FROM worker_state WHERE key='lease_epoch'").fetchone()
        last = None
        if row is not None:
            try:
                last = int(row['value'])
            except (TypeError, ValueError):
                last = None
        if last is not None and epoch < last:
            return True
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO worker_state(key,value) VALUES('lease_epoch',?)", (str(epoch),))
        return False


class ReliableTaskRunner:
    """Validate -> fence -> claim -> execute -> persist, shared by both workers.

    execute(envelope, notes_grants, history_grants) is async (or sync) and returns
    {'code', 'data'}. publish_started(task_id) and publish_result(task_id, result) are async
    transport callbacks. stale(epoch) is an optional fencing check; when omitted the ledger's
    local monotonic check is used. Results are persisted and published from the outbox by the
    caller's pump, so a Router that was offline during execution still receives them on recovery.
    """

    def __init__(self, identity, ledger, execute, publish_started, publish_result,
                 stale=None, clock=time.time, grant_filter=active_grants):
        self.identity = identity
        self.ledger = ledger
        self._execute = execute
        self._publish_started = publish_started
        self._publish_result = publish_result
        self._stale = stale
        self._clock = clock
        self._grant_filter = grant_filter
        # Number of claimed tasks currently executing. A draining worker waits for this to
        # reach zero rather than killing an answer mid-flight.
        self.inflight = 0
        self.notice_failures = 0     # started-notice transport failures (observability)

    async def wait_idle(self, timeout):
        """Wait for claimed work to finish, bounded; True when nothing is executing."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self.inflight > 0 and time.monotonic() < deadline:
            await asyncio.sleep(.2)
        return self.inflight == 0

    def activity(self):
        """Live platform work metadata, without task IDs, prompts or business conclusions."""
        backlog=self.ledger.pending()
        return {'source':'live_worker_runtime','scope':'platform_managed_queries_only',
                'state':'running' if self.inflight else 'result_pending' if backlog['unacked'] else 'idle',
                'executing_count':self.inflight,'results_pending':backlog['unacked'],
                'oldest_result_at':backlog['oldest_updated_at'],'observed_at':self._clock(),
                'progress':None,'desktop_sessions_included':False}

    async def handle(self, body):
        """Return one of rejected/duplicate/stale/done. Never raises for a bad envelope."""
        try:
            envelope = validate_envelope(body, self.identity)
        except EnvelopeError:
            return 'rejected'
        now = self._clock()
        if self._stale is not None:
            stale = await maybe_await(self._stale(envelope.get('lease_epoch')))
        else:
            stale = self.ledger.stale_epoch(envelope.get('lease_epoch'))
        if stale:
            await self._publish_result(envelope['task_id'],
                                       {'code': 'STALE_DISPATCH', 'data': {'reason': 'stale dispatch lease epoch'}})
            return 'stale'
        try:
            claimed = self.ledger.claim(envelope)
        except ValueError:
            return 'duplicate'      # same task_id with a conflicting envelope: never re-run
        if not claimed:
            return 'duplicate'
        self.inflight += 1
        try:
            # The started notice is best-effort: the Router keeps redelivering until it sees a
            # terminal result and the claim prevents a second execution, so a transient publish
            # failure must never leave a claimed task stuck at 'running' with no result.
            try:
                await self._publish_started(envelope['task_id'])
            except Exception as error:
                # Observability: the task still runs, but an administrator should be able to see
                # that a transport failure happened instead of it being silently swallowed.
                self.notice_failures += 1
                print('worker: started notice failed for %s: %s'
                      % (envelope.get('task_id'), type(error).__name__), file=sys.stderr)
            started = self._clock()
            if expired(envelope['deadline'], now):
                result = {'code': 'EXPIRED', 'data': None}
            else:
                # Authorization is checked here, immediately before any material is read. Directory
                # (notes) grants are material: a non-approved or expired one is dropped. History grants
                # are a tri-state administrative veto evaluated by history_scope inside the execution
                # path, so they are passed through unfiltered -- dropping a 'denied' grant would turn an
                # explicit veto into a silent allow (regression found by the live skill drill).
                usable, rejected = self._grant_filter(envelope.get('notes_grants'), now=now)
                history = list(envelope.get('history_grants') or [])
                try:
                    result = await maybe_await(self._execute(envelope, usable, history))
                except Exception:
                    result = {'code': 'EXECUTION_FAILED', 'data': None}
                if not isinstance(result, dict):
                    result = {'code': 'EXECUTION_FAILED', 'data': None}
                if rejected and isinstance(result.get('data'), dict):
                    result['data'] = {**result['data'], 'directory_grants_rejected': rejected}
            self._stamp_timings(result, received=now, started=started, finished=self._clock())
            self.ledger.finish(envelope['task_id'], result)
            # Publish right away so the caller does not wait for the next outbox pump tick (up to 1s).
            # The result stays unacked in the ledger, so the pump still resends it until the Router
            # acknowledges; a duplicate delivery is safe (the Router's terminal guard ignores it).
            try:
                await self._publish_result(envelope['task_id'], result)
            except Exception:
                pass
        finally:
            self.inflight -= 1
        return 'done'

    @staticmethod
    def _stamp_timings(result, received, started, finished):
        """Attach the worker-side phases the task audit ledger cannot see.

        The Router already records accepted/dispatched/running/terminal times; these are the
        execution phases (claim, run) that decide whether a slow answer was material building,
        process startup or model generation. Only results that already carry a data object are
        annotated, so a bare failure shape is left untouched.
        """
        if not isinstance(result, dict) or not isinstance(result.get('data'), dict):
            return
        existing = result['data'].get('timings')
        timings = dict(existing) if isinstance(existing, dict) else {}
        timings.update({'received_at': received,
                        'claim_seconds': round(max(0.0, started - received), 3),
                        'execution_seconds': round(max(0.0, finished - started), 3),
                        'total_seconds': round(max(0.0, finished - received), 3)})
        result['data']['timings'] = timings
