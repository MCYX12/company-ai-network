"""Distributed dispatch lease on JetStream KV (cross-host HA, opt-in).

A single replicated KV entry is the arbiter. Acquire/renew use compare-and-set on
the entry revision, and every acquire/renew bumps a monotonic epoch used as the
fencing token, so a stale holder cannot keep dispatching. Unlike the SQLite
LeaseStore this arbitrates across hosts, because the KV bucket is a replicated
JetStream stream.
"""
import asyncio
import json
import time
import uuid

LEASE_TTL = 15


def _now(now):
    return time.time() if now is None else now


class KvLease:
    def __init__(self, js, bucket='poc_leases', replicas=1):
        self.js = js
        self.bucket = bucket
        self.replicas = replicas
        self._kv = None

    async def _bucket(self):
        if self._kv is None:
            from nats.js.api import KeyValueConfig
            try:
                self._kv = await self.js.create_key_value(
                    config=KeyValueConfig(bucket=self.bucket, replicas=self.replicas))
            except Exception:
                # Bucket already exists (possibly with a different replica count).
                self._kv = await self.js.key_value(self.bucket)
        return self._kv

    async def _read(self, name):
        from nats.js.errors import KeyNotFoundError, NotFoundError
        kv = await self._bucket()
        try:
            entry = await kv.get(name)
        except (KeyNotFoundError, NotFoundError):
            return None, None
        if entry is None:
            return None, None
        try:
            return json.loads(entry.value), entry.revision
        except ValueError:
            return None, entry.revision

    @staticmethod
    def _encode(value):
        return json.dumps(value, ensure_ascii=False).encode()

    async def acquire(self, name, holder, ttl=LEASE_TTL, now=None):
        now = _now(now)
        for _ in range(12):
            current, revision = await self._read(name)
            if current and current.get('expires', 0) > now:
                return None
            epoch = int((current or {}).get('epoch', 0)) + 1
            value = {'name': name, 'holder': holder, 'token': str(uuid.uuid4()),
                     'epoch': epoch, 'expires': now + ttl}
            kv = await self._bucket()
            try:
                if revision is None:
                    await kv.create(name, self._encode(value))
                else:
                    await kv.update(name, self._encode(value), last=revision)
            except Exception:
                await asyncio.sleep(0)
                continue
            return value
        return None

    async def renew(self, name, holder, token, ttl=LEASE_TTL, now=None):
        now = _now(now)
        for _ in range(12):
            current, revision = await self._read(name)
            if (not current or current.get('token') != token or current.get('holder') != holder
                    or current.get('expires', 0) <= now):
                return None
            value = dict(current, epoch=int(current['epoch']) + 1, expires=now + ttl)
            kv = await self._bucket()
            try:
                await kv.update(name, self._encode(value), last=revision)
            except Exception:
                await asyncio.sleep(0)
                continue
            return value
        return None

    async def current(self, name):
        value, _ = await self._read(name)
        return value

    async def stale(self, name, epoch):
        if epoch is None:
            return False
        current = await self.current(name)
        return bool(current and current.get('epoch', 0) > epoch)

    async def holder(self, name, now=None):
        now = _now(now)
        current = await self.current(name)
        return current if current and current.get('expires', 0) > now else None

    async def release(self, name, token):
        current, _ = await self._read(name)
        if current and current.get('token') == token:
            kv = await self._bucket()
            try:
                await kv.delete(name)
                return 1
            except Exception:
                return 0
        return 0

    async def reap(self, name=None, now=None):
        return 0


class AsyncLease:
    """Uniform async view over the synchronous SQLite LeaseStore (behaviour unchanged)."""

    def __init__(self, store):
        self._store = store

    @property
    def store(self):
        return self._store

    async def acquire(self, *args, **kwargs):
        return self._store.acquire(*args, **kwargs)

    async def renew(self, *args, **kwargs):
        return self._store.renew(*args, **kwargs)

    async def stale(self, *args, **kwargs):
        return self._store.stale(*args, **kwargs)

    async def current(self, *args, **kwargs):
        return self._store.current(*args, **kwargs)

    async def holder(self, *args, **kwargs):
        return self._store.holder(*args, **kwargs)

    async def release(self, *args, **kwargs):
        return self._store.release(*args, **kwargs)
