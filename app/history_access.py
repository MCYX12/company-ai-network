"""Router-side history-access ledger.

The administrator can restrict or explicitly approve the history scope a device owner
already enabled locally. A grant carries a project scope: '*' means every project the
device owner allows, otherwise only history entries whose name/path match the scope.
Grants never replace device-owner consent; they only add an administrative veto/limit.
"""
import sqlite3
import time
import uuid
from .common import authority_base, RUNTIME

DECISIONS = {'approved', 'denied', 'revoked', 'pending'}


def _relevant(grants, caller, target):
    if not isinstance(grants, list):
        return None
    return [g for g in grants if isinstance(g, dict) and g.get('caller') == caller and g.get('target') == target]


def decision(caller, target, grants, now=None, scope=None):
    """Tri-state administrative layer over device-owner consent.

    UNMANAGED: nothing the administrator has decided is still in force, so device consent
        alone decides. That covers no record at all *and* records that merely lapsed — a
        pending request, a withdrawn (revoked) approval, or an approval whose window closed.
        Counting those as DENIED meant one expired grant silently disabled a device's own
        local-history path until an administrator acted again.
    DENIED: an explicit administrative denial still stands (revoke that record to hand the
        decision back to the device owner), or an *active* approval exists but does not cover
        the requested scope — the administrator deliberately limited the grant.
    APPROVED: an active approval matches (and covers scope when given).
    """
    now = time.time() if now is None else now
    relevant = _relevant(grants, caller, target)
    if relevant is None:
        return 'DENIED'  # malformed grant list: fail closed
    if not relevant:
        return 'UNMANAGED'
    for grant in relevant:
        if grant.get('state') != 'approved' or grant.get('expires', 0) <= now:
            continue
        grant_scope = grant.get('scope') or '*'
        if scope is None or grant_scope == '*' or grant_scope == scope:
            return 'APPROVED'
    if any(g.get('state') == 'approved' and g.get('expires', 0) > now for g in relevant):
        return 'DENIED'  # active approval, but it does not cover this scope
    if any(g.get('state') == 'denied' for g in relevant):
        return 'DENIED'  # explicit veto, kept until an administrator lifts it
    # pending / revoked / expired only: the administrator's window lapsed; device consent applies.
    return 'UNMANAGED'


def allowed_scopes(grants, caller, target, now=None):
    now = time.time() if now is None else now
    scopes = set()
    for grant in _relevant(grants, caller, target) or []:
        if grant.get('state') == 'approved' and grant.get('expires', 0) > now:
            scopes.add(grant.get('scope') or '*')
    return scopes


class HistoryAccess:
    def __init__(self, path=None):
        self.path = path or authority_base(RUNTIME) / 'history-access.sqlite3'
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,caller TEXT,target TEXT,state TEXT,created REAL,expires REAL,actor TEXT,reason TEXT,scope TEXT DEFAULT '*')")
            db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,request_id TEXT,event TEXT,time REAL,actor TEXT,reason TEXT)')
            columns = {row[1] for row in db.execute('PRAGMA table_info(requests)')}
            if 'scope' not in columns:
                db.execute("ALTER TABLE requests ADD COLUMN scope TEXT DEFAULT '*'")

    def db(self):
        return sqlite3.connect(self.path, timeout=5)

    def request(self, caller, target, reason='authenticated peer requested history scope', scope='*'):
        if not isinstance(scope, str) or not 1 <= len(scope) <= 128:
            raise ValueError('Invalid scope')
        with self.db() as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            old = db.execute("SELECT * FROM requests WHERE caller=? AND target=? AND COALESCE(scope,'*')=? ORDER BY created DESC LIMIT 1",
                             (caller, target, scope)).fetchone()
            if old and old['state'] == 'pending':
                return dict(old)
            if db.execute("SELECT COUNT(*) FROM requests WHERE caller=? AND target=? AND state='pending'", (caller, target)).fetchone()[0] >= 30:
                raise ValueError('Pending request limit')
            ident = str(uuid.uuid4()); now = time.time()
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)', (ident, caller, target, 'pending', now, 0, '', reason, scope))
            db.execute('INSERT INTO events(request_id,event,time,actor,reason) VALUES(?,?,?,?,?)', (ident, 'requested', now, target, reason))
            return dict(db.execute('SELECT * FROM requests WHERE id=?', (ident,)).fetchone())

    def listing(self, caller=None, target=None):
        with self.db() as db:
            db.row_factory = sqlite3.Row
            sql = 'SELECT * FROM requests' + (' WHERE caller=? AND target=?' if caller is not None else '') + ' ORDER BY created DESC LIMIT 100'
            rows = db.execute(sql, (caller, target) if caller is not None else ()).fetchall()
        return [dict(r, state='expired' if r['state'] == 'approved' and r['expires'] <= time.time() else r['state']) for r in rows]

    def decide(self, ident, decision, seconds, reason, actor):
        if decision not in DECISIONS or type(seconds) != int or not 1 <= seconds <= 86400 or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError('Invalid decision')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT state,expires FROM requests WHERE id=?', (ident,)).fetchone()
            if not old:
                raise ValueError('Unknown request')
            now = time.time()
            db.execute('UPDATE requests SET state=?,expires=?,actor=?,reason=? WHERE id=?',
                       (decision, now + seconds if decision == 'approved' else 0, actor, reason.strip(), ident))
            db.execute('INSERT INTO events(request_id,event,time,actor,reason) VALUES(?,?,?,?,?)', (ident, decision, now, actor, reason.strip()))

    def grants(self, caller, target, deadline):
        rows = self.listing(caller, target)
        return [{k: r[k] for k in ['id', 'caller', 'target', 'state']} | {'scope': r.get('scope') or '*',
                'expires': min(r['expires'] or deadline, deadline)}
                for r in rows if r['state'] in {'pending', 'approved', 'denied', 'revoked', 'expired'}]


def handle(actor, body):
    store = HistoryAccess()
    if not body:
        from .registry import load_registry
        registry = load_registry(); rows = store.listing()
        for r in rows:
            r['device'] = registry.get(r['target'], {}).get('device_id', '未登记')
            r['employee'] = registry.get(r['caller'], {}).get('employee_id', r['caller'])
        return {'requests': rows}
    create = {'caller', 'target', 'decision', 'seconds', 'reason'}
    if set(body) in (create, create | {'scope'}):
        row = store.request(body['caller'], body['target'], 'administrator opened a history scope', body.get('scope', '*'))
        store.decide(row['id'], body['decision'], body['seconds'], body['reason'], actor)
        return {'updated': row['id']}
    if set(body) != {'id', 'decision', 'seconds', 'reason'}:
        raise ValueError('Invalid fields')
    store.decide(body['id'], body['decision'], body['seconds'], body['reason'], actor)
    return {'updated': body['id']}
