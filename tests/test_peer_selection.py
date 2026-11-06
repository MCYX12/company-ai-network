"""Target selection is data-driven: no hardcoded device or tool preference."""
import asyncio
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.peer_client import approval_granted, select_target, wait_task
from scripts.export_client_enrollment import export

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', ROOT / 'packaging/client/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def peer(agent_id, device_id='mac-demo', tool='claude-code', presence='online', actions=('ask_peer',)):
    return {'agent_id': agent_id, 'device_id': device_id, 'tool': tool,
            'presence': presence, 'available_task_actions': list(actions)}


class SelectTargetTests(unittest.TestCase):
    def test_explicit_target_wins(self):
        target, error = select_target([peer('a'), peer('b')], requested='b')
        self.assertEqual(target, 'b')
        self.assertIsNone(error)

    def test_unknown_explicit_target_is_not_silently_replaced(self):
        target, error = select_target([peer('a')], requested='zzz')
        self.assertIsNone(target)
        self.assertEqual(error['code'], 'NO_ELIGIBLE_AGENT')
        self.assertEqual(error['data']['target'], 'zzz')

    def test_configured_default_is_used(self):
        target, error = select_target([peer('a'), peer('b')], default='b')
        self.assertEqual(target, 'b')
        self.assertIsNone(error)

    def test_ineligible_configured_default_does_not_fall_back(self):
        target, error = select_target([peer('a')], default='b')
        self.assertIsNone(target)
        self.assertEqual(error['code'], 'NO_ELIGIBLE_AGENT')
        self.assertEqual(error['data']['default_target'], 'b')

    def test_single_peer_needs_no_configuration(self):
        target, error = select_target([peer('only', tool='deepseek-harness')])
        self.assertEqual(target, 'only')
        self.assertIsNone(error)

    def test_multiple_peers_require_an_explicit_choice(self):
        target, error = select_target([peer('b'), peer('a', tool='deepseek-harness')])
        self.assertIsNone(target)
        self.assertEqual(error['code'], 'CLARIFY_TARGET')
        self.assertEqual([c['agent_id'] for c in error['data']['candidates']], ['a', 'b'])

    def test_offline_or_non_dialogue_peers_are_excluded(self):
        target, error = select_target([peer('offline', presence='offline'),
                                       peer('status-only', actions=('get_status',))])
        self.assertIsNone(target)
        self.assertEqual(error['code'], 'NO_ELIGIBLE_AGENT')


class ReadinessSelectionTests(unittest.TestCase):
    def test_a_present_but_starting_target_reports_its_real_state(self):
        candidates = [dict(peer('b'), readiness='starting', available_task_actions=[])]
        target, error = select_target(candidates, requested='b')
        self.assertIsNone(target)
        self.assertEqual(error['code'], 'TARGET_NOT_READY')
        self.assertEqual(error['data']['readiness'], 'starting')
        self.assertTrue(error['data']['retryable'])

    def test_a_draining_target_is_not_waited_on(self):
        candidates = [dict(peer('b'), readiness='draining', available_task_actions=[])]
        _target, error = select_target(candidates, requested='b')
        self.assertEqual(error['code'], 'TARGET_NOT_READY')
        self.assertFalse(error['data']['retryable'])

    def test_a_peer_without_a_readiness_field_stays_eligible(self):
        candidates = [peer('older')]
        target, error = select_target(candidates)
        self.assertEqual(target, 'older')
        self.assertIsNone(error)


class WaitTaskTests(unittest.TestCase):
    def test_terminal_result_stops_polling_immediately(self):
        calls = []

        async def send(action, payload):
            calls.append(payload['task_id'])
            return {'code': 'OK', 'data': {'state': 'succeeded' if len(calls) >= 2 else 'running'}}

        result = asyncio.run(wait_task(send, 't1', 10, poll=0.01))
        self.assertEqual(result['data']['state'], 'succeeded')
        self.assertEqual(len(calls), 2)

    def test_transient_transport_errors_do_not_end_the_wait(self):
        calls={'n':0}

        async def send(action, payload):
            calls['n']+=1
            if calls['n']<3:
                raise TimeoutError('router or worker restarting')
            return {'code': 'OK', 'data': {'state': 'succeeded'}}

        result = asyncio.run(wait_task(send, 't1', 10, poll=0.01))
        self.assertEqual(result['data']['state'], 'succeeded')
        self.assertEqual(calls['n'], 3)

    def test_the_wait_window_is_a_monotonic_deadline_not_a_sleep_count(self):
        now = [0.0]

        async def send(action, payload):
            now[0] += 0.4          # every query itself takes time
            return {'code': 'OK', 'data': {'state': 'running'}}

        async def sleep(seconds):
            now[0] += seconds

        result = asyncio.run(wait_task(send, 't1', 1.0, poll=0.5,
                                       clock=lambda: now[0], sleep=sleep))
        self.assertEqual(result['code'], 'PENDING')
        # 0.4s query + 0.5s sleep + 0.4s query = 1.3s, then the deadline is already past.
        self.assertLess(now[0], 2.0)


class ApprovalPollingTests(unittest.TestCase):
    def test_approval_granted_matches_the_request(self):
        rows = [{'id': 'a', 'state': 'pending'}, {'id': 'b', 'state': 'approved'}]
        self.assertTrue(approval_granted(rows, 'b'))
        self.assertFalse(approval_granted(rows, 'a'))
        self.assertFalse(approval_granted(rows, 'missing'))


class EnrollmentMetadataTests(unittest.TestCase):
    def runtime(self, folder):
        runtime = Path(folder)
        (runtime / 'certs').mkdir()
        (runtime / 'registry.json').write_text(json.dumps({'schema_version': 1, 'agents': [
            {'agent_id': 'alice', 'device_id': 'd', 'employee_id': 'e',
             'tool': 'fixture', 'projects': ['p'], 'enabled': True}]}))
        (runtime / 'endpoint.txt').write_text('tls://127.0.0.1:14222')
        for name in ['ca.crt', 'alice.crt', 'alice.key']:
            (runtime / 'certs' / name).write_text('x')
        return runtime

    def package(self, folder, metadata):
        package = Path(folder) / 'identity.zip'
        with zipfile.ZipFile(package, 'w') as z:
            z.writestr('enrollment.json', json.dumps(metadata))
            for name in ['certs/ca.crt', 'certs/alice.crt', 'certs/alice.key']:
                z.writestr(name, 'x')
        return package

    def test_default_target_is_optional_and_recorded(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = self.runtime(folder)
            export(runtime, 'alice', runtime / 'pkg.zip', default_target='mac-deepseek-01')
            with zipfile.ZipFile(runtime / 'pkg.zip') as z:
                self.assertEqual(json.loads(z.read('enrollment.json'))['default_target'], 'mac-deepseek-01')
            # No default target at all stays byte-compatible with older packages.
            export(runtime, 'alice', runtime / 'plain.zip')
            with zipfile.ZipFile(runtime / 'plain.zip') as z:
                self.assertNotIn('default_target', json.loads(z.read('enrollment.json')))

    def test_invalid_default_target_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = self.runtime(folder)
            with self.assertRaises(ValueError):
                export(runtime, 'alice', runtime / 'pkg.zip', default_target='Mac DeepSeek')

    def test_installer_accepts_optional_default_target(self):
        with tempfile.TemporaryDirectory() as folder:
            package = self.package(folder, dict(version=1, identity='alice',
                                                endpoint='tls://localhost:14222',
                                                default_target='mac-deepseek-01'))
            config, files = installer.credentials(package)
            self.assertEqual(config['default_target'], 'mac-deepseek-01')
            self.assertEqual(set(files), {'certs/ca.crt', 'certs/alice.crt', 'certs/alice.key'})

    def test_installer_rejects_invalid_default_target(self):
        with tempfile.TemporaryDirectory() as folder:
            package = self.package(folder, dict(version=1, identity='alice',
                                                endpoint='tls://localhost:14222',
                                                default_target='Mac DeepSeek'))
            with self.assertRaises(ValueError):
                installer.credentials(package)


if __name__ == '__main__':
    unittest.main()
