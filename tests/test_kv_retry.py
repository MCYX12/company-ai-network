"""Shared-KV retry linkage: the HA recovery state machine the Router relies on."""
import unittest

from app import kv_task_store


class Store(kv_task_store.KvTaskStore):
    def __init__(self):
        super().__init__(js=None)
        self.data = {}
        self.tasks = {}
        self.idem = {}

    async def _read(self, key):
        if key in self.idem:
            return self.idem[key]
        return self.data.get(key)

    async def _put(self, key, value):
        self.data[key] = value

    async def _scan_retries(self):
        return [value for key, value in self.data.items()
                if key.startswith(kv_task_store.RETRY_PREFIX)]

    async def get(self, task_id):
        return self.tasks.get(task_id)

    async def submit(self, owner, target, key, operation, payload, ttl, retry_of=None):
        idem_key = kv_task_store.IDEM_PREFIX + owner + '.' + key
        if idem_key in self.idem:
            raise ValueError('IDEMPOTENCY_CONFLICT')      # a different remaining TTL
        task_id = 'new-' + key
        row = {'task_id': task_id, 'state': 'accepted', 'owner': owner, 'target': target,
               'idempotency_key': key, 'next_send': 0}
        if retry_of:
            row['retry_of'] = retry_of
            row['retry_pending'] = True
            row['next_send'] = float('inf')
        self.tasks[task_id] = row
        self.idem[idem_key] = task_id
        return row

    async def _mutate(self, key, fn, attempts=12):
        task_id = key[len(kv_task_store.TASK_PREFIX):]
        row = self.tasks.get(task_id)
        if row is None:
            return None
        updated, result = fn(dict(row))
        if updated is not None:
            self.tasks[task_id] = updated
        return result

    async def _mutate_status(self, key, fn, attempts=12):
        result = await self._mutate(key, fn, attempts)
        return ('committed', result) if result is not None else ('missing', None)


class KvRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store()
        self.store.tasks['root'] = {'task_id': 'root', 'state': 'failed', 'owner': 'ag-a',
                                    'idempotency_key': 'k0'}

    async def _retry(self, ttl=600):
        return await self.store.submit_retry('root', 'ag-a', 'ag-b', 'ask_peer', {'q': 'x'}, ttl)

    async def test_submit_retry_links_both_directions(self):
        new = await self._retry()
        self.assertEqual(new['task_id'], 'new-retry-root')
        linkage = await self.store.retry_for('root')
        self.assertEqual(linkage['retry_task_id'], 'new-retry-root')
        self.assertTrue(await self.store.is_retry('new-retry-root'))
        self.assertEqual((await self.store.retried_from('new-retry-root'))['root_task_id'], 'root')

    async def test_submit_retry_is_idempotent(self):
        first = await self._retry()
        second = await self._retry()
        self.assertEqual(first['task_id'], second['task_id'])
        self.assertEqual(len(self.store.tasks), 2)

    async def test_half_written_reverse_link_is_repaired(self):
        await self._retry()
        del self.store.data[kv_task_store.RETRY_REVERSE_PREFIX + 'new-retry-root']
        linkage = await self.store.retried_from('new-retry-root')
        self.assertEqual(linkage['root_task_id'], 'root')
        self.assertIn(kv_task_store.RETRY_REVERSE_PREFIX + 'new-retry-root', self.store.data)

    async def test_ttl_change_adopts_the_existing_replacement(self):
        await self._retry(ttl=600)
        # forward and reverse linkage lost, but the task and its idempotency index remain
        del self.store.data[kv_task_store.RETRY_PREFIX + 'root']
        del self.store.data[kv_task_store.RETRY_REVERSE_PREFIX + 'new-retry-root']
        again = await self._retry(ttl=300)
        self.assertEqual(again['task_id'], 'new-retry-root')
        self.assertEqual((await self.store.retry_for('root'))['retry_task_id'], 'new-retry-root')

    async def test_child_is_released_only_after_linkage(self):
        new = await self._retry()
        self.assertFalse(new.get('retry_pending'))
        self.assertEqual(self.store.tasks[new['task_id']]['next_send'], 0)

    async def test_linkage_failure_keeps_the_child_out_of_dispatch(self):
        original = self.store.record_retry

        async def fail(*_args, **_kwargs):
            raise RuntimeError('linkage write failed')
        self.store.record_retry = fail
        with self.assertRaises(RuntimeError):
            await self._retry()
        child = next(row for row in self.store.tasks.values() if row.get('retry_of'))
        self.assertTrue(child['retry_pending'])
        self.assertEqual(child['next_send'], float('inf'))   # not in the dispatchable list
        self.assertTrue(await self.store.is_retry(child['task_id']))
        self.store.record_retry = original

    async def test_existing_child_is_released_on_reentry(self):
        new = await self._retry()
        self.store.tasks[new['task_id']]['retry_pending'] = True
        self.store.tasks[new['task_id']]['next_send'] = float('inf')
        again = await self._retry()
        self.assertFalse(again.get('retry_pending'))          # released, not stuck forever
        self.assertEqual(again['next_send'], 0)

    async def test_unknown_root_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.store.submit_retry('missing', 'ag-a', 'ag-b', 'ask_peer', {}, 600)

    async def test_original_is_not_a_retry(self):
        self.assertFalse(await self.store.is_retry('root'))
        self.assertIsNone(await self.store.retry_for('new-retry-root'))


if __name__ == '__main__':
    unittest.main()
