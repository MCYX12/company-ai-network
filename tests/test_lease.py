import multiprocessing
import tempfile
from pathlib import Path
import time
import unittest

from app.lease import LeaseStore


def _contend(path, name, worker_id, rounds, sink):
    store = LeaseStore(path)
    acquired = 0
    for _ in range(rounds):
        # A 30 s TTL keeps the mutual-exclusion assertion meaningful on a loaded host: with a
        # 1 s TTL, a worker preempted for longer than the TTL lost the lease *legitimately*,
        # and the holder() check below then reported a false 'overlap' (seen 2026-10-04 while
        # the full suite ran under load). The invariant under test is "no two live holders at
        # the same time", not "a holder can never be descheduled".
        lease = store.acquire(name, f'w{worker_id}', ttl=30)
        if lease:
            acquired += 1
            current = store.holder(name)
            if not current or current['token'] != lease['token']:
                sink.put(('overlap', worker_id))
            time.sleep(0.005)
            store.release(name, lease['token'])
    sink.put(('done', worker_id, acquired))


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'leases.sqlite3'

    def tearDown(self):
        self.tmp.cleanup()

    def test_mutual_exclusion_and_fencing(self):
        a = LeaseStore(self.path); b = LeaseStore(self.path)
        now = 1000.0
        first = a.acquire('dispatch', 'router-a', ttl=10, now=now)
        self.assertIsNotNone(first)
        self.assertIsNone(b.acquire('dispatch', 'router-b', ttl=10, now=now))
        self.assertEqual(a.holder('dispatch', now=now)['holder'], 'router-a')
        later = now + 11
        second = b.acquire('dispatch', 'router-b', ttl=10, now=later)
        self.assertIsNotNone(second)
        self.assertGreater(second['epoch'], first['epoch'])
        self.assertIsNone(a.renew('dispatch', 'router-a', first['token'], ttl=10, now=later))
        self.assertIsNotNone(b.renew('dispatch', 'router-b', second['token'], ttl=10, now=later))

    def test_renew_without_expiry_and_reap(self):
        store = LeaseStore(self.path)
        lease = store.acquire('x', 'h', ttl=5, now=1000.0)
        renewed = store.renew('x', 'h', lease['token'], ttl=5, now=1003.0)
        self.assertGreater(renewed['epoch'], lease['epoch'])
        self.assertIsNone(store.renew('x', 'other', lease['token'], ttl=5, now=1004.0))
        self.assertEqual(store.reap(now=1003.5), 0)
        self.assertEqual(store.holder('x', now=1009.0), None)
        self.assertEqual(store.reap(now=1009.0), 1)

    def test_processes_share_one_lease_without_overlap(self):
        try:
            sink = multiprocessing.Queue()
        except (PermissionError, OSError) as exc:
            # Some sandboxes deny POSIX semaphores; the multi-process assertion is
            # meaningless there, so report it as skipped rather than failed.
            self.skipTest('multiprocessing semaphores unavailable: %s' % exc)
        procs = [multiprocessing.Process(target=_contend, args=(str(self.path), 'dispatch', i, 30, sink)) for i in range(4)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(40)
        results = [sink.get(timeout=5) for _ in range(len(procs))]
        self.assertEqual([r for r in results if r[0] == 'overlap'], [])
        self.assertGreater(sum(r[2] for r in results), 0)


if __name__ == '__main__':
    unittest.main()
