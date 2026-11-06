"""Self-developed (non-built-in) agents: declared capabilities instead of a tool whitelist."""
import unittest
from pathlib import Path
from unittest.mock import patch

from app.adapters import describe, execute
from app.inventory import inspect_tool
from app.policy import allowed
from app.registry import PROFILES, capabilities, profile, validate_registry
from scripts.enroll_agent import build_parser, enrollment_row
from scripts import render_site


def agent(**kwargs):
    row = {'agent_id': 'mac-deepseek-01', 'device_id': 'mac-demo', 'employee_id': 'demo-b',
           'tool': 'deepseek-harness', 'projects': ['demo-project'], 'enabled': True}
    row.update(kwargs)
    return row


class RegistryTests(unittest.TestCase):
    def test_self_developed_tool_with_declared_capabilities(self):
        declared = ['get_status', 'ask_peer', 'get_project_progress']
        parsed = validate_registry({'schema_version': 1, 'agents': [agent(capabilities=declared)]})
        self.assertEqual(capabilities(parsed['mac-deepseek-01']), declared)

    def test_builtin_profiles_are_unchanged(self):
        self.assertEqual(capabilities({'tool': 'codex'}), PROFILES['worker'])
        self.assertEqual(capabilities({'tool': 'claude-code'}), PROFILES['worker'])
        self.assertEqual(capabilities({'tool': 'fixture'}), PROFILES['readonly'])
        self.assertEqual(capabilities({'tool': 'openclaw'}), [])

    def test_undeclared_self_developed_tool_has_no_capabilities(self):
        parsed = validate_registry({'schema_version': 1, 'agents': [agent()]})
        self.assertEqual(capabilities(parsed['mac-deepseek-01']), [])

    def test_unknown_capability_rejected(self):
        with self.assertRaises(ValueError):
            validate_registry({'schema_version': 1, 'agents': [agent(capabilities=['run_shell'])]})

    def test_duplicate_capability_rejected(self):
        with self.assertRaises(ValueError):
            validate_registry({'schema_version': 1, 'agents': [agent(capabilities=['get_status', 'get_status'])]})


class InventoryTests(unittest.TestCase):
    @patch('app.inventory.subprocess.run')
    def test_self_developed_tool_has_no_fixed_command(self, run):
        result = inspect_tool('deepseek-harness')
        self.assertEqual(result['adapter_status'], 'not_integrated')
        self.assertIsNone(result['executable'])
        run.assert_not_called()


class AdapterTests(unittest.TestCase):
    def test_describe_reports_declared_capability(self):
        described = describe(agent(capabilities=['get_status', 'ask_peer']))
        self.assertEqual(described['adapter_status'], 'status_only')
        self.assertEqual(described['capabilities'], ['get_status', 'ask_peer'])

    def test_builtin_worker_does_not_pretend_to_serve_a_self_developed_agent(self):
        code, data = execute(agent(capabilities=['get_status']), 'get_status', {})
        self.assertEqual(code, 'TOOL_UNAVAILABLE')
        self.assertEqual(data['hint'], 'SELF_DEVELOPED_AGENT_RUNS_ITS_OWN_WORKER')


class PolicyTests(unittest.TestCase):
    def registry(self, declared):
        return {'alice': {'agent_id': 'alice', 'device_id': 'd', 'employee_id': 'e', 'tool': 'fixture',
                          'projects': ['p'], 'enabled': True},
                'mac-deepseek-01': {'agent_id': 'mac-deepseek-01', 'device_id': 'd', 'employee_id': 'e',
                                    'tool': 'deepseek-harness', 'projects': ['p'], 'enabled': True,
                                    'capabilities': declared}}

    @patch('app.policy.approved', return_value=True)
    def test_declared_ask_peer_is_allowed(self, approved):
        self.assertTrue(allowed('alice', 'mac-deepseek-01', 'ask_peer',
                                {'question': 'q', 'caller': 'alice', 'projects': ['p']},
                                self.registry(['get_status', 'ask_peer'])))

    @patch('app.policy.approved', return_value=True)
    def test_tool_without_ask_peer_cannot_be_asked(self, approved):
        self.assertFalse(allowed('alice', 'mac-deepseek-01', 'ask_peer',
                                 {'question': 'q', 'caller': 'alice', 'projects': ['p']},
                                 self.registry(['get_status'])))
        approved.assert_not_called()


class EnrollmentTests(unittest.TestCase):
    def args(self, *extra):
        return build_parser().parse_args(['--runtime', '/tmp/does-not-need-to-exist',
                                          '--agent', 'mac-deepseek-01', '--device', 'mac-demo',
                                          '--employee', 'demo-b', '--tool', 'deepseek-harness',
                                          *extra, '--project', 'demo-project'])

    def test_explicit_capabilities_become_a_valid_row(self):
        row = enrollment_row(self.args('--capabilities', 'get_status,ask_peer'))
        self.assertEqual(row['tool'], 'deepseek-harness')
        self.assertEqual(row['capabilities'], ['get_status', 'ask_peer'])
        validate_registry({'schema_version': 1, 'agents': [row]})

    def test_profile_shortcut(self):
        self.assertEqual(enrollment_row(self.args('--profile', 'dialogue'))['capabilities'], profile('dialogue'))

    def test_builtin_tool_needs_no_capabilities_argument(self):
        row = enrollment_row(self.args('--tool', 'codex'))
        self.assertNotIn('capabilities', row)
        validate_registry({'schema_version': 1, 'agents': [row]})

    def test_invalid_capability_rejected(self):
        with self.assertRaises(ValueError):
            enrollment_row(self.args('--capabilities', 'run_shell'))

    def test_invalid_tool_id_rejected(self):
        args = self.args()
        args.tool = 'DeepSeek Harness'
        with self.assertRaises(ValueError):
            enrollment_row(args)


class RenderSiteTests(unittest.TestCase):
    def profile_for(self, declared):
        entry = {'agent_id': 'mac-deepseek-01', 'device_id': 'mac-demo', 'employee_id': 'demo-b',
                 'tool': 'deepseek-harness', 'projects': ['demo-project'], 'enabled': True}
        if declared is not None:
            entry['capabilities'] = declared
        return {'prefix': 'corp.dev.v1', 'server': {'nats_port': 14222}, 'agents': [entry]}

    def test_dialogue_agent_gets_worker_acl(self):
        nats = render_site.render_nats(self.profile_for(['get_status', 'ask_peer']), Path('/tmp/certs'))
        user = nats['accounts']['POC']['users'][0]
        self.assertIn('corp.dev.v1.agent.mac-deepseek-01.task', user['permissions']['subscribe']['allow'])

    def test_readonly_agent_stays_a_caller(self):
        nats = render_site.render_nats(self.profile_for(['get_status']), Path('/tmp/certs'))
        user = nats['accounts']['POC']['users'][0]
        self.assertEqual(user['permissions']['subscribe']['allow'], ['corp.dev.v1.client.mac-deepseek-01.response'])

    def test_dialogue_agent_gets_jetstream_acl(self):
        nats = render_site.render_nats(self.profile_for(['get_status', 'ask_peer']), Path('/tmp/certs'))
        user = nats['accounts']['POC']['users'][0]
        self.assertIn('$JS.API.>', user['permissions']['publish']['allow'])
        self.assertIn('corp.dev.v1.task.mac-deepseek-01', user['permissions']['subscribe']['allow'])

    def test_client_agent_gets_no_jetstream_acl(self):
        nats = render_site.render_nats(self.profile_for(['get_status']), Path('/tmp/certs'))
        user = nats['accounts']['POC']['users'][0]
        self.assertNotIn('$JS.API.>', user['permissions']['publish']['allow'])
        self.assertEqual([s for s in user['permissions']['subscribe']['allow'] if s.startswith('$JS')], [])

    def test_router_can_publish_the_durable_stream(self):
        nats = render_site.render_nats(self.profile_for(['get_status']), Path('/tmp/certs'))
        router = [u for u in nats['accounts']['POC']['users'] if u['user'] == 'router'][0]
        self.assertIn('corp.dev.v1.task.*', router['permissions']['publish']['allow'])
        self.assertIn('$JS.API.>', router['permissions']['publish']['allow'])
        self.assertTrue(nats['jetstream']['store_dir'].endswith('jetstream'))
        # A custom account must enable JetStream itself, not only via the global block.
        self.assertEqual(nats['accounts']['POC']['jetstream'], 'enabled')


if __name__ == '__main__':
    unittest.main()
