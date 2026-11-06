import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app.common import decode, encode
from app.lease import LeaseStore
from app.task_worker import TaskWorker

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


class FakeNc:
    def __init__(self):
        self.published = []

    async def publish(self, subject, data):
        self.published.append((subject, decode(data)))


class FakeMsg:
    def __init__(self, body):
        self.data = encode(body)


class FencingTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.runtime = Path(self.tmp.name)
        self.lease = LeaseStore(self.runtime / 'leases.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_epoch_increments_and_stale_detection(self):
        first = self.lease.acquire('dispatch', 'r1', ttl=15, now=1000)
        self.assertEqual(first['epoch'], 1)
        self.assertFalse(self.lease.stale('dispatch', 1))
        self.lease.release('dispatch', first['token'])
        second = self.lease.acquire('dispatch', 'r2', ttl=15, now=1001)
        self.assertEqual(second['epoch'], 2)
        self.assertTrue(self.lease.stale('dispatch', 1))
        self.assertFalse(self.lease.stale('dispatch', 2))
        self.assertFalse(self.lease.stale('dispatch', None))

    def test_worker_rejects_stale_dispatch_without_claiming(self):
        first = self.lease.acquire('dispatch', 'r1', ttl=15, now=1000)
        self.lease.release('dispatch', first['token'])
        self.lease.acquire('dispatch', 'r2', ttl=15, now=1001)  # epoch 2
        nc = FakeNc()
        with patch('app.task_worker.RUNTIME', self.runtime):
            worker = TaskWorker(nc, {'agent_id': 'peer-x', 'tool': 'codex', 'projects': ['demo-project']})
            body = {'task_id': 't1', 'target': 'peer-x', 'operation': 'ask_peer', 'payload': {'question': 'x'},
                    'deadline': time.time() + 60, 'lease_epoch': 1}
            asyncio.run(worker.receive(FakeMsg(body)))
            self.assertEqual(nc.published[-1][1]['code'], 'STALE_DISPATCH')
            row = worker.store.db.execute("SELECT * FROM work WHERE task_id='t1'").fetchone()
            self.assertIsNone(row)

    def test_current_epoch_is_accepted_by_fencing(self):
        self.lease.acquire('dispatch', 'r1', ttl=15, now=1000)
        self.assertFalse(self.lease.stale('dispatch', 1))


if __name__ == '__main__':
    unittest.main()
