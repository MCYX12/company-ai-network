"""KvTaskStore: atomic accept repair and explicit result-commit status.

The assessment found two HA-ledger faults: submit() created the idempotency index before the
task record, so a crash between the two left an index pointing at nothing and a same-key retry
could never succeed; and _mutate() swallowed write failures and returned None exactly like a
successful no-op, so the Router acked a result the authoritative ledger never stored and the
Worker stopped resending it. These cases pin the repair path and the status vocabulary.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.common import encode
from app.kv_task_store import KvTaskStore

from nats.js.errors import KeyNotFoundError, KeyWrongLastSequenceError


class FakeEntry:
    def __init__(self, value, revision):
        self.value = value
        self.revision = revision


class FakeKv:
    """Minimal in-memory stand-in for the nats KeyValue API the store uses."""

    def __init__(self):
        self.store = {}
        self.revision = 0
        self.fail_update = False
        self.stale_once = None

    async def get(self, key):
        if key not in self.store:
            raise KeyNotFoundError(key)
        value, revision = self.store[key]
        if self.stale_once == key:
            self.stale_once = None
            revision = max(0, revision - 1)      # simulate reading a stale revision
        return FakeEntry(value, revision)

    async def create(self, key, value):
        if key in self.store:
            raise KeyWrongLastSequenceError('exists')
        self.revision += 1
        self.store[key] = (bytes(value), self.revision)

    async def update(self, key, value, last=None):
        if self.fail_update:
            raise ConnectionError('kv down')
        if key not in self.store or self.store[key][1] != last:
            raise KeyWrongLastSequenceError('revision')
        self.revision += 1
        self.store[key] = (bytes(value), self.revision)

    async def keys(self):
        return list(self.store)

    async def delete(self, key):
        self.store.pop(key, None)


class FakeJs:
    def __init__(self, kv):
        self.kv = kv

    async def create_key_value(self, config=None):
        return self.kv

    async def key_value(self, bucket):
        return self.kv


class KvLedgerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.kv = FakeKv()
        self.store = KvTaskStore(FakeJs(self.kv))

    async def submit(self, key='k1', question='hi'):
        return await self.store.submit('ag-a', 'mac-1', key, 'ask_peer', {'question': question}, 60)

    async def test_same_key_is_idempotent_and_conflict_is_refused(self):
        first = await self.submit()
        again = await self.submit()
        self.assertEqual(first['task_id'], again['task_id'])
        with self.assertRaises(ValueError):
            await self.submit(question='different')

    async def test_orphan_idempotency_index_is_repaired_on_retry(self):
        row = await self.submit()
        del self.kv.store['t.' + row['task_id']]          # crash between the two creates
        repaired = await self.submit()
        self.assertEqual(repaired['task_id'], row['task_id'])
        self.assertIsNotNone(await self.store.get(row['task_id']))
        self.assertEqual(repaired['fingerprint'], row['fingerprint'])

    async def test_finish_status_distinguishes_durable_terminal_and_retry(self):
        row = await self.submit()
        self.assertEqual(await self.store.finish_status(row['task_id'], {'code': 'OK', 'data': {'text': 'x'}}),
                         'committed')
        self.assertEqual(await self.store.finish_status(row['task_id'], {'code': 'OK', 'data': None}),
                         'terminal')
        other = await self.submit(key='k2')
        self.kv.fail_update = True
        self.assertEqual(await self.store.finish_status(other['task_id'], {'code': 'OK', 'data': None}),
                         'retry')
        self.assertIsNone((await self.store.get(other['task_id']))['result'])
        self.kv.fail_update = False
        self.assertEqual(await self.store.finish_status(other['task_id'], {'code': 'OK', 'data': None}),
                         'committed')
        self.assertIsNotNone((await self.store.get(other['task_id']))['result'])

    async def test_finish_status_missing_task(self):
        self.assertEqual(await self.store.finish_status('no-such-task', {'code': 'OK'}), 'missing')

    async def test_cas_conflict_is_retried_until_committed(self):
        row = await self.submit()
        self.kv.stale_once = 't.' + row['task_id']
        self.assertEqual(await self.store.finish_status(row['task_id'], {'code': 'OK', 'data': {'text': 'x'}}),
                         'committed')
        stored = await self.store.get(row['task_id'])
        self.assertEqual(stored['state'], 'succeeded')


class FakeNc:
    def __init__(self):
        self.published = []

    async def publish(self, subject, data):
        self.published.append((subject, json.loads(data)))


class RouterResultAckTests(unittest.IsolatedAsyncioTestCase):
    """The Router must not acknowledge a result the authoritative ledger did not store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patcher = patch('app.router.RUNTIME', Path(self.tmp.name))
        self.patcher.start()
        from app.router import Router
        self.nc = FakeNc()
        self.router = Router(self.nc)

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    async def test_result_is_acked_only_after_a_durable_write(self):
        from app import router as router_module

        class Tasks:
            def __init__(self):
                self.status = 'retry'
                self.calls = 0

            async def get(self, task_id):
                return {'task_id': task_id, 'owner': 'ag-a', 'target': 'mac-1', 'operation': 'ask_peer',
                        'state': 'running', 'created_at': time.time(), 'deadline': time.time() + 60,
                        'payload': json.dumps({'question': 'hi'})}

            async def finish(self, task_id, result):
                self.calls += 1

            async def finish_status(self, task_id, result):
                self.calls += 1
                return self.status

        tasks = Tasks()
        self.router.tasks = tasks
        msg = SimpleNamespace(subject='corp.dev.v1.agent.mac-1.result',
                              data=encode({'task_id': 't1', 'code': 'OK', 'data': {'text': 'hi'}}))
        with patch.object(router_module, 'allowed', return_value=True):
            await self.router.task_result(msg)
            acks = [b for s, b in self.nc.published if s.endswith('.response')]
            self.assertEqual(acks, [])                       # retry: no ack, worker keeps resending
            tasks.status = 'committed'
            await self.router.task_result(msg)
        acks = [b for s, b in self.nc.published if s.endswith('.response') and b.get('type') == 'task_result_ack']
        self.assertEqual(len(acks), 1)


if __name__ == '__main__':
    unittest.main()
