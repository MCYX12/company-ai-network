"""Business readiness is a gate, not a label.

A worker that has only printed a ready line, or is draining for maintenance, must not be
advertised as answerable and must not receive a dispatch. The state comes from the
heartbeat and defaults to ready so an older worker keeps working.
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.common import PREFIX, decode, encode
from app.conversation_access import AccessStore


class FakeNc:
    def __init__(self):
        self.published = []

    async def publish(self, subject, data):
        self.published.append((subject, decode(data)))


class FakeMsg:
    def __init__(self, source, body):
        self.subject = f'{PREFIX}.ingress.{source}.request'
        self.data = encode(body)


class ReadinessGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        (self.runtime / 'registry.json').write_text(json.dumps({'schema_version': 1, 'agents': [
            {'agent_id': 'ag-a', 'device_id': 'ubuntu-demo', 'employee_id': 'demo-a',
             'tool': 'codex', 'projects': ['demo-project'], 'enabled': True},
            {'agent_id': 'ag-b', 'device_id': 'mac-demo', 'employee_id': 'demo-b',
             'tool': 'codex', 'projects': ['demo-project'], 'enabled': True}]}))
        store = AccessStore(self.runtime / 'conversation-access.sqlite3')
        row = store.request('ag-a', 'ag-b', ['demo-project'])
        store.decide(row['approval_id'], 'approved', 3600, 'readiness')
        # The heartbeat loads the registry and the dispatch path opens the grant ledgers, so
        # every patch must stay active for the whole test, not just for Router construction.
        for patcher in (patch.dict(os.environ, {'AGENT_REGISTRY': str(self.runtime / 'registry.json')}),
                        patch('app.router.RUNTIME', self.runtime),
                        patch('app.conversation_access.RUNTIME', self.runtime),
                        patch('app.common.RUNTIME', self.runtime),
                        patch('app.registry.RUNTIME', self.runtime),
                        patch('app.iam.RUNTIME', self.runtime),
                        patch('app.admin_channel.RUNTIME', self.runtime),
                        patch('app.directory_requests.RUNTIME', self.runtime),
                        patch('app.history_access.RUNTIME', self.runtime)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _router(self):
        from app.router import Router
        nc = FakeNc()
        return Router(nc), nc

    def _heartbeat(self, router, readiness):
        body = {'agent_id': 'ag-b', 'device_id': 'mac-demo', 'status': 'online',
                'capabilities': ['get_status', 'ask_peer'], 'task_actions': ['ask_peer'],
                'adapter_status': 'ready', 'readiness': readiness}
        asyncio.run(router.heartbeat(SimpleNamespace(
            subject=f'{PREFIX}.agent.ag-b.heartbeat', data=encode(body))))

    def _body(self, action, payload=None, target=None):
        body = {'schema_version': '1.0', 'message_id': str(uuid.uuid4()),
                'correlation_id': str(uuid.uuid4()), 'sender_agent_id': 'ag-a',
                'action': action, 'expires_at': time.time() + 5, 'payload': payload or {}}
        if target:
            body['target_agent_id'] = target
        return body

    def _agents(self, router, nc):
        asyncio.run(router.ingress(FakeMsg('ag-a', self._body('list_agents'))))
        return {a['agent_id']: a for a in nc.published[-1][1]['data']['agents']}

    def test_a_starting_worker_is_listed_but_advertises_no_action(self):
        router, nc = self._router()
        self._heartbeat(router, 'starting')
        row = self._agents(router, nc)['ag-b']
        self.assertEqual(row['readiness'], 'starting')
        self.assertEqual(row['available_task_actions'], [])

    def test_a_ready_worker_advertises_ask_peer(self):
        router, nc = self._router()
        self._heartbeat(router, 'ready')
        row = self._agents(router, nc)['ag-b']
        self.assertEqual(row['readiness'], 'ready')
        self.assertIn('ask_peer', row['available_task_actions'])

    def test_listing_shares_only_common_project_ids_and_rejects_private_filter(self):
        path=self.runtime/'registry.json';data=json.loads(path.read_text());data['agents'][1]['projects'].append('private-project');path.write_text(json.dumps(data))
        router,nc=self._router();row=self._agents(router,nc)['ag-b'];self.assertEqual(row['project_ids'],['demo-project'])
        asyncio.run(router.ingress(FakeMsg('ag-a',self._body('list_agents',{'project_id':'private-project'}))))
        self.assertEqual(nc.published[-1][1]['code'],'FORBIDDEN')

    def test_an_invalid_readiness_is_rejected_without_touching_presence(self):
        router, _nc = self._router()
        self._heartbeat(router, 'whenever')
        self.assertNotIn('ag-b', router.presence)
        self.assertNotIn('ag-b', router.runtime_ready)

    def _submit_and_dispatch(self, router):
        with patch('app.router.allowed', return_value=True):
            row = router.tasks.submit('ag-a', 'ag-b', 'k1', 'ask_peer',
                                      {'question': 'q', 'caller': 'ag-a',
                                       'projects': ['demo-project']}, 600)
            router.presence = {'ag-b': time.time()}
            router.runtime_tasks = {'ag-b': ['ask_peer']}
            asyncio.run(router.dispatch_tasks())
            state = router.tasks.get(row['task_id'])['state']
            router.tasks.db.close()
            return state

    def test_a_not_ready_worker_is_not_dispatched_to(self):
        router, nc = self._router()
        router.runtime_ready = {'ag-b': 'starting'}
        self.assertEqual(self._submit_and_dispatch(router), 'queued')
        self.assertEqual([s for s, _b in nc.published if s.endswith('.task')], [])

    def test_a_ready_worker_is_dispatched_to(self):
        router, nc = self._router()
        router.runtime_ready = {'ag-b': 'ready'}
        self.assertEqual(self._submit_and_dispatch(router), 'dispatched')
        self.assertEqual([s for s, _b in nc.published if s.endswith('.task')], [f'{PREFIX}.agent.ag-b.task'])

    def test_targeted_maintenance_blocks_only_the_selected_worker(self):
        from app.maintenance import begin
        begin(['ag-b'], runtime=self.runtime)
        router, nc = self._router()
        router.runtime_ready = {'ag-b': 'ready'}
        self.assertEqual(self._submit_and_dispatch(router), 'queued')
        self.assertEqual([s for s, _b in nc.published if s.endswith('.task')], [])

    def test_other_workers_maintenance_does_not_block_this_target(self):
        from app.maintenance import begin
        begin(['ag-a'], runtime=self.runtime)
        router, nc = self._router()
        router.runtime_ready = {'ag-b': 'ready'}
        self.assertEqual(self._submit_and_dispatch(router), 'dispatched')


class MaintenanceDrainTests(unittest.TestCase):
    def test_maintenance_requested_reads_the_flag_file(self):
        from app import agent
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(agent.maintenance_requested(d))
            (Path(d) / 'maintenance').write_text('')
            self.assertTrue(agent.maintenance_requested(d))

    def test_apply_maintenance_is_idempotent_and_stops_consuming(self):
        from app import agent

        class Worker:
            draining = False

            def __init__(self):
                self.calls = 0

            def stop_consuming(self):
                self.calls += 1
                self.draining = True

        worker = Worker()
        self.assertTrue(agent.apply_maintenance(worker))
        self.assertTrue(worker.draining)
        self.assertFalse(agent.apply_maintenance(worker))
        self.assertEqual(worker.calls, 1)


if __name__ == '__main__':
    unittest.main()
