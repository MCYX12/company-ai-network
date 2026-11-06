"""KV task store quota: measured size and real terminal cleanup."""
import unittest

from app import kv_task_store


class FakeKv:
    def __init__(self):
        self.deleted = []

    async def delete(self, key):
        self.deleted.append(key)


class Store(kv_task_store.KvTaskStore):
    def __init__(self, rows):
        super().__init__(js=None)
        self._rows = rows
        self.kv = FakeKv()
        self.data = {}

    async def _put(self, key, value):
        self.data[key] = value

    async def _mutate(self, key, fn, attempts=12):
        task_id = key[len(kv_task_store.TASK_PREFIX):]
        for index, row in enumerate(self._rows):
            if row['task_id'] == task_id:
                updated, result = fn(dict(row))
                if updated is not None:
                    self._rows[index] = updated
                return result
        return None

    async def _scan(self):
        return [dict(row) for row in self._rows]

    async def _bucket(self):
        return self.kv

    async def dead_letters(self, limit=100):
        return [row for row in self._rows if row['state'] == 'failed']


def row(task_id, state='succeeded', created=1.0, result=None):
    return {'task_id': task_id, 'state': state, 'created_at': created, 'updated_at': created,
            'result': result or '{}', 'owner': 'ag-a', 'idempotency_key': 'k-' + task_id}


class QuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_size_is_measured_not_a_placeholder(self):
        store = Store([row('a'), row('b', state='running')])
        status = await store.quota_status(limit_bytes=1000000000)
        self.assertGreater(status['size_bytes'], 0)
        self.assertFalse(status['over_quota'])
        self.assertEqual(status['pending'], 1)

    async def test_over_quota_purges_oldest_terminal_only(self):
        rows = [row('old', created=1.0), row('new', created=3.0), row('live', state='running', created=2.0)]
        store = Store(rows)
        status = await store.quota_status(limit_bytes=1)
        self.assertTrue(status['over_quota'])
        result = await store.enforce_quota(limit_bytes=1)
        self.assertEqual(result['purged'], 2)
        # Terminal rows are compacted in place (no delete): get() and dedup still work, and the
        # running row is untouched.
        compacted = sorted(row['task_id'] for row in store._rows if row.get('compacted'))
        self.assertEqual(compacted, ['new', 'old'])
        self.assertFalse(next(r for r in store._rows if r['task_id'] == 'live').get('compacted'))
        self.assertEqual(store.kv.deleted, [])

    async def test_before_purges_only_older_terminal(self):
        store = Store([row('old', created=1.0), row('new', created=10.0)])
        result = await store.enforce_quota(before=5.0)
        self.assertEqual(result['purged'], 1)
        self.assertTrue(next(r for r in store._rows if r['task_id'] == 'old').get('compacted'))
        self.assertFalse(next(r for r in store._rows if r['task_id'] == 'new').get('compacted'))


if __name__ == '__main__':
    unittest.main()
