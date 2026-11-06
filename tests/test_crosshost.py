"""The cross-host gate must reject a same-host topology and accept a real one."""
import unittest

from app import crosshost

SAME_HOST = {
    'r1': {'machine_id': 'M1', 'runtime_dir': '/a', 'addresses': ['10.0.0.21'], 'authz_shared': True},
    'r2': {'machine_id': 'M1', 'runtime_dir': '/b', 'addresses': ['10.0.0.22'], 'authz_shared': True},
    'n1': {'machine_id': 'M1', 'runtime_dir': '/c', 'addresses': ['10.0.0.11']},
    'n2': {'machine_id': 'M1', 'runtime_dir': '/d', 'addresses': ['10.0.0.12']},
    'n3': {'machine_id': 'M1', 'runtime_dir': '/e', 'addresses': ['10.0.0.13']},
    'w1': {'machine_id': 'M1', 'runtime_dir': '/f', 'addresses': ['10.0.0.31']},
}
REAL = {
    'r1': {'machine_id': 'R1', 'runtime_dir': '/a', 'addresses': ['10.0.0.21'], 'authz_shared': True},
    'r2': {'machine_id': 'R2', 'runtime_dir': '/b', 'addresses': ['10.0.0.22'], 'authz_shared': True},
    'n1': {'machine_id': 'N1', 'runtime_dir': '/c', 'addresses': ['10.0.0.11']},
    'n2': {'machine_id': 'N2', 'runtime_dir': '/d', 'addresses': ['10.0.0.12']},
    'n3': {'machine_id': 'N3', 'runtime_dir': '/e', 'addresses': ['10.0.0.13']},
    'w1': {'machine_id': 'W1', 'runtime_dir': '/f', 'addresses': ['10.0.0.31']},
}


class CrossHostTests(unittest.TestCase):
    def test_same_machine_topology_is_rejected(self):
        checks = crosshost.assess(SAME_HOST, nats=['n1', 'n2', 'n3'], routers=['r1', 'r2'], workers=['w1'])
        self.assertTrue(checks)
        self.assertTrue(all(not c['ok'] for c in checks if '物理机' in c['check'] or '不同机' in c['check']))

    def test_real_topology_passes(self):
        checks = crosshost.assess(REAL, nats=['n1', 'n2', 'n3'], routers=['r1', 'r2'], workers=['w1'])
        self.assertEqual(crosshost.summarize(checks)['failed'], 0, checks)

    def test_loopback_address_and_missing_authz_fail(self):
        facts = {k: dict(v) for k, v in REAL.items()}
        facts['r1']['addresses'] = ['127.0.0.1']
        checks = crosshost.assess(facts, nats=['n1', 'n2', 'n3'], routers=['r1', 'r2'], workers=['w1'])
        self.assertFalse([c for c in checks if '回环' in c['check']][0]['ok'])

    def test_machine_identity_prefers_machine_id(self):
        self.assertEqual(crosshost.machine_identity({'machine_id': 'M', 'hostname': 'h'}), 'M')
        self.assertEqual(crosshost.machine_identity({'hostname': 'h'}), 'h')
        self.assertIsNone(crosshost.machine_identity({}))


if __name__ == '__main__':
    unittest.main()
