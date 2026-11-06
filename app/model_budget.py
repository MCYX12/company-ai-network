"""Per caller->target daily model-invocation budget (plan 11 / AI-01)."""
import os
import sqlite3
import time
from .common import RUNTIME, authority_base


def daily_limit():
    try:
        return max(1, int(os.environ.get('POC_MODEL_CALL_BUDGET', '50')))
    except ValueError:
        return 50


class ModelBudget:
    def __init__(self, path=None, limit=None):
        self.path = path or authority_base(RUNTIME) / 'model-budget.sqlite3'
        self.limit = limit or daily_limit()
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS usage(caller TEXT,target TEXT,day TEXT,count INTEGER,PRIMARY KEY(caller,target,day))')
            db.execute('CREATE TABLE IF NOT EXISTS denials(id INTEGER PRIMARY KEY,caller TEXT,target TEXT,day TEXT,time REAL,reason TEXT)')

    def db(self):
        return sqlite3.connect(self.path, timeout=5)

    @staticmethod
    def day(now=None):
        return time.strftime('%Y-%m-%d', time.gmtime(now if now is not None else time.time()))

    def used(self, caller, target, now=None):
        with self.db() as db:
            row = db.execute('SELECT count FROM usage WHERE caller=? AND target=? AND day=?', (caller, target, self.day(now))).fetchone()
        return row[0] if row else 0

    def remaining(self, caller, target, now=None):
        return max(0, self.limit - self.used(caller, target, now))

    def allow(self, caller, target, now=None):
        return self.used(caller, target, now) < self.limit

    def spend(self, caller, target, now=None):
        day = self.day(now)
        with self.db() as db:
            db.execute('INSERT INTO usage VALUES(?,?,?,1) ON CONFLICT(caller,target,day) DO UPDATE SET count=count+1', (caller, target, day))
            row = db.execute('SELECT count FROM usage WHERE caller=? AND target=? AND day=?', (caller, target, day)).fetchone()
        return row[0]

    def record_denial(self, caller, target, reason='DAILY_LIMIT', now=None):
        with self.db() as db:
            db.execute('INSERT INTO denials(caller,target,day,time,reason) VALUES(?,?,?,?,?)',
                       (caller, target, self.day(now), time.time() if now is None else now, reason))
