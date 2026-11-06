"""Server-local approval ledger. No network operation can approve a request."""
import json
import sqlite3
import time
import uuid
from .common import authority_base, RUNTIME, approval_expiry

class AccessStore:
    def __init__(self, path=None):
        self.path = path or authority_base(RUNTIME)/'conversation-access.sqlite3'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, caller TEXT, target TEXT, projects TEXT, state TEXT, created REAL, expires REAL, reason TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS access_events (id INTEGER PRIMARY KEY, approval TEXT, event TEXT, timestamp REAL, reason TEXT)')
    def db(self):
        return sqlite3.connect(self.path, timeout=5)
    def request(self, caller, target, projects):
        projects=json.dumps(sorted(set(projects)))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT id,state,expires FROM approvals WHERE caller=? AND target=? AND projects=? AND (state='pending' OR (state='approved' AND expires>?)) ORDER BY created DESC LIMIT 1",(caller,target,projects,time.time())).fetchone()
            if row:return dict(approval_id=row[0],state=row[1],expires_at=row[2])
            ident=str(uuid.uuid4());now=time.time()
            db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?)',(ident,caller,target,projects,'pending',now,0,''))
            db.execute('INSERT INTO access_events(approval,event,timestamp,reason) VALUES (?,?,?,?)',(ident,'requested',now,''))
            return dict(approval_id=ident,state='pending',expires_at=0)
    def decide(self, ident, decision, seconds, reason):
        if decision not in {'approved','revoked','denied'} or not reason.strip() or not (seconds==0 or 1<=seconds<=86400):raise ValueError('Invalid decision, reason or duration (0=no expiry, or 1..86400 seconds)')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT state FROM approvals WHERE id=?',(ident,)).fetchone()
            if not row or (decision in {'approved','denied'} and row[0]!='pending'):raise ValueError('Request absent or no longer pending')
            now=time.time()
            db.execute('UPDATE approvals SET state=?,expires=?,reason=? WHERE id=?',(decision,approval_expiry(seconds,now) if decision=='approved' else 0,reason,ident))
            db.execute('INSERT INTO access_events(approval,event,timestamp,reason) VALUES (?,?,?,?)',(ident,decision,now,reason))
    def permits(self, caller, target, projects):
        with self.db() as db:
            rows=db.execute("SELECT projects FROM approvals WHERE caller=? AND target=? AND state='approved' AND expires>?",(caller,target,time.time())).fetchall()
        return bool(projects) and any(set(projects).issubset(json.loads(row[0])) for row in rows)
    def listing(self, caller=None):
        with self.db() as db:
            db.row_factory=sqlite3.Row
            rows=db.execute('SELECT * FROM approvals'+(' WHERE caller=?' if caller else '')+' ORDER BY created DESC LIMIT 100',(caller,) if caller else ()).fetchall()
        return [dict(row,projects=json.loads(row['projects']),state='expired' if row['state']=='approved' and row['expires']<=time.time() else row['state']) for row in rows]

def approved(caller,target,projects):
    # Route through the unified authorization entry (assessment item 4).
    try:
        from .authz import decide
        return decide(caller,target,'conversation',projects=projects,runtime=authority_base(RUNTIME))['allowed']
    except (OSError,sqlite3.Error,ValueError,TypeError):return False
