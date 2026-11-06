from pathlib import Path
import asyncio
import tempfile
import unittest
from types import SimpleNamespace

from app.jetstream_bus import apply_once, consume, pending
from app.task_store import TaskStore

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


class JetStreamBusTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.store = TaskStore(Path(self.tmp.name) / 'tasks.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_once_is_idempotent(self):
        task = self.store.submit('ag-a', 'peer-x', 'k', 'ask_peer', {'question': 'x'}, 600)
        self.assertTrue(apply_once(self.store, task['task_id'], {'code': 'OK', 'data': {'n': 1}}))
        self.assertFalse(apply_once(self.store, task['task_id'], {'code': 'FORBIDDEN', 'data': None}))
        self.assertEqual(self.store.get(task['task_id'])['state'], 'succeeded')

    def test_pending_is_the_reconciliation_source(self):
        task = self.store.submit('ag-a', 'peer-x', 'k', 'ask_peer', {'question': 'x'}, 600)
        self.assertEqual([row['task_id'] for row in pending(self.store)], [task['task_id']])
        apply_once(self.store, task['task_id'], {'code': 'OK', 'data': {}})
        self.assertEqual(pending(self.store), [])


class FakeConsumerInfo:
    """The server-side view of an existing durable consumer (only what the check reads)."""

    def __init__(self, name, stream_name, **config):
        self.name = name
        self.stream_name = stream_name
        self.config = SimpleNamespace(**config)


class FakePullSubscription:
    def __init__(self, info=None, error=None):
        self.info = info
        self.error = error
        self.consumer_info_calls = 0

    async def consumer_info(self):
        self.consumer_info_calls += 1
        if self.error is not None:
            raise self.error
        return self.info

    async def fetch(self, count, timeout=None):
        raise AssertionError('consume(total=0) must not fetch')


class FakeDurableJS:
    """Minimal stub of JetStreamContext: records the requested durable config."""

    def __init__(self, subscription):
        self.subscription = subscription
        self.durable = None
        self.config = None

    async def pull_subscribe(self, subject, durable=None, config=None):
        self.durable = durable
        self.config = config
        return self.subscription


class DurableConfigDriftTests(unittest.TestCase):
    """An existing durable keeps its first config; drift must be reported, never silent."""

    def run_consume(self, js, **kwargs):
        return asyncio.run(consume(js, 'poc', lambda body: None, total=0, **kwargs))

    def test_matching_durable_config_does_not_warn(self):
        info = FakeConsumerInfo('worker-1', 'TASKS', max_deliver=3, ack_wait=30.0, max_ack_pending=50)
        js = FakeDurableJS(FakePullSubscription(info=info))
        with self.assertNoLogs('app.jetstream_bus', level='WARNING'):
            self.run_consume(js, durable='worker-1', max_deliver=3, ack_wait=30, max_ack_pending=50)
        self.assertEqual(js.config.max_ack_pending, 50)

    def test_mismatched_durable_config_warns_with_names_and_values(self):
        info = FakeConsumerInfo('worker-1', 'TASKS', max_deliver=5, ack_wait=60.0, max_ack_pending=1000)
        js = FakeDurableJS(FakePullSubscription(info=info))
        with self.assertLogs('app.jetstream_bus', level='WARNING') as captured:
            self.run_consume(js, durable='worker-1', max_deliver=3, ack_wait=30, max_ack_pending=50)
        self.assertEqual(len(captured.records), 1)
        text = captured.records[0].getMessage()
        for expected in ('worker-1', 'TASKS', 'max_deliver', 'requested=3', 'actual=5',
                         'ack_wait', 'requested=30', 'actual=60.0',
                         'max_ack_pending', 'requested=50', 'actual=1000', 'delete'):
            self.assertIn(expected, text)

    def test_unreadable_consumer_info_does_not_warn_or_crash(self):
        subscription = FakePullSubscription(error=RuntimeError('permissions violation'))
        js = FakeDurableJS(subscription)
        with self.assertNoLogs('app.jetstream_bus', level='WARNING'):
            processed = self.run_consume(js, durable='worker-1', max_deliver=3)
        self.assertEqual(processed, 0)
        self.assertEqual(subscription.consumer_info_calls, 1)

    def test_non_durable_subscription_is_not_checked(self):
        subscription = FakePullSubscription(info=FakeConsumerInfo('x', 'TASKS', max_deliver=9))
        js = FakeDurableJS(subscription)
        with self.assertNoLogs('app.jetstream_bus', level='WARNING'):
            self.run_consume(js, durable=None, max_deliver=3)
        self.assertEqual(subscription.consumer_info_calls, 0)

    def test_subscription_without_consumer_info_is_tolerated(self):
        js = FakeDurableJS(object())
        with self.assertNoLogs('app.jetstream_bus', level='WARNING'):
            self.run_consume(js, durable='worker-1', max_deliver=3)
        self.assertEqual(js.config.max_deliver, 3)


if __name__ == '__main__':
    unittest.main()
