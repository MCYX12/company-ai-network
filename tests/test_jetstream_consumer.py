"""JetStream consumer semantics: target isolation, correct max_deliver, DLQ, bad JSON."""
import asyncio
import json
import unittest

from app.jetstream_bus import consume


class FakeMetadata:
    def __init__(self, num_delivered):
        self.num_delivered = num_delivered


class FakeMessage:
    def __init__(self, data, num_delivered=1):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.metadata = FakeMetadata(num_delivered)
        self.acked = 0
        self.naked = 0
        self.terminated = 0
        self.progress = 0

    async def ack(self):
        self.acked += 1

    async def nak(self):
        self.naked += 1

    async def term(self):
        self.terminated += 1

    async def in_progress(self):
        self.progress += 1


class FakeSubscription:
    def __init__(self, batches):
        self.batches = list(batches)

    async def fetch(self, count, timeout=None):
        if self.batches:
            return self.batches.pop(0)
        raise Exception('fetch timeout')


class FakeJS:
    def __init__(self, batches):
        self.subscription = FakeSubscription(batches)
        self.published = []
        self.subject = None
        self.config = None

    async def pull_subscribe(self, subject, durable=None, config=None):
        self.subject = subject
        self.config = config
        return self.subscription

    async def publish(self, subject, data):
        self.published.append((subject, data))


class ConsumerTests(unittest.TestCase):
    def test_target_isolates_the_filter_subject(self):
        js = FakeJS([])
        asyncio.run(consume(js, 'poc', lambda body: None, target='peer-x', total=0))
        self.assertEqual(js.subject, 'poc.task.peer-x')

    def test_no_target_uses_the_wildcard(self):
        js = FakeJS([])
        asyncio.run(consume(js, 'poc', lambda body: None, total=0))
        self.assertEqual(js.subject, 'poc.task.>')

    def test_ack_wait_is_configurable(self):
        js = FakeJS([])
        asyncio.run(consume(js, 'poc', lambda body: None, total=0, ack_wait=45))
        self.assertEqual(js.config.ack_wait, 45)

    def test_malformed_message_is_quarantined_without_crashing(self):
        bad = FakeMessage(b'{not-json')
        js = FakeJS([[bad]])
        calls = []

        async def handler(body):
            calls.append(body)

        asyncio.run(consume(js, 'poc', handler, total=1))
        self.assertEqual(calls, [])
        self.assertEqual(js.published[0][0], 'poc.dlq.unknown')
        self.assertEqual(bad.terminated, 1)

    def test_handler_runs_once_before_dlq_when_max_deliver_is_one(self):
        msg = FakeMessage({'task_id': 't', 'target': 'peer-x'}, num_delivered=1)
        js = FakeJS([[msg]])
        attempts = []

        async def handler(body):
            attempts.append(body)
            raise ValueError('boom')

        asyncio.run(consume(js, 'poc', handler, max_deliver=1, total=1))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(js.published[0][0], 'poc.dlq.peer-x')
        self.assertEqual(msg.terminated, 1)
        self.assertEqual(msg.naked, 0)

    def test_failed_attempt_below_max_deliver_naks(self):
        msg = FakeMessage({'task_id': 't', 'target': 'peer-x'}, num_delivered=1)
        js = FakeJS([[msg]])

        async def handler(body):
            raise ValueError('boom')

        asyncio.run(consume(js, 'poc', handler, max_deliver=3, total=1))
        self.assertEqual(msg.naked, 1)
        self.assertEqual(js.published, [])

    def test_success_acks_and_does_not_dead_letter(self):
        msg = FakeMessage({'task_id': 't', 'target': 'peer-x'}, num_delivered=1)
        js = FakeJS([[msg]])
        seen = []

        async def handler(body):
            seen.append(body['task_id'])

        asyncio.run(consume(js, 'poc', handler, total=1))
        self.assertEqual(seen, ['t'])
        self.assertEqual(msg.acked, 1)
        self.assertEqual(js.published, [])


if __name__ == '__main__':
    unittest.main()
