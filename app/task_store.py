"""Single-host durable PoC ledger. PostgreSQL/JetStream HA is a later stage.

State machine (plan section 10.3): accepted -> queued -> dispatched -> running ->
succeeded/failed/expired/cancelled. Per-target delivery concurrency and bounded
redelivery follow section 10.2. Offline targets stay resumable until the deadline
(REL-03); only repeated deliveries that never start become dead letters (REL-04).
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .worker_kernel import WorkerLedger

TERMINAL={'succeeded','failed','expired','cancelled'}
ACTIVE=('accepted','queued','dispatched','running','cancel_requested')
MAX_ATTEMPTS=int(os.environ.get('POC_MAX_ATTEMPTS','5'))
CONCURRENCY=int(os.environ.get('POC_DELIVERY_CONCURRENCY','2'))
BACKOFF=tuple(int(value) for value in os.environ.get('POC_DELIVERY_BACKOFF','2,5,10,20,30').split(','))

def backoff(count):
    return BACKOFF[min(max(count,1),len(BACKOFF))-1]

def open_db(path):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600);os.close(fd)
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    return db

def digest(data):
    return hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

class TaskStore:
    def __init__(self,path):
        self.db=open_db(path)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS tasks(
          task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, target TEXT NOT NULL,
          idempotency_key TEXT NOT NULL, fingerprint TEXT NOT NULL, operation TEXT NOT NULL,
          payload TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, deadline REAL NOT NULL,
          next_send REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
          deferrals INTEGER NOT NULL DEFAULT 0, result TEXT,
          UNIQUE(owner,idempotency_key));
        CREATE TABLE IF NOT EXISTS task_audit(
          id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, event TEXT NOT NULL, timestamp REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS task_archive(
          task_id TEXT PRIMARY KEY, owner TEXT, target TEXT, operation TEXT, state TEXT,
          created_at REAL, deadline REAL, attempts INTEGER, result TEXT, archived_at REAL);
        ''')
        columns={row['name'] for row in self.db.execute('PRAGMA table_info(tasks)')}
        if 'deferrals' not in columns:
            self.db.execute('ALTER TABLE tasks ADD COLUMN deferrals INTEGER NOT NULL DEFAULT 0')
        if 'cancel_state' not in columns:
            # Execution state and cancel outcome are tracked separately (assessment: CANNOT_CANCEL is not a failure).
            self.db.execute("ALTER TABLE tasks ADD COLUMN cancel_state TEXT NOT NULL DEFAULT 'none'")
        # Controlled recovery for an interrupted READ-ONLY task: one new attempt, same target,
        # recorded here so a retry of a retry can never happen and the linkage stays auditable.
        self.db.execute('CREATE TABLE IF NOT EXISTS retries(root_task_id TEXT PRIMARY KEY,'
                        'retry_task_id TEXT,attempt INTEGER,reason TEXT,created REAL)')

    def audit(self,task_id,event):
        self.db.execute('INSERT INTO task_audit(task_id,event,timestamp) VALUES(?,?,?)',(task_id,event,time.time()))

    def submit(self,owner,target,key,operation,payload,ttl):
        fingerprint=digest({'target':target,'operation':operation,'payload':payload,'ttl':ttl})
        with self.db:
            old=self.db.execute('SELECT * FROM tasks WHERE owner=? AND idempotency_key=?',(owner,key)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:raise ValueError('IDEMPOTENCY_CONFLICT')
                return dict(old)
            count=self.db.execute("SELECT count(*) FROM tasks WHERE state NOT IN ('succeeded','failed','expired','cancelled')").fetchone()[0]
            if count>=500:raise ValueError('RATE_LIMITED')
            task_id=str(uuid.uuid4());now=time.time()
            self.db.execute('INSERT INTO tasks(task_id,owner,target,idempotency_key,fingerprint,operation,payload,state,created_at,deadline) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (task_id,owner,target,key,fingerprint,operation,json.dumps(payload),'accepted',now,now+ttl))
            self.audit(task_id,'accepted')
        return self.get(task_id)

    def submit_retry(self,root_task_id,owner,target,operation,payload,ttl,reason='WORKER_INTERRUPTED'):
        """Create the one replacement task and its linkage in a single transaction.

        A crash between two commits would otherwise leave a new task with no recorded parent,
        which the client could not follow and the audit could not explain.
        """
        key='retry-'+root_task_id
        fingerprint=digest({'target':target,'operation':operation,'payload':payload,'ttl':ttl})
        with self.db:
            existing=self.db.execute('SELECT retry_task_id FROM retries WHERE root_task_id=?',(root_task_id,)).fetchone()
            if existing:
                return self.get(existing['retry_task_id'])
            if not self.db.execute('SELECT 1 FROM tasks WHERE task_id=?',(root_task_id,)).fetchone():
                raise ValueError('UNKNOWN_ROOT_TASK')
            old=self.db.execute('SELECT * FROM tasks WHERE owner=? AND idempotency_key=?',(owner,key)).fetchone()
            if old:
                self.db.execute('INSERT OR IGNORE INTO retries VALUES(?,?,?,?,?)',
                                (root_task_id,old['task_id'],2,reason,time.time()))
                return dict(old)
            count=self.db.execute("SELECT count(*) FROM tasks WHERE state NOT IN ('succeeded','failed','expired','cancelled')").fetchone()[0]
            if count>=500:raise ValueError('RATE_LIMITED')
            task_id=str(uuid.uuid4());now=time.time()
            self.db.execute('INSERT INTO tasks(task_id,owner,target,idempotency_key,fingerprint,operation,payload,state,created_at,deadline) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (task_id,owner,target,key,fingerprint,operation,json.dumps(payload),'accepted',now,now+ttl))
            self.db.execute('INSERT INTO retries VALUES(?,?,?,?,?)',(root_task_id,task_id,2,reason,now))
            self.audit(task_id,'accepted')
        return self.get(task_id)

    def get(self,task_id):
        row=self.db.execute('SELECT * FROM tasks WHERE task_id=?',(task_id,)).fetchone()
        return dict(row) if row else None

    def record_retry(self,root_task_id,retry_task_id,attempt,reason):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO retries VALUES(?,?,?,?,?)',
                            (root_task_id,retry_task_id,int(attempt),str(reason)[:200],time.time()))

    def retry_for(self,task_id):
        row=self.db.execute('SELECT * FROM retries WHERE root_task_id=?',(task_id,)).fetchone()
        return dict(row) if row else None

    def retried_from(self,task_id):
        row=self.db.execute('SELECT * FROM retries WHERE retry_task_id=?',(task_id,)).fetchone()
        return dict(row) if row else None

    def is_retry(self,task_id):
        return self.retried_from(task_id) is not None

    def history(self, task_id):
        return [dict(r) for r in self.db.execute(
            'SELECT event,timestamp FROM task_audit WHERE task_id=? ORDER BY id DESC LIMIT 100',
            (task_id,))][::-1]

    def inflight_counts(self):
        rows=self.db.execute("SELECT target,count(*) AS n FROM tasks WHERE state IN ('dispatched','running') GROUP BY target")
        return {row['target']:row['n'] for row in rows}

    def list_owned(self, owner, permitted, state=None, limit=20, before=None):
        # Filter authorization before pagination. Never include payload/result bodies in lists.
        clauses=['owner=?'];args=[owner]
        if state:
            clauses.append('state=?');args.append(state)
        if before is not None:
            clauses.append('rowid<?');args.append(before)
        rows=[]
        for row in self.db.execute('SELECT rowid AS sequence,* FROM tasks WHERE '+
                ' AND '.join(clauses)+' ORDER BY rowid DESC',args):
            item=dict(row)
            if permitted(item):
                rows.append(item)
                if len(rows)>limit:break
        has_more=len(rows)>limit
        page=rows[:limit]
        tasks=[]
        for row in page:
            public=self.public(row)
            result=public.pop('result')
            public['result_code']=result.get('code') if isinstance(result,dict) else None
            event=self.db.execute('SELECT timestamp FROM task_audit WHERE task_id=? ORDER BY id DESC LIMIT 1',
                                  (row['task_id'],)).fetchone()
            public['updated_at']=event['timestamp'] if event else row['created_at']
            tasks.append(public)
        return {'tasks':tasks,'next_cursor':page[-1]['sequence'] if has_more else None}

    def due(self,now=None):
        now=time.time() if now is None else now
        with self.db:
            expired=self.db.execute("SELECT task_id FROM tasks WHERE deadline<=? AND state NOT IN ('succeeded','failed','expired','cancelled')",(now,)).fetchall()
            for row in expired:
                self.db.execute("UPDATE tasks SET state='expired',result=? WHERE task_id=?",(json.dumps({'code':'EXPIRED'}),row['task_id']))
                self.audit(row['task_id'],'expired')
        return [dict(r) for r in self.db.execute("SELECT * FROM tasks WHERE state NOT IN ('succeeded','failed','expired','cancelled') AND next_send<=? ORDER BY created_at LIMIT 20",(now,))]

    def defer(self,task_id,now):
        """Target offline or not ready: stay resumable until the deadline (REL-03)."""
        with self.db:
            row=self.get(task_id)
            if not row or row['state'] not in ACTIVE:return False
            deferrals=row['deferrals']+1
            self.db.execute("UPDATE tasks SET state='queued',deferrals=?,next_send=? WHERE task_id=?",
                            (deferrals,now+backoff(deferrals),task_id))
            self.audit(task_id,'deferred')
        return True

    def hold(self,task_id,now):
        """Per-target concurrency reached: healthy backpressure, never a dead letter."""
        with self.db:
            row=self.get(task_id)
            if not row or row['state'] not in ACTIVE:return False
            self.db.execute("UPDATE tasks SET state='queued',next_send=? WHERE task_id=?",(now+1,task_id))
        return True

    def requeue_stale(self,task_id,now=None):
        """A worker rejected a stale dispatch epoch: requeue for the current lease holder.

        Without this the task stays 'dispatched' until the next backoff, so a
        rejected stale dispatch is recorded and recovered instead of silently
        occupying an in-flight slot.
        """
        now=time.time() if now is None else now
        with self.db:
            row=self.get(task_id)
            if not row or row['state'] not in ('dispatched','running') or row['deadline']<=now:return False
            changed=self.db.execute("UPDATE tasks SET state='queued',next_send=? WHERE task_id=? AND state IN ('dispatched','running')",
                                    (now,task_id)).rowcount
            if changed:self.audit(task_id,'stale_dispatch')
        return bool(changed)

    def mark_running(self,task_id):
        """Worker confirmed it began the task; stop redelivering until the deadline."""
        with self.db:
            row=self.get(task_id)
            if not row or row['state'] not in ACTIVE:return False
            changed=self.db.execute("UPDATE tasks SET state='running',next_send=? WHERE task_id=? AND state IN ('accepted','queued','dispatched')",
                                    (row['deadline'],task_id)).rowcount
            if changed:self.audit(task_id,'running')
        return bool(changed)

    def can_send(self,task_id,now=None):
        """True when another delivery attempt is allowed, without touching state."""
        now=time.time() if now is None else now
        row=self.get(task_id)
        return bool(row and row['state'] in ('accepted','queued','dispatched')
                    and row['deadline']>now and row['attempts']+1<=MAX_ATTEMPTS)

    def record_send_failure(self,task_id,now=None):
        """A transport publish/ack failed: count the attempt and back off, or
        dead-letter after MAX_ATTEMPTS. Distinct from a worker's failed execution."""
        now=time.time() if now is None else now
        with self.db:
            row=self.get(task_id)
            if not row or row['state'] not in ('accepted','queued','dispatched'):return False
            attempts=row['attempts']+1
            if attempts>MAX_ATTEMPTS:
                self.db.execute("UPDATE tasks SET state='failed',result=? WHERE task_id=?",
                                (json.dumps({'code':'DELIVERY_FAILED','data':None}),task_id))
                self.audit(task_id,'dead_letter')
                return False
            self.db.execute("UPDATE tasks SET state='queued',attempts=?,next_send=? WHERE task_id=?",
                            (attempts,now+backoff(attempts),task_id))
            self.audit(task_id,'send_failed')
        return True

    def plan(self,now,permitted,eligible,reserve=False):
        """Pick tasks to deliver at this instant.

        permitted(row) -> authorization still holds; eligible(target,operation) ->
        target heartbeat fresh and advertises the operation. Returns rows to publish.

        reserve=True lets a durable transport (JetStream) publish first: the row is
        returned without being marked dispatched, and the caller must call sent()
        only after PubAck, or record_send_failure() when the publish fails.
        """
        inflight=self.inflight_counts();chosen=[]
        for row in self.due(now):
            task_id=row['task_id']
            if not permitted(row):
                self.finish(task_id,{'code':'FORBIDDEN','data':None})
                continue
            if not eligible(row['target'],row['operation']):
                self.defer(task_id,now)
                continue
            if inflight.get(row['target'],0)>=CONCURRENCY:
                self.hold(task_id,now)
                continue
            if reserve:
                if not self.can_send(task_id,now):
                    self.record_send_failure(task_id,now)
                    continue
            elif not self.sent(task_id,now):
                continue
            inflight[row['target']]=inflight.get(row['target'],0)+1
            chosen.append(self.get(task_id))
        return chosen

    def dead_letters(self, limit=100):
        rows=self.db.execute("SELECT * FROM tasks WHERE state='failed' AND result LIKE '%DELIVERY_FAILED%' ORDER BY created_at LIMIT ?",(limit,))
        return [dict(row) for row in rows]

    def requeue(self, task_id, actor='admin'):
        """Manual replay of one dead letter: reset attempts and dispatch from scratch."""
        with self.db:
            row=self.get(task_id)
            if not row:raise ValueError('NOT_FOUND')
            result=json.loads(row['result']) if row['result'] else {}
            if row['state']!='failed' or result.get('code')!='DELIVERY_FAILED':raise ValueError('NOT_DEAD_LETTER')
            if row['deadline']<=time.time():raise ValueError('DEADLINE_PASSED')
            self.db.execute("UPDATE tasks SET state='accepted',attempts=0,next_send=0,deferrals=0,result=NULL WHERE task_id=?",(task_id,))
            self.audit(task_id,'requeued')
        return self.get(task_id)

    def purge_dead_letters(self, before):
        """Archive and delete dead letters created before a cutoff, bounding the live ledger."""
        with self.db:
            rows=self.db.execute("SELECT * FROM tasks WHERE state='failed' AND result LIKE '%DELIVERY_FAILED%' AND created_at<?",(before,)).fetchall()
            now=time.time()
            for row in rows:
                self.db.execute('INSERT OR REPLACE INTO task_archive VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (row['task_id'],row['owner'],row['target'],row['operation'],row['state'],row['created_at'],row['deadline'],row['attempts'],row['result'],now))
                self.db.execute('DELETE FROM tasks WHERE task_id=?',(row['task_id'],))
                self.audit(row['task_id'],'purged')
        return len(rows)

    def quota_status(self, limit_bytes=None):
        limit=limit_bytes or int(os.environ.get('POC_TASKS_DB_QUOTA','67108864'))
        page_count=self.db.execute('PRAGMA page_count').fetchone()[0]
        page_size=self.db.execute('PRAGMA page_size').fetchone()[0]
        size=page_count*page_size
        pending=self.db.execute("SELECT count(*) FROM tasks WHERE state NOT IN ('succeeded','failed','expired','cancelled')").fetchone()[0]
        return {'size_bytes':size,'limit_bytes':limit,'over_quota':size>limit,'dead_letters':len(self.dead_letters(100000)),'pending':pending}

    def enforce_quota(self, limit_bytes=None, before=None):
        """Archive dead letters until under quota. Never deletes active tasks."""
        if not self.quota_status(limit_bytes)['over_quota']:
            return {'purged':0,**self.quota_status(limit_bytes)}
        cutoff=time.time() if before is None else before
        purged=self.purge_dead_letters(cutoff)
        return {'purged':purged,**self.quota_status(limit_bytes)}

    def cancel(self, task_id, owner):
        # Pre-dispatch cancellations are immediate; dispatched/running tasks enter a
        # cooperative cancel_requested state until the worker confirms (REL-03).
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row=self.get(task_id)
            if not row or row['owner']!=owner:
                return 'FORBIDDEN',None
            if row['state']=='cancelled':return 'CANCELLED',row
            if row['state'] in TERMINAL:return 'ALREADY_FINISHED',row
            if row['state']=='cancel_requested':return 'CANCEL_REQUESTED',row
            if row['state'] in ('accepted','queued') and row['attempts']==0:
                state='expired' if row['deadline']<=time.time() else 'cancelled'
                cancel='expired' if state=='expired' else 'cancelled'
                self.db.execute('UPDATE tasks SET state=?,result=?,cancel_state=? WHERE task_id=?',
                                (state,json.dumps({'code':state.upper(),'data':None}),cancel,task_id))
                self.audit(task_id,state)
                return state.upper(),self.get(task_id)
            self.db.execute("UPDATE tasks SET state='cancel_requested',cancel_state='requested',result=?,next_send=? WHERE task_id=?",
                            (json.dumps({'code':'CANCEL_REQUESTED','data':None}),row['deadline'],task_id))
            self.audit(task_id,'cancel_requested')
        return 'CANCEL_REQUESTED',self.get(task_id)

    def confirm_cancelled(self, task_id):
        with self.db:
            row=self.get(task_id)
            if not row or row['state'] in TERMINAL:return False
            self.db.execute("UPDATE tasks SET state='cancelled',cancel_state='cancelled',result=? WHERE task_id=?",
                            (json.dumps({'code':'CANCELLED','data':None}),task_id))
            self.audit(task_id,'cancelled')
        return True

    def mark_cannot_cancel(self,task_id):
        """Cancellation failed: keep the execution state and keep tracking the real result."""
        with self.db:
            row=self.get(task_id)
            if not row or row['state'] in TERMINAL:return False
            self.db.execute("UPDATE tasks SET cancel_state='cannot_cancel' WHERE task_id=?",(task_id,))
            self.audit(task_id,'cancel_cannot')
        return True

    def sent(self,task_id,now=None):
        now=time.time() if now is None else now
        with self.db:
            row=self.get(task_id)
            if not row or row['state'] not in ('accepted','queued','dispatched') or row['deadline']<=now:
                return False
            attempts=row['attempts']+1
            if attempts>MAX_ATTEMPTS:
                self.db.execute("UPDATE tasks SET state='failed',result=? WHERE task_id=?",
                                (json.dumps({'code':'DELIVERY_FAILED','data':None}),task_id))
                self.audit(task_id,'dead_letter')
                return False
            changed=self.db.execute("UPDATE tasks SET state='dispatched',attempts=?,next_send=? WHERE task_id=? AND state IN ('accepted','queued','dispatched') AND deadline>?",
                                    (attempts,now+backoff(attempts),task_id,now)).rowcount
            if changed:self.audit(task_id,'dispatched')
        return bool(changed)

    def finish_status(self,task_id,result):
        """SQLite writes are synchronous and durable, so a successful call is 'committed'.

        Mirrors KvTaskStore.finish_status so the Router can gate its result ack on one contract.
        """
        row=self.get(task_id)
        if not row:return 'missing'
        if row['state'] in TERMINAL:return 'terminal'
        self.finish(task_id,result)
        return 'committed'

    def finish(self,task_id,result):
        with self.db:
            row=self.get(task_id)
            if not row or row['state'] in TERMINAL:return
            if result.get('code')=='CANNOT_CANCEL':
                # The running task cannot be interrupted: do not call it failed, keep tracking it.
                self.db.execute("UPDATE tasks SET cancel_state='cannot_cancel' WHERE task_id=?",(task_id,))
                self.audit(task_id,'cancel_cannot')
                return
            if result.get('code')=='CANCELLED':
                state='cancelled';cancel='cancelled'
            else:
                state='succeeded' if result['code']=='OK' else 'failed'
                if time.time()>=row['deadline']:state='expired';result={'code':'EXPIRED'}
                cancel=row['cancel_state'] if row['cancel_state'] and row['cancel_state']!='none' else 'none'
            self.db.execute('UPDATE tasks SET state=?,result=?,cancel_state=? WHERE task_id=?',(state,json.dumps(result),cancel,task_id))
            self.audit(task_id,state)

    @staticmethod
    def public(row):
        return {k:row[k] for k in ['task_id','target','operation','state','created_at','deadline','attempts','deferrals','cancel_state']}|{'result':json.loads(row['result']) if row['result'] else None}

class WorkerStore(WorkerLedger):
    """Durable worker ledger; the implementation is the shared app/worker_kernel kernel.

    Kept as a named subclass so existing callers, drills and health checks keep importing it
    from here, while the standalone client ships and imports the very same kernel.
    """
