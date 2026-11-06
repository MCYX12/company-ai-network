"""One contract test suite for the built-in and standalone device workers.

The assessment found two execution paths: app/task_worker.py had a durable claim/outbox core,
while packaging/client/worker.py answered from Core NATS in one shot (no claim, no outbox, no
ack), so a Router outage during execution lost the result and a redelivery could run the model
again. Both now use app/worker_kernel.py; these cases pin the shared contract and are run
against BOTH entry points, so the two paths cannot drift apart again.
"""
import asyncio
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.worker_kernel import (EnvelopeError, WorkerLedger, active_grants, expired, validate_envelope,
                               ReliableTaskRunner)
from app.task_worker import TaskWorker

ROOT = Path(__file__).resolve().parents[1]


def load_client_worker():
    spec = importlib.util.spec_from_file_location('device_worker_contract', ROOT / 'packaging/client/worker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Msg:
    def __init__(self, body):
        self.data = json.dumps(body).encode()


class _FakeNc:
    def __init__(self):
        self.published = []

    async def publish(self, subject, data):
        self.published.append((subject, json.loads(data)))


class _RecordingExecutor:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {'code': 'OK', 'data': {'text': 'ok'}}

    async def __call__(self, envelope, grants, history):
        self.calls.append({'task_id': envelope['task_id'], 'grants': grants, 'history': history})
        return dict(self.result)


class BuiltinHarness:
    """The server worker driven through receive(), captured on a fake NATS connection."""

    def __init__(self, tmp):
        self.nc = _FakeNc()
        self.exec = _RecordingExecutor()
        self.patcher = patch('app.task_worker.RUNTIME', Path(tmp))
        self.patcher.start()
        recorder = self.exec

        class Worker(TaskWorker):
            async def _execute(self, envelope, grants, history):
                return await recorder(envelope, grants, history)

        self.worker = Worker(self.nc, {'agent_id': 'peer-x', 'tool': 'codex', 'projects': ['demo-project']})

    def feed(self, body):
        asyncio.run(self.worker.receive(_Msg(body)))

    def flush(self):
        for task_id, result in self.worker.store.outbox():
            self.nc.published.append(('result', {'task_id': task_id, **result}))

    def results(self):
        return [body for _subject, body in self.nc.published if 'code' in body]

    def outbox(self):
        return self.worker.store.outbox()

    def close(self):
        self.worker.store.db.close()
        self.patcher.stop()


class ClientHarness:
    """The standalone client worker driven through handle(), captured on callbacks."""

    def __init__(self, tmp):
        module = load_client_worker()
        self.exec = _RecordingExecutor()
        self.published = []
        self.client = module.ClientWorker('peer-x', WorkerLedger(Path(tmp) / 'worker-peer-x.sqlite3'),
                                          self.exec, self._started, self._result)
        self.ledger = self.client.ledger

    async def _started(self, task_id):
        self.published.append({'type': 'task_started', 'task_id': task_id})

    async def _result(self, task_id, result):
        self.published.append({'task_id': task_id, **result})

    def feed(self, body):
        asyncio.run(self.client.handle(body))

    def flush(self):
        for task_id, result in self.ledger.outbox():
            self.published.append({'task_id': task_id, **result})

    def results(self):
        return [body for body in self.published if 'code' in body]

    def outbox(self):
        return self.ledger.outbox()

    def close(self):
        self.ledger.db.close()


def envelope(task_id='t1', **overrides):
    body = {'task_id': task_id, 'target': 'peer-x', 'operation': 'ask_peer',
            'payload': {'question': '进展如何'}, 'deadline': time.time() + 60}
    body.update(overrides)
    return body


class KernelUnitTests(unittest.TestCase):
    def test_standalone_worker_reports_an_install_digest(self):
        module = load_client_worker()
        self.assertRegex(module.INSTALL_DIGEST or '', r'^[0-9a-f]{16}$')

    def test_validate_envelope_accepts_and_rejects(self):
        self.assertIsNotNone(validate_envelope(envelope(), 'peer-x'))
        for bad in ({},
                    envelope(target='other'),
                    envelope(task_id=''),
                    envelope(payload='no'),
                    envelope(deadline='soon'),
                    envelope(notes_grants={}),
                    {'task_id': 't', 'target': 'peer-x', 'operation': 'ask_peer',
                     'payload': {}, 'deadline': 1, 'surprise': 1}):
            with self.assertRaises(EnvelopeError, msg=bad):
                validate_envelope(bad, 'peer-x')

    def test_active_grants_checks_state_and_expiry(self):
        now = 1000.0
        grants = [{'id': 'ok', 'root': '/a', 'state': 'approved', 'expires': now + 10},
                  {'id': 'old', 'root': '/b', 'state': 'approved', 'expires': now - 1},
                  {'id': 'revoked', 'root': '/c', 'state': 'revoked'}]
        usable, rejected = active_grants(grants, now=now)
        self.assertEqual([g['id'] for g in usable], ['ok'])
        self.assertEqual(sorted(r['reason'] for r in rejected), ['EXPIRED', 'NOT_APPROVED'])
        history, _ = active_grants([{'id': 'h', 'scope': '*', 'state': 'approved'}], now=now, require_root=False)
        self.assertEqual([g['id'] for g in history], ['h'])

    def test_ledger_claims_once_and_rejects_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = WorkerLedger(Path(tmp) / 'w.sqlite3')
            try:
                body = envelope()
                self.assertTrue(ledger.claim(body))
                self.assertFalse(ledger.claim(body))          # same envelope: a repeat, not a conflict
                with self.assertRaises(ValueError):
                    ledger.claim(dict(body, payload={'question': 'different'}))
            finally:
                ledger.db.close()

    def test_outbox_persists_until_acknowledged(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = WorkerLedger(Path(tmp) / 'w.sqlite3')
            try:
                ledger.claim(envelope())
                ledger.finish('t1', {'code': 'OK', 'data': {'text': 'hi'}})
                self.assertEqual(ledger.outbox()[0][1]['data']['text'], 'hi')
                self.assertEqual(ledger.pending()['unacked'], 1)
                ledger.acknowledge('t1')
                self.assertEqual(ledger.outbox(), [])
            finally:
                ledger.db.close()

    def test_local_epoch_fencing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = WorkerLedger(Path(tmp) / 'w.sqlite3')
            try:
                self.assertFalse(ledger.stale_epoch(5))
                self.assertFalse(ledger.stale_epoch(5))
                self.assertFalse(ledger.stale_epoch(6))
                self.assertTrue(ledger.stale_epoch(4))
                self.assertFalse(ledger.stale_epoch(None))
            finally:
                ledger.db.close()

    def test_denied_history_grant_reaches_the_execution_path(self):
        seen = {}

        async def execute(envelope_, notes, history):
            seen['history'] = history
            return {'code': 'INVALID_REQUEST', 'data': {'hint': 'HISTORY_ACCESS_DENIED_BY_ADMIN'}}

        with tempfile.TemporaryDirectory() as tmp:
            ledger = WorkerLedger(Path(tmp) / 'w.sqlite3')

            async def publish_started(task_id):
                pass

            async def publish_result(task_id, result):
                pass

            runner = ReliableTaskRunner('peer-x', ledger, execute, publish_started, publish_result)
            body = envelope(history_grants=[{'id': 'h', 'caller': 'ag-a', 'target': 'peer-x',
                                             'state': 'denied', 'expires': 9e9, 'scope': '*'}])
            self.assertEqual(asyncio.run(runner.handle(body)), 'done')
            # The tri-state veto must arrive intact, not be dropped as "not approved".
            self.assertEqual([grant['id'] for grant in seen['history']], ['h'])
            result = ledger.outbox()[0][1]
            self.assertEqual(result['code'], 'INVALID_REQUEST')
            self.assertEqual(result['data']['hint'], 'HISTORY_ACCESS_DENIED_BY_ADMIN')
            ledger.db.close()

    def test_runner_normalises_failures_and_reports_grant_rejections(self):
        async def boom(envelope_, grants, history):
            raise RuntimeError('model down')

        with tempfile.TemporaryDirectory() as tmp:
            ledger = WorkerLedger(Path(tmp) / 'w.sqlite3')
            published = []
            async def publish_started(task_id):
                published.append(('started', task_id))
            async def publish_result(task_id, result):
                published.append(('result', result))
            runner = ReliableTaskRunner('peer-x', ledger, boom, publish_started, publish_result)
            body = envelope(notes_grants=[{'id': 'g', 'root': '/a', 'state': 'approved',
                                           'expires': time.time() - 1}])
            self.assertEqual(asyncio.run(runner.handle(body)), 'done')
            self.assertEqual(ledger.outbox()[0][1]['code'], 'EXECUTION_FAILED')
            self.assertEqual([entry for entry in published if entry[0] == 'started'],
                             [('started', 't1')])
            ledger.db.close()


    def test_started_publish_failure_still_finishes_the_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = WorkerLedger(Path(tmp) / 'w.sqlite3')

            async def execute(envelope_, notes, history):
                return {'code': 'OK', 'data': {'text': 'ok'}}

            async def publish_started(task_id):
                raise RuntimeError('transport down')     # must not strand the claimed task

            async def publish_result(task_id, result):
                pass

            runner = ReliableTaskRunner('peer-x', ledger, execute, publish_started, publish_result)
            self.assertEqual(asyncio.run(runner.handle(envelope())), 'done')
            self.assertEqual(ledger.outbox()[0][1]['code'], 'OK')
            self.assertEqual(runner.inflight, 0)
            self.assertEqual(runner.notice_failures, 1)   # the transport failure is observable
            ledger.db.close()


class KernelTimingTests(unittest.TestCase):
    def test_runner_stamps_phase_timings_on_a_data_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = WorkerLedger(Path(tmp) / 'w.sqlite3')

            async def execute(envelope_, notes, history):
                return {'code': 'OK', 'data': {'text': 'ok'}}

            async def started(task_id):
                pass

            async def publish_result(task_id, result):
                pass

            times = iter([100.0, 100.5, 102.0])
            runner = ReliableTaskRunner('peer-x', ledger, execute, started, publish_result,
                                        clock=lambda: next(times))
            self.assertEqual(asyncio.run(runner.handle(envelope())), 'done')
            timings = ledger.outbox()[0][1]['data']['timings']
            self.assertEqual(timings, {'received_at': 100.0, 'claim_seconds': 0.5,
                                       'execution_seconds': 1.5, 'total_seconds': 2.0})
            ledger.db.close()

    def test_inflight_is_tracked_and_wait_idle_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = WorkerLedger(Path(tmp) / 'w.sqlite3')

            async def slow(envelope_, notes, history):
                await asyncio.sleep(0.15)
                return {'code': 'OK', 'data': {'text': 'ok'}}

            async def noop(*_):
                pass

            runner = ReliableTaskRunner('peer-x', ledger, slow, noop, noop)

            async def scenario():
                task = asyncio.ensure_future(runner.handle(envelope()))
                for _ in range(100):
                    if runner.inflight == 1:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(runner.inflight, 1)
                self.assertTrue(await runner.wait_idle(2))
                self.assertEqual(runner.inflight, 0)
                await task

            asyncio.run(scenario())
            ledger.db.close()


class ContractCases:
    """Cases every worker implementation must satisfy, run against each in turn."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.harness = self.make_harness(self.tmp.name)

    def tearDown(self):
        self.harness.close()
        self.tmp.cleanup()

    def test_duplicate_delivery_executes_once(self):
        body = envelope()
        self.harness.feed(body)
        self.harness.feed(body)
        self.assertEqual(len(self.harness.exec.calls), 1)
        self.assertIn('OK', [r['code'] for r in self.harness.results()])

    def test_expired_deadline_never_executes(self):
        self.harness.feed(envelope(deadline=time.time() - 1))
        self.assertEqual(self.harness.exec.calls, [])
        self.assertEqual({r['code'] for r in self.harness.results()}, {'EXPIRED'})

    def test_expired_grant_is_withheld_before_any_read_and_reported(self):
        grants = [{'id': 'old', 'root': '/secret', 'state': 'approved', 'expires': time.time() - 1},
                  {'id': 'live', 'root': '/ok', 'state': 'approved', 'expires': time.time() + 600}]
        self.harness.feed(envelope(notes_grants=grants))
        call = self.harness.exec.calls[0]
        self.assertEqual([g['id'] for g in call['grants']], ['live'])
        self.harness.flush()
        result = self.harness.results()[0]
        self.assertEqual([r['reason'] for r in result['data']['directory_grants_rejected']], ['EXPIRED'])

    def test_invalid_envelope_is_dropped_without_execution(self):
        self.harness.feed(envelope(target='someone-else'))
        self.harness.feed({'task_id': 'x', 'target': 'peer-x', 'operation': 'ask_peer',
                           'payload': {}, 'deadline': time.time() + 60, 'surprise': True})
        self.assertEqual(self.harness.exec.calls, [])
        self.harness.flush()
        self.assertEqual(self.harness.results(), [])

    def test_result_carries_worker_phase_timings(self):
        self.harness.feed(envelope())
        result = next(r for r in self.harness.results() if r.get('code') == 'OK')
        timings = result['data']['timings']
        for key in ('received_at', 'claim_seconds', 'execution_seconds', 'total_seconds'):
            self.assertIn(key, timings)
        self.assertGreaterEqual(timings['total_seconds'], timings['execution_seconds'])

    def test_result_stays_until_acknowledged(self):
        self.harness.feed(envelope())
        self.assertEqual(len(self.harness.outbox()), 1)
        # Published immediately (not waiting for the pump tick), so the caller is not delayed.
        self.assertIn('OK', [r['code'] for r in self.harness.results()])
        before = len(self.harness.results())
        # The still-unacked result is resent (Router was offline): the pump must not stop early.
        self.harness.flush()
        self.assertGreater(len(self.harness.results()), before)
        self._acknowledge('t1')
        self.assertEqual(self.harness.outbox(), [])
        settled = len(self.harness.results())
        self.harness.flush()
        self.assertEqual(len(self.harness.results()), settled)


class BuiltinContractTests(ContractCases, unittest.TestCase):
    def make_harness(self, tmp):
        return BuiltinHarness(tmp)

    def _acknowledge(self, task_id):
        self.harness.worker.store.acknowledge(task_id)

    def test_a_draining_worker_claims_no_new_task(self):
        self.harness.worker.stop_consuming()
        self.harness.feed(envelope())
        self.assertEqual(self.harness.exec.calls, [])
        self.assertEqual(self.harness.results(), [])

    def test_draining_does_not_abort_the_in_flight_task(self):
        worker = self.harness.worker
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(envelope_, grants, history):
            started.set()
            await release.wait()
            return {'code': 'OK', 'data': {'text': 'drained-ok'}}

        worker.runner._execute = slow

        async def scenario():
            # The durable consumer awaits process(); cancelling it used to cancel the
            # in-flight handle and lose the task.
            worker._jetstream_task = asyncio.ensure_future(worker.process(envelope()))
            await asyncio.wait_for(started.wait(), 2)
            worker.stop_consuming()
            release.set()
            await asyncio.wait_for(worker._jetstream_task, 2)

        asyncio.run(scenario())
        outbox = self.harness.outbox()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0][1]['code'], 'OK')
        self.assertEqual(worker.runner.inflight, 0)


class ClientContractTests(ContractCases, unittest.TestCase):
    def make_harness(self, tmp):
        return ClientHarness(tmp)

    def _acknowledge(self, task_id):
        self.harness.ledger.acknowledge(task_id)

    def test_stale_epoch_is_refused_without_claiming(self):
        self.harness.feed(envelope(task_id='t1', lease_epoch=5))
        self.harness.feed(envelope(task_id='t2', lease_epoch=4))
        self.assertEqual([c['task_id'] for c in self.harness.exec.calls], ['t1'])
        stale = [r for r in self.harness.published if r.get('code') == 'STALE_DISPATCH']
        self.assertEqual(len(stale), 1)
        self.assertEqual([task_id for task_id, _result in self.harness.outbox()], ['t1'])


if __name__ == '__main__':
    unittest.main()
