"""Shared authoritative task ledger on JetStream KV (cross-host HA, opt-in).

Mirrors the SQLite TaskStore state machine but persists each task as one KV entry,
so two Routers on different hosts share a single authoritative ledger. Writes are
compare-and-set on the entry revision, and the terminal guard still makes a
duplicate result safe. The SQLite TaskStore stays the default; this backend is
selected only when POC_HA=1.
"""
import json
import time
import uuid

from .task_store import (ACTIVE, CONCURRENCY, MAX_ATTEMPTS, TERMINAL, backoff, digest)

TASK_PREFIX = 't.'
IDEM_PREFIX = 'i.'
RETRY_PREFIX = 'r.'
RETRY_REVERSE_PREFIX = 'rr.'
# A purged terminal task leaves a compact tombstone at its idempotency key, so a later
# same-key submit is deduplicated instead of resurrected as a fresh 'accepted' task.
IDEM_TOMBSTONE_PREFIX = 'x.'
SEQ_KEY = 'c.seq'
MAX_ACTIVE = 500


def _now(now):
    return time.time() if now is None else now


def _is_cas_conflict(error):
    """True for a KV compare-and-set conflict (retry) as opposed to a connection failure."""
    return type(error).__name__ in ('KeyWrongLastSequenceError', 'WrongLastSequenceError')


def row_bytes(row):
    """Serialized size of one task row, so quota checks measure real data (not a placeholder)."""
    try:
        return len(json.dumps(row, ensure_ascii=False).encode())
    except (TypeError, ValueError):
        return len(str(row).encode())

class KvTaskStore:
    def __init__(self, js, bucket='poc_tasks', replicas=1):
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
                self._kv = await self.js.key_value(self.bucket)
        return self._kv

    async def _read(self, key):
        from nats.js.errors import KeyNotFoundError, NotFoundError
        kv = await self._bucket()
        try:
            entry = await kv.get(key)
        except (KeyNotFoundError, NotFoundError):
            return None
        if entry is None:
            return None
        try:
            return json.loads(entry.value)
        except ValueError:
            return None

    async def _scan(self):
        from nats.js.errors import NoKeysError
        kv = await self._bucket()
        try:
            keys = await kv.keys()
        except NoKeysError:
            return []
        rows = []
        for key in keys:
            # The bucket also holds idempotency index and sequence keys.
            if not key.startswith(TASK_PREFIX):
                continue
            entry = await kv.get(key)
            if entry is not None:
                try:
                    rows.append(self._readable(json.loads(entry.value)))
                except ValueError:
                    pass
        return rows

    async def _mutate_status(self, key, fn, attempts=12):
        """Apply fn under compare-and-set, returning (status, result).

        status is 'committed' (persisted), 'noop' (fn changed nothing), 'missing' (no entry) or
        'retry' (temporary failure / CAS contention exhausted). A caller that must not acknowledge
        a result until it is durable needs this distinction: swallowing a write failure and
        reporting success is exactly how a Worker's outbox gets acked and stops resending a lost
        result (assessment 2026-10-05).
        """
        from nats.js.errors import KeyNotFoundError, NotFoundError
        try:
            kv = await self._bucket()
        except Exception:
            return 'retry', None
        for _ in range(attempts):
            try:
                entry = await kv.get(key)
            except (KeyNotFoundError, NotFoundError):
                return 'missing', None
            except Exception:
                return 'retry', None
            if entry is None:
                return 'missing', None
            try:
                row = json.loads(entry.value)
            except ValueError:
                return 'retry', None
            updated, result = fn(row)
            if updated is None:
                return 'noop', result
            try:
                await kv.update(key, json.dumps(updated, ensure_ascii=False).encode(), last=entry.revision)
            except Exception as error:
                if _is_cas_conflict(error):
                    continue
                return 'retry', None
            return 'committed', result
        return 'retry', None

    async def _mutate(self, key, fn, attempts=12):
        # Kept for callers that only want the mutation result; status is available via _mutate_status.
        _status, result = await self._mutate_status(key, fn, attempts)
        return result

    @staticmethod
    def _audit(row, event):
        audit = row.setdefault('audit', [])
        audit.append({'event': event, 'timestamp': time.time()})
        row['audit'] = audit[-100:]

    async def _next_seq(self):
        from nats.js.errors import KeyNotFoundError, NotFoundError
        kv = await self._bucket()
        for _ in range(20):
            try:
                entry = await kv.get(SEQ_KEY)
            except (KeyNotFoundError, NotFoundError):
                entry = None
            current = int(json.loads(entry.value)) if entry else 0
            try:
                if entry is None:
                    await kv.create(SEQ_KEY, str(current + 1).encode())
                else:
                    await kv.update(SEQ_KEY, str(current + 1).encode(), last=entry.revision)
            except Exception:
                continue
            return current + 1
        raise ValueError('SEQUENCE_UNAVAILABLE')

    @staticmethod
    def public(row):
        from .task_store import TaskStore
        return TaskStore.public(KvTaskStore._readable(row))

    @staticmethod
    def _readable(row):
        if row and row.get('compacted'):
            # Old tombstones lack scope metadata. Keep them readable, but use an
            # unrecognized operation so the Router denies rather than inventing rights.
            row = {'operation': 'purged', 'payload': '{}', 'seq': 0, 'attempts': 0,
                   'deferrals': 0, 'cancel_state': 'none', 'audit': [], **row}
        return row

    @staticmethod
    def _row(task_id, owner, target, key, fingerprint, operation, payload, ttl, seq):
        now = time.time()
        row = {'task_id': task_id, 'owner': owner, 'target': target, 'idempotency_key': key,
               'fingerprint': fingerprint, 'operation': operation, 'payload': json.dumps(payload),
               'state': 'accepted', 'created_at': now, 'deadline': now + ttl, 'next_send': 0,
               'attempts': 0, 'deferrals': 0, 'result': None, 'cancel_state': 'none',
               'seq': seq, 'audit': []}
        KvTaskStore._audit(row, 'accepted')
        return row

    async def _repair(self, task_id, owner, target, key, fingerprint, operation, payload, ttl, kv,
                      retry_of=None):
        """Create the task record an idempotency index already points at, or adopt the winner."""
        row = self._row(task_id, owner, target, key, fingerprint, operation, payload, ttl,
                        await self._next_seq())
        marker = retry_of or (key[len('retry-'):] if key.startswith('retry-') else None)
        if marker:
            # A repaired replacement must keep its retry metadata, or it would enter the
            # dispatchable list unlinked (assessment 2026-10-08).
            row['retry_of'] = marker
            row['retry_pending'] = True
            row['next_send'] = float('inf')
        try:
            await kv.create(TASK_PREFIX + task_id, json.dumps(row, ensure_ascii=False).encode())
        except Exception:
            winner = await self.get(task_id)
            if winner:
                if winner['fingerprint'] != fingerprint:
                    raise ValueError('IDEMPOTENCY_CONFLICT')
                return winner
            raise
        return row

    async def submit(self, owner, target, key, operation, payload, ttl, retry_of=None):
        fingerprint = digest({'target': target, 'operation': operation, 'payload': payload, 'ttl': ttl})
        kv = await self._bucket()
        idem_key = IDEM_PREFIX + owner + '.' + key
        existing = await self._read(idem_key)
        if existing:
            row = await self.get(existing)
            if row:
                if row['fingerprint'] != fingerprint:
                    raise ValueError('IDEMPOTENCY_CONFLICT')
                return row
            # The index exists but its task record does not: a previous accept died between the two
            # creates. Adopt the indexed task_id and repair it, so a same-key retry recovers instead
            # of returning an entry that can never be accepted (assessment 2026-10-05).
            return await self._repair(existing, owner, target, key, fingerprint, operation, payload, ttl, kv,
                                      retry_of=retry_of)
        rows = await self._scan()
        if sum(1 for r in rows if r['state'] not in TERMINAL) >= MAX_ACTIVE:
            raise ValueError('RATE_LIMITED')
        task_id = str(uuid.uuid4())
        row = self._row(task_id, owner, target, key, fingerprint, operation, payload, ttl,
                        await self._next_seq())
        if retry_of:
            # Born not-due and marked as a retry, so a linkage-write failure cannot leave an
            # unlinked child in the dispatchable list (assessment 2026-10-08).
            row['retry_of'] = retry_of
            row['retry_pending'] = True
            row['next_send'] = float('inf')
        try:
            await kv.create(idem_key, json.dumps(task_id).encode())
        except Exception:
            winner = await self._read(idem_key)
            if winner:
                row2 = await self.get(winner)
                if row2 and row2['fingerprint'] != fingerprint:
                    raise ValueError('IDEMPOTENCY_CONFLICT')
                if row2:
                    return row2
                return await self._repair(winner, owner, target, key, fingerprint, operation, payload, ttl, kv,
                                          retry_of=retry_of)
            raise
        try:
            await kv.create(TASK_PREFIX + task_id, json.dumps(row, ensure_ascii=False).encode())
        except Exception:
            # A concurrent writer may have created it; otherwise the index is now orphaned and a
            # retry repairs it through the existing-index-but-missing-record branch above.
            winner = await self.get(task_id)
            if winner:
                return winner
            raise
        return row

    async def get(self, task_id):
        return self._readable(await self._read(TASK_PREFIX + task_id))

    async def history(self, task_id):
        row = await self.get(task_id)
        return row['audit'] if row else []

    async def list_owned(self, owner, permitted, state=None, limit=20, before=None):
        rows = [r for r in await self._scan() if r['owner'] == owner]
        if state:
            rows = [r for r in rows if r['state'] == state]
        if before is not None:
            rows = [r for r in rows if r['seq'] < before]
        rows.sort(key=lambda r: r['seq'], reverse=True)
        allowed = []
        for row in rows:
            if permitted(row):
                allowed.append(row)
                if len(allowed) > limit:
                    break
        has_more = len(allowed) > limit
        page = allowed[:limit]
        tasks = []
        for row in page:
            public = self.public(row)
            result = public.pop('result')
            public['result_code'] = result.get('code') if isinstance(result, dict) else None
            audit = row.get('audit') or []
            public['updated_at'] = audit[-1]['timestamp'] if audit else row['created_at']
            tasks.append(public)
        return {'tasks': tasks, 'next_cursor': page[-1]['seq'] if has_more else None}

    async def _expire(self, task_id):
        def fn(row):
            if row['state'] in TERMINAL:
                return None, None
            row['state'] = 'expired'
            row['result'] = json.dumps({'code': 'EXPIRED'})
            self._audit(row, 'expired')
            return row, None
        return await self._mutate(TASK_PREFIX + task_id, fn)

    async def due(self, now=None):
        now = _now(now)
        candidates = []
        for row in await self._scan():
            if row['state'] in TERMINAL:
                continue
            if row['deadline'] <= now:
                await self._expire(row['task_id'])
                continue
            if row.get('retry_pending'):
                # A Router may have acknowledged the interrupted root before the
                # replacement release failed. Repair on scheduler ticks, rather
                # than relying on that already-acked result being delivered again.
                root = await self.get(row.get('retry_of')) if row.get('retry_of') else None
                if not root or root['state'] != 'failed':
                    continue
                result = json.loads(root.get('result') or '{}')
                if result.get('code') != 'WORKER_INTERRUPTED' or any(
                        row.get(key) != root.get(key) for key in ('owner', 'target', 'operation')):
                    continue
                try:
                    await self.record_retry(root['task_id'], row['task_id'], 2, 'WORKER_INTERRUPTED')
                    row = await self._clear_retry_pending(row['task_id'])
                except Exception:
                    continue
            if row['next_send'] <= now:
                candidates.append(row)
        candidates.sort(key=lambda r: r['created_at'])
        return candidates[:20]

    async def inflight_counts(self):
        counts = {}
        for row in await self._scan():
            if row['state'] in ('dispatched', 'running'):
                counts[row['target']] = counts.get(row['target'], 0) + 1
        return counts

    async def can_send(self, task_id, now=None):
        now = _now(now)
        row = await self.get(task_id)
        return bool(row and row['state'] in ('accepted', 'queued', 'dispatched')
                    and row['deadline'] > now and row['attempts'] + 1 <= MAX_ATTEMPTS)

    async def sent(self, task_id, now=None):
        now = _now(now)
        def fn(row):
            if row['state'] not in ('accepted', 'queued', 'dispatched') or row['deadline'] <= now:
                return None, False
            attempts = row['attempts'] + 1
            if attempts > MAX_ATTEMPTS:
                row['state'] = 'failed'
                row['result'] = json.dumps({'code': 'DELIVERY_FAILED', 'data': None})
                self._audit(row, 'dead_letter')
                return row, False
            row['state'] = 'dispatched'
            row['attempts'] = attempts
            row['next_send'] = now + backoff(attempts)
            self._audit(row, 'dispatched')
            return row, True
        return await self._mutate(TASK_PREFIX + task_id, fn)

    async def record_send_failure(self, task_id, now=None):
        now = _now(now)
        def fn(row):
            if row['state'] not in ('accepted', 'queued', 'dispatched'):
                return None, False
            attempts = row['attempts'] + 1
            if attempts > MAX_ATTEMPTS:
                row['state'] = 'failed'
                row['result'] = json.dumps({'code': 'DELIVERY_FAILED', 'data': None})
                self._audit(row, 'dead_letter')
                return row, False
            row['state'] = 'queued'
            row['attempts'] = attempts
            row['next_send'] = now + backoff(attempts)
            self._audit(row, 'send_failed')
            return row, True
        return await self._mutate(TASK_PREFIX + task_id, fn)

    async def defer(self, task_id, now):
        def fn(row):
            if row['state'] not in ACTIVE:
                return None, False
            row['deferrals'] = row['deferrals'] + 1
            row['state'] = 'queued'
            row['next_send'] = now + backoff(row['deferrals'])
            self._audit(row, 'deferred')
            return row, True
        return await self._mutate(TASK_PREFIX + task_id, fn)

    async def hold(self, task_id, now):
        def fn(row):
            if row['state'] not in ACTIVE:
                return None, False
            row['state'] = 'queued'
            row['next_send'] = now + 1
            return row, True
        return await self._mutate(TASK_PREFIX + task_id, fn)

    async def requeue_stale(self, task_id, now=None):
        now = _now(now)
        def fn(row):
            if row['state'] not in ('dispatched', 'running') or row['deadline'] <= now:
                return None, False
            row['state'] = 'queued'
            row['next_send'] = now
            self._audit(row, 'stale_dispatch')
            return row, True
        return await self._mutate(TASK_PREFIX + task_id, fn)

    async def mark_running(self, task_id):
        def fn(row):
            if row['state'] not in ACTIVE:
                return None, False
            row['state'] = 'running'
            row['next_send'] = row['deadline']
            self._audit(row, 'running')
            return row, True
        return await self._mutate(TASK_PREFIX + task_id, fn)

    async def finish(self, task_id, result):
        return await self.finish_status(task_id, result)

    async def finish_status(self, task_id, result):
        """Persist a terminal result and report whether the authoritative ledger really has it.

        'committed' (written), 'terminal' (already terminal / nothing to change), 'missing'
        (unknown task) or 'retry' (temporary failure). Only committed/terminal may be acked.
        """
        def fn(row):
            if row['state'] in TERMINAL:
                return None, None
            if result.get('code') == 'CANNOT_CANCEL':
                row['cancel_state'] = 'cannot_cancel'
                self._audit(row, 'cancel_cannot')
                return row, None
            effective = result
            if result.get('code') == 'CANCELLED':
                row['state'] = 'cancelled'
                row['cancel_state'] = 'cancelled'
            else:
                state = 'succeeded' if result['code'] == 'OK' else 'failed'
                if time.time() >= row['deadline']:
                    state = 'expired'
                    effective = {'code': 'EXPIRED'}
                row['state'] = state
                if not row.get('cancel_state') or row['cancel_state'] == 'none':
                    row['cancel_state'] = 'none'
            row['result'] = json.dumps(effective)
            self._audit(row, row['state'])
            return row, None
        status, _result = await self._mutate_status(TASK_PREFIX + task_id, fn)
        return {'committed': 'committed', 'noop': 'terminal', 'missing': 'missing'}.get(status, 'retry')

    async def cancel(self, task_id, owner):
        def fn(row):
            if row['owner'] != owner:
                return None, ('FORBIDDEN', None)
            if row['state'] == 'cancelled':
                return None, ('CANCELLED', row)
            if row['state'] in TERMINAL:
                return None, ('ALREADY_FINISHED', row)
            if row['state'] == 'cancel_requested':
                return None, ('CANCEL_REQUESTED', row)
            if row['state'] in ('accepted', 'queued') and row['attempts'] == 0:
                state = 'expired' if row['deadline'] <= time.time() else 'cancelled'
                row['state'] = state
                row['result'] = json.dumps({'code': state.upper(), 'data': None})
                row['cancel_state'] = 'expired' if state == 'expired' else 'cancelled'
                self._audit(row, state)
                return row, (state.upper(), row)
            row['state'] = 'cancel_requested'
            row['cancel_state'] = 'requested'
            row['result'] = json.dumps({'code': 'CANCEL_REQUESTED', 'data': None})
            row['next_send'] = row['deadline']
            self._audit(row, 'cancel_requested')
            return row, ('CANCEL_REQUESTED', row)
        result = await self._mutate(TASK_PREFIX + task_id, fn)
        return result if result is not None else ('FORBIDDEN', None)

    async def confirm_cancelled(self, task_id):
        def fn(row):
            if row['state'] in TERMINAL:
                return None, False
            row['state'] = 'cancelled'
            row['cancel_state'] = 'cancelled'
            row['result'] = json.dumps({'code': 'CANCELLED', 'data': None})
            self._audit(row, 'cancelled')
            return row, True
        return await self._mutate(TASK_PREFIX + task_id, fn)

    async def mark_cannot_cancel(self, task_id):
        def fn(row):
            if row['state'] in TERMINAL:
                return None, False
            row['cancel_state'] = 'cannot_cancel'
            self._audit(row, 'cancel_cannot')
            return row, True
        return await self._mutate(TASK_PREFIX + task_id, fn)

    async def plan(self, now, permitted, eligible, reserve=False):
        inflight = await self.inflight_counts()
        chosen = []
        for row in await self.due(now):
            task_id = row['task_id']
            if not permitted(row):
                await self.finish(task_id, {'code': 'FORBIDDEN', 'data': None})
                continue
            if not eligible(row['target'], row['operation']):
                await self.defer(task_id, now)
                continue
            if inflight.get(row['target'], 0) >= CONCURRENCY:
                await self.hold(task_id, now)
                continue
            if reserve:
                if not await self.can_send(task_id, now):
                    await self.record_send_failure(task_id, now)
                    continue
            elif not await self.sent(task_id, now):
                continue
            inflight[row['target']] = inflight.get(row['target'], 0) + 1
            current = await self.get(task_id)
            if current:
                chosen.append(current)
        return chosen

    async def dead_letters(self, limit=100):
        out = []
        for row in sorted(await self._scan(), key=lambda r: r['created_at']):
            result = json.loads(row['result']) if row['result'] else {}
            if row['state'] == 'failed' and result.get('code') == 'DELIVERY_FAILED':
                out.append(row)
                if len(out) >= limit:
                    break
        return out

    async def requeue(self, task_id, actor='admin'):
        def fn(row):
            result = json.loads(row['result']) if row['result'] else {}
            if row['state'] != 'failed' or result.get('code') != 'DELIVERY_FAILED':
                raise ValueError('NOT_DEAD_LETTER')
            if row['deadline'] <= time.time():
                raise ValueError('DEADLINE_PASSED')
            row['state'] = 'accepted'
            row['attempts'] = 0
            row['next_send'] = 0
            row['deferrals'] = 0
            row['result'] = None
            self._audit(row, 'requeued')
            return row, row
        return await self._mutate(TASK_PREFIX + task_id, fn)

    async def purge_dead_letters(self, before):
        kv = await self._bucket()
        removed = 0
        for row in await self.dead_letters(100000):
            if row['created_at'] < before:
                await self._compact(row['task_id'])
                removed += 1
        return removed

    async def _put(self, key, value):
        kv = await self._bucket()
        await kv.put(key, json.dumps(value, ensure_ascii=False).encode())

    async def record_retry(self, root_task_id, retry_task_id, attempt, reason):
        """Persist the root<->retry linkage (both directions) for HA recovery."""
        entry = {'root_task_id': root_task_id, 'retry_task_id': retry_task_id,
                 'attempt': int(attempt), 'reason': str(reason)[:200], 'created': time.time()}
        await self._put(RETRY_PREFIX + root_task_id, entry)
        await self._put(RETRY_REVERSE_PREFIX + retry_task_id, entry)
        return entry

    async def retry_for(self, task_id):
        return await self._read(RETRY_PREFIX + task_id)

    async def _scan_retries(self):
        from nats.js.errors import NoKeysError
        kv = await self._bucket()
        try:
            keys = await kv.keys()
        except NoKeysError:
            return []
        rows = []
        for key in keys:
            if not key.startswith(RETRY_PREFIX):
                continue
            entry = await kv.get(key)
            if entry is not None:
                try:
                    rows.append(json.loads(entry.value))
                except ValueError:
                    pass
        return rows

    async def retried_from(self, task_id):
        entry = await self._read(RETRY_REVERSE_PREFIX + task_id)
        if entry:
            return entry
        # Repair a half-written reverse link from the authoritative forward row.
        for row in await self._scan_retries():
            if row.get('retry_task_id') == task_id:
                try:
                    await self._put(RETRY_REVERSE_PREFIX + task_id, row)
                except Exception:
                    pass
                return row
        return None

    async def is_retry(self, task_id):
        if await self.retried_from(task_id):
            return True
        row = await self.get(task_id)
        return bool(row and row.get('retry_of'))

    async def submit_retry(self, root_task_id, owner, target, operation, payload, ttl,
                           reason='WORKER_INTERRUPTED'):
        """Create the one replacement task and its linkage (idempotent; never loops).

        A half-written linkage is repaired from the other direction, and a shrinking remaining
        TTL (which changes the idempotency fingerprint) adopts the existing replacement instead
        of raising IDEMPOTENCY_CONFLICT.
        """
        existing = await self.retry_for(root_task_id)
        if not existing:
            existing = next((row for row in await self._scan_retries()
                             if row.get('root_task_id') == root_task_id), None)
            if existing:
                await self.record_retry(root_task_id, existing.get('retry_task_id'),
                                        existing.get('attempt', 2), existing.get('reason', reason))
        if existing and existing.get('retry_task_id'):
            found = await self.get(existing['retry_task_id'])
            if found:
                if found.get('retry_pending') or found.get('next_send') == float('inf'):
                    # A previous call wrote the linkage but died before releasing the child:
                    # repair both directions and release it now.
                    await self.record_retry(root_task_id, found['task_id'],
                                            existing.get('attempt', 2), existing.get('reason', reason))
                    await self._clear_retry_pending(found['task_id'])
                    found = await self.get(found['task_id']) or found
                return found
        if not await self.get(root_task_id):
            raise ValueError('UNKNOWN_ROOT_TASK')
        try:
            new = await self.submit(owner, target, 'retry-' + root_task_id, operation, payload, ttl,
                                    retry_of=root_task_id)
        except ValueError as error:
            if str(error) != 'IDEMPOTENCY_CONFLICT':
                raise
            indexed = await self._read(IDEM_PREFIX + owner + '.retry-' + root_task_id)
            new = await self.get(indexed) if indexed else None
            if new is None:
                raise
        await self.record_retry(root_task_id, new['task_id'], 2, reason)
        await self._clear_retry_pending(new['task_id'])
        return await self.get(new['task_id']) or new

    async def _clear_retry_pending(self, task_id):
        """After the linkage is durable, release the child into the dispatchable set."""
        def fn(row):
            if not row.get('retry_pending') and row.get('next_send') != float('inf'):
                return None, row
            row['retry_pending'] = False
            row['next_send'] = 0
            return row, row
        status, result = await self._mutate_status(TASK_PREFIX + task_id, fn)
        if status not in ('committed', 'noop') or not result:
            raise ValueError('RETRY_RELEASE_UNAVAILABLE')
        return result

    async def _compact(self, task_id):
        """Drop content, retaining the contract for public reads and authorization."""
        def fn(row):
            if row.get('state') not in TERMINAL or row.get('compacted'):
                return None, row
            try:
                result = json.loads(row.get('result') or '{}')
            except ValueError:
                result = {}
            code = result.get('code') if isinstance(result, dict) else None
            payload = json.loads(row.get('payload') or '{}')
            scope = {key: payload[key] for key in ('project_id', 'projects') if key in payload}
            compacted = {'task_id': row['task_id'], 'owner': row.get('owner'), 'target': row.get('target'),
                    'idempotency_key': row.get('idempotency_key'), 'fingerprint': row.get('fingerprint'),
                    'state': row['state'], 'created_at': row.get('created_at'),
                    'deadline': row.get('deadline'), 'result': json.dumps({'code': code, 'purged': True}),
                    'operation': row.get('operation', 'purged'), 'payload': json.dumps(scope),
                    'seq': row.get('seq', 0), 'attempts': row.get('attempts', 0),
                    'deferrals': row.get('deferrals', 0), 'cancel_state': row.get('cancel_state', 'none'),
                    'audit': [{'event': 'content_purged', 'timestamp': time.time()}],
                    'compacted': True, 'purged_at': time.time()}
            return compacted, compacted
        return await self._mutate(TASK_PREFIX + task_id, fn)

    async def _bucket_bytes(self):
        """(total, task) bytes actually stored, so the quota is not just task-row bytes."""
        from nats.js.errors import NoKeysError
        kv = await self._bucket()
        keys_fn = getattr(kv, 'keys', None)
        if keys_fn is None:
            return 0, 0
        try:
            keys = await keys_fn()
        except NoKeysError:
            return 0, 0
        except Exception:
            return 0, 0
        total = task_bytes = 0
        for key in keys:
            try:
                entry = await kv.get(key)
            except Exception:
                continue
            if entry is None:
                continue
            size = len(entry.value or b'')
            total += size
            if key.startswith(TASK_PREFIX):
                task_bytes += size
        return total, task_bytes

    async def quota_status(self, limit_bytes=None):
        """Measured quota: serialized task bytes, limit and over_quota (no fixed placeholder)."""
        limit = limit_bytes or 67108864
        rows = await self._scan()
        size = sum(row_bytes(row) for row in rows)
        bucket_bytes, task_bytes = await self._bucket_bytes()
        pending = sum(1 for r in rows if r['state'] not in TERMINAL)
        return {'size_bytes': size, 'task_bytes': task_bytes, 'bucket_bytes': bucket_bytes,
                'limit_bytes': limit, 'over_quota': (bucket_bytes or size) > limit,
                'dead_letters': len(await self.dead_letters(100000)), 'pending': pending,
                'tasks': len(rows)}

    async def enforce_quota(self, limit_bytes=None, before=None):
        """Delete terminal tasks (oldest first) until under the limit, or older than the cutoff.

        Only terminal tasks are removed; pending/running work is never dropped.
        """
        limit = limit_bytes or 67108864
        status = await self.quota_status(limit)
        purged = 0
        if status['over_quota'] or before is not None:
            kv = await self._bucket()
            terminal = sorted((row for row in await self._scan() if row['state'] in TERMINAL),
                              key=lambda r: (r.get('updated_at') or r.get('created_at') or 0))
            removed = 0
            for row in terminal:
                stamp = row.get('updated_at') or row.get('created_at') or 0
                if before is not None and stamp >= before:
                    continue
                if before is None and (status['bucket_bytes'] or status['size_bytes']) - removed <= limit:
                    break
                # Compact in place: the terminal row and its idempotency index stay (so get()
                # and same-key dedup still work), while the large result is released.
                if not row.get('compacted'):
                    result = await self._compact(row['task_id'])
                    if result is None:
                        raise ValueError('COMPACTION_UNAVAILABLE')
                    removed += max(0, row_bytes(row) - row_bytes(result))
                    purged += 1
        return {'purged': purged, **await self.quota_status(limit)}
