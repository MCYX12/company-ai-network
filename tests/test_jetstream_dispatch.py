"""Durable dispatch: reserve -> PubAck -> mark dispatched, with failure backoff."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.task_store import MAX_ATTEMPTS, TaskStore


class ReserveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name) / 'tasks.sqlite3')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def submit(self):
        return self.store.submit('ag-a', 'peer-x', 'k1', 'ask_peer', {'question': 'q'}, 600)

    def test_reserve_returns_without_marking_dispatched(self):
        row = self.submit()
        now = time.time()
        chosen = self.store.plan(now, lambda r: True, lambda t, o: True, reserve=True)
        self.assertEqual(len(chosen), 1)
        current = self.store.get(row['task_id'])
        self.assertEqual(current['state'], 'accepted')
        self.assertEqual(current['attempts'], 0)

    def test_sent_after_ack_marks_dispatched(self):
        row = self.submit()
        now = time.time()
        self.store.plan(now, lambda r: True, lambda t, o: True, reserve=True)
        self.assertTrue(self.store.sent(row['task_id'], now))
        current = self.store.get(row['task_id'])
        self.assertEqual(current['state'], 'dispatched')
        self.assertEqual(current['attempts'], 1)

    def test_record_send_failure_backs_off_then_dead_letters(self):
        row = self.submit()
        now = time.time()
        for _ in range(MAX_ATTEMPTS):
            self.assertTrue(self.store.record_send_failure(row['task_id'], now))
        self.assertEqual(self.store.get(row['task_id'])['state'], 'queued')
        self.assertFalse(self.store.record_send_failure(row['task_id'], now))
        failed = self.store.get(row['task_id'])
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(json.loads(failed['result'])['code'], 'DELIVERY_FAILED')

    def test_can_send_stops_at_max_attempts(self):
        row = self.submit()
        now = time.time()
        for _ in range(MAX_ATTEMPTS):
            self.store.record_send_failure(row['task_id'], now)
        # attempts == MAX_ATTEMPTS, so no further send is allowed
        self.assertFalse(self.store.can_send(row['task_id'], now))


class RouterDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _dispatch(self, publish):
        from app.router import Router
        with patch('app.router.RUNTIME', self.runtime), \
             patch('app.directory_requests.RUNTIME', self.runtime), \
             patch('app.history_access.RUNTIME', self.runtime), \
             patch('app.router.allowed', return_value=True), \
             patch('app.jetstream_bus.publish', new=publish):
            router = Router(SimpleNamespace(publish=AsyncMock()))
            router.js = object()  # force the JetStream branch
            now = time.time()
            router.presence = {'peer-x': now}
            router.runtime_tasks = {'peer-x': ['ask_peer']}
            row = router.tasks.submit('ag-a', 'peer-x', 'k1', 'ask_peer', {'question': 'q'}, 600)
            asyncio.run(router.dispatch_tasks())
            state = router.tasks.get(row['task_id'])
            router.tasks.db.close()
            return state

    def test_router_marks_dispatched_only_after_puback(self):
        publish = AsyncMock(return_value={'stream': 'TASKS', 'seq': 1})
        state = self._dispatch(publish)
        publish.assert_awaited_once()
        self.assertEqual(state['state'], 'dispatched')
        self.assertEqual(state['attempts'], 1)

    def test_router_keeps_task_queued_when_puback_fails(self):
        publish = AsyncMock(side_effect=RuntimeError('no ack'))
        state = self._dispatch(publish)
        self.assertEqual(state['state'], 'queued')
        self.assertEqual(state['attempts'], 1)


class EnsureTests(unittest.TestCase):
    def test_ensure_passes_replica_count(self):
        from app import jetstream_bus
        calls = []

        class FakeJS:
            async def add_stream(self, name, subjects, config):
                calls.append({'name': name, 'subjects': subjects, 'replicas': config.num_replicas})
                return type('S', (), {'config': type('C', (), {'name': name})()})()

        asyncio.run(jetstream_bus.ensure(FakeJS(), 'TASKS', 'poc', replicas=3))
        self.assertEqual(calls[0]['name'], 'TASKS')
        self.assertEqual(calls[0]['subjects'], ['poc.task.>'])
        self.assertEqual(calls[0]['replicas'], 3)


if __name__ == '__main__':
    unittest.main()
