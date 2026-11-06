import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import site
from app.registry import validate_registry
from scripts import render_site


class SiteProfileTests(unittest.TestCase):
    def test_defaults_match_current_single_site(self):
        profile = site.load()
        self.assertEqual(profile['prefix'], 'corp.dev.v1')
        self.assertEqual(profile['defaults'], {'caller': 'ag-a', 'target': 'ag-b', 'project': 'company-ai-network'})

    def test_poc_site_overrides_and_merges(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'site.json'
            path.write_text(json.dumps({'prefix': 'acme.dev.v1', 'server': {'nats_port': 15000}, 'defaults': {'caller': 'pc-1'}}))
            with patch.dict(os.environ, {'POC_SITE': str(path)}):
                profile = site.load()
                self.assertEqual(profile['prefix'], 'acme.dev.v1')
                self.assertEqual(profile['server']['nats_port'], 15000)
                self.assertEqual(profile['server']['ocsp_port'], 2560)
                self.assertEqual(profile['defaults']['caller'], 'pc-1')
                self.assertEqual(profile['defaults']['target'], 'ag-b')

    def test_validate_reports_problems(self):
        bad = {'prefix': 'bad prefix', 'server': {'nats_port': 0, 'ocsp_port': 70000, 'dashboard_port': 8787},
               'agents': [{'agent_id': 'a', 'device_id': 'a', 'employee_id': 'a', 'tool': 'fixture', 'projects': []},
                          {'agent_id': 'a', 'device_id': 'b', 'employee_id': 'b', 'tool': 'fixture', 'projects': ['p']}]}
        problems = site.validate(bad)
        self.assertTrue(any('prefix' in p for p in problems))
        self.assertTrue(any('nats_port' in p for p in problems))
        self.assertTrue(any('duplicate' in p for p in problems))
        self.assertTrue(any('projects' in p for p in problems))

    def test_valid_example_has_no_problems(self):
        profile = json.loads((Path(__file__).resolve().parents[1] / 'config/site.example.json').read_text())
        self.assertEqual(site.validate({**site.DEFAULTS, **profile}), [])


class RenderSiteTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((Path(__file__).resolve().parents[1] / 'config/site.example.json').read_text())

    def test_registry_is_valid_and_complete(self):
        registry = render_site.render_registry(self.profile)
        validate_registry(registry)
        self.assertEqual({a['agent_id'] for a in registry['agents']}, {'pc-dev-01', 'pc-hr-01', 'pc-fin-01'})

    def test_nats_users_and_acls_follow_the_prefix(self):
        nats = render_site.render_nats(self.profile, Path('/tmp/certs'))
        users = {u['user']: u for u in nats['accounts']['POC']['users']}
        self.assertIn('router', users)
        self.assertIn('acme.dev.v1.agent.pc-dev-01.task', users['pc-dev-01']['permissions']['subscribe']['allow'])
        self.assertIn('acme.dev.v1.agent.pc-dev-01.result', users['pc-dev-01']['permissions']['publish']['allow'])
        self.assertNotIn('heartbeat', json.dumps(users['pc-hr-01']))
        self.assertIn('acme.dev.v1.ingress.pc-hr-01.request', users['pc-hr-01']['permissions']['publish']['allow'])

    def test_build_and_enroll_commands(self):
        out = render_site.build(self.profile, certs_dir=Path('/tmp/certs'))
        self.assertEqual(len(out['enroll']), 3)
        self.assertEqual(out['nats']['tls']['ca_file'], '/tmp/certs/ca.crt')
        with self.assertRaises(ValueError):
            render_site.build({'prefix': 'bad prefix', 'agents': []})


if __name__ == '__main__':
    unittest.main()
