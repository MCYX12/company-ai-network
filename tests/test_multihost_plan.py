"""Pure multi-host deployment planner: host validation and rendered per-host files."""
import json
import unittest

from scripts.multihost_plan import render_plan, validate_hosts

PROFILE = {
    'prefix': 'corp.dev.v1',
    'server': {'nats_port': 14222},
    'projects': [{'id': 'project-alpha', 'name': 'Alpha'}],
    'defaults': {'caller': 'pc-dev-01', 'target': 'pc-dev-02', 'project': 'project-alpha'},
    'agents': [
        {'agent_id': 'pc-dev-01', 'device_id': 'pc-dev-01', 'employee_id': 'emp-1001',
         'tool': 'claude-code', 'projects': ['project-alpha']},
        {'agent_id': 'pc-dev-02', 'device_id': 'pc-dev-02', 'employee_id': 'emp-1002',
         'tool': 'claude-code', 'projects': ['project-alpha']},
    ],
}

HOSTS = [
    {'name': 'n1', 'role': 'nats', 'address': '10.0.0.11'},
    {'name': 'n2', 'role': 'nats', 'address': '10.0.0.12'},
    {'name': 'n3', 'role': 'nats', 'address': '10.0.0.13'},
    {'name': 'r1', 'role': 'router', 'address': '10.0.0.21'},
    {'name': 'r2', 'role': 'router', 'address': '10.0.0.22'},
    {'name': 'w1', 'role': 'worker', 'address': '10.0.0.31', 'agent_id': 'pc-dev-01'},
]


class ValidateTests(unittest.TestCase):
    def test_valid_topology(self):
        self.assertTrue(all(c['ok'] for c in validate_hosts(HOSTS)))

    def test_two_nats_hosts_rejected(self):
        checks = validate_hosts(HOSTS[:2] + HOSTS[3:])
        self.assertFalse([c for c in checks if 'R3' in c['check']][0]['ok'])

    def test_missing_agent_id_rejected(self):
        hosts = list(HOSTS)
        hosts[-1] = {'name': 'w1', 'role': 'worker', 'address': '10.0.0.31'}
        self.assertFalse([c for c in validate_hosts(hosts) if 'agent_id' in c['check']][0]['ok'])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.plan = render_plan(PROFILE, HOSTS)

    def test_topology_and_checks(self):
        self.assertEqual(self.plan['topology']['nats'], ['n1', 'n2', 'n3'])
        self.assertEqual(self.plan['topology']['routers'], ['r1', 'r2'])
        self.assertTrue(all(c['ok'] for c in self.plan['checks']))

    def test_registry_shared(self):
        registry = json.loads(self.plan['files']['registry.json'])
        self.assertEqual(registry['agents'][0]['agent_id'], 'pc-dev-01')

    def test_each_nats_node_has_cluster_routes_and_jetstream(self):
        conf = json.loads(self.plan['files']['nats/n1/nats.conf'])
        self.assertEqual(conf['server_name'], 'n1')
        self.assertEqual(len(conf['cluster']['routes']), 2)
        self.assertTrue(conf['jetstream']['store_dir'].endswith('jetstream'))
        self.assertEqual(conf['accounts']['POC']['jetstream'], 'enabled')

    def test_router_env_enables_ha(self):
        env = self.plan['files']['router/r1/env']
        self.assertIn('POC_HA=1', env)
        self.assertIn('POC_JETSTREAM_REPLICAS=3', env)
        self.assertIn('tls://10.0.0.11:14222', env)

    def test_worker_env_enables_ha_and_replicated_stream(self):
        # The built-in worker only validates the shared KV dispatch lease with POC_HA=1, so the
        # generated worker environment must carry it exactly like the Router's (assessment P1).
        env = self.plan['files']['worker/w1/env']
        self.assertIn('POC_HA=1', env)
        self.assertIn('POC_JETSTREAM_REPLICAS=3', env)
        self.assertIn('tls://10.0.0.11:14222', env)

    def test_units_and_enrollment(self):
        router_unit = self.plan['files']['router/r1/company-agent-router.service']
        self.assertIn('-m app.router', router_unit)
        worker_unit = self.plan['files']['worker/w1/company-agent.service']
        self.assertIn('-m app.agent pc-dev-01', worker_unit)
        self.assertIn('pc-dev-01', self.plan['files']['enroll.txt'])

    def test_invalid_host_plan_rejected(self):
        with self.assertRaises(ValueError):
            render_plan(PROFILE, HOSTS[:2] + HOSTS[3:])


if __name__ == '__main__':
    unittest.main()
