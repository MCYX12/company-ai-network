"""Shared replay ledger so multiple Router instances reject the same message_id.

The in-memory Router cache is per process. For multi-instance / failover deployments the
seen-message ledger must be shared. This uses a SQLite file with BEGIN IMMEDIATE so several
processes on one host serialize safely; swap in Redis or JetStream KV for multi-host.
"""
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import time


class ReplayStore:
    def __init__(self, path=None):
        from .common import authority_base, RUNTIME
        self.path = Path(path or authority_base(RUNTIME) / 'replay.sqlite3')
        self.lock = threading.Lock()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS seen(source TEXT NOT NULL, message_id TEXT NOT NULL, '
                       'expires REAL NOT NULL, PRIMARY KEY(source, message_id))')
            db.execute('CREATE INDEX IF NOT EXISTS seen_expires ON seen(expires)')
            db.commit()

    def check_and_record(self, source, message_id, expires, limit=100000, now=None):
        """True = proceed (recorded); False = replay; 'rate_limited' = cache full."""
        now = now or time.time()
        with self.lock, closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM seen WHERE expires < ?', (now,))
            if db.execute('SELECT 1 FROM seen WHERE source=? AND message_id=?', (source, message_id)).fetchone():
                db.rollback()
                return False
            if db.execute('SELECT count(*) FROM seen').fetchone()[0] >= limit:
                db.rollback()
                return 'rate_limited'
            db.execute('INSERT INTO seen(source, message_id, expires) VALUES(?,?,?)', (source, message_id, expires))
            db.commit()
            return True
