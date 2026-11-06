"""PoC multi-instance lease with fencing tokens (HA seed).

A single SQLite file is the arbiter; BEGIN IMMEDIATE serializes contenders, so two
Router processes sharing the file cannot both hold the dispatch lease. Every acquire
or renew bumps a monotonic epoch that acts as a fencing token: a stale holder cannot
renew and must stop acting. Single-host seed, not distributed consensus.
"""
import inspect
import sqlite3
import time
import uuid
from .common import RUNTIME


async def maybe_await(value):
    """Await the value only if it is awaitable: sync and async leases stay interchangeable."""
    return await value if inspect.isawaitable(value) else value


class LeaseStore:
    def __init__(self, path=None):
        self.path = path or RUNTIME / 'leases.sqlite3'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS leases(name TEXT PRIMARY KEY,holder TEXT,token TEXT,epoch INTEGER NOT NULL DEFAULT 0,expires REAL)')
            # Monotonic epoch floor: releasing/reaping a lease must never let the epoch go backwards.
            db.execute('CREATE TABLE IF NOT EXISTS lease_epochs(name TEXT PRIMARY KEY,epoch INTEGER NOT NULL)')

    def db(self):
        return sqlite3.connect(self.path, timeout=5)

    @staticmethod
    def _now(now):
        return time.time() if now is None else now

    def acquire(self, name, holder, ttl=15, now=None):
        now = self._now(now)
        with self.db() as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM leases WHERE name=?', (name,)).fetchone()
            if row and row['expires'] > now:
                return None
            floor = db.execute('SELECT epoch FROM lease_epochs WHERE name=?', (name,)).fetchone()
            epoch = max(row['epoch'] if row else 0, floor[0] if floor else 0) + 1
            db.execute('INSERT INTO lease_epochs(name,epoch) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET epoch=excluded.epoch', (name, epoch))
            token = str(uuid.uuid4())
            db.execute('INSERT INTO leases VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET holder=excluded.holder,token=excluded.token,epoch=excluded.epoch,expires=excluded.expires',
                       (name, holder, token, epoch, now + ttl))
            return {'name': name, 'holder': holder, 'token': token, 'epoch': epoch, 'expires': now + ttl}

    def renew(self, name, holder, token, ttl=15, now=None):
        now = self._now(now)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT token,expires,epoch,holder FROM leases WHERE name=?', (name,)).fetchone()
            if not row or row[0] != token or row[3] != holder or row[1] <= now:
                return None
            epoch = row[2] + 1
            db.execute('UPDATE leases SET epoch=?,expires=? WHERE name=?', (epoch, now + ttl, name))
            db.execute('INSERT INTO lease_epochs(name,epoch) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET epoch=excluded.epoch', (name, epoch))
            return {'name': name, 'holder': holder, 'token': token, 'epoch': epoch, 'expires': now + ttl}

    def current(self, name):
        with self.db() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM leases WHERE name=?', (name,)).fetchone()
        return dict(row) if row else None

    def stale(self, name, epoch):
        """True when a newer lease epoch exists: the sender is a stale holder."""
        if epoch is None:
            return False
        row = self.current(name)
        return bool(row and row['epoch'] > epoch)

    def release(self, name, token):
        with self.db() as db:
            return db.execute('DELETE FROM leases WHERE name=? AND token=?', (name, token)).rowcount

    def holder(self, name, now=None):
        now = self._now(now)
        with self.db() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM leases WHERE name=?', (name,)).fetchone()
        return dict(row) if row and row['expires'] > now else None

    def reap(self, name=None, now=None):
        now = self._now(now)
        with self.db() as db:
            if name:
                return db.execute('DELETE FROM leases WHERE name=? AND expires<=?', (name, now)).rowcount
            return db.execute('DELETE FROM leases WHERE expires<=?', (now,)).rowcount
