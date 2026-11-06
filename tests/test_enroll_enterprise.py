import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os

from scripts import bootstrap, ca_tool
from scripts.enroll_agent import enroll

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


class EnterpriseEnrollmentTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.runtime = Path(self.tmp.name)
        bootstrap.RUNTIME = self.runtime
        bootstrap.main()
        self.ca = self.runtime / 'enterprise-ca'
        ca_tool.init_root(self.ca, days=30)
        ca_tool.init_intermediate(self.ca, days=30)
        ca_tool.issue(self.ca, 'ocsp-signer', 'ocsp', 30, 'http://127.0.0.1:2560')
        (self.runtime / 'certs/ENTERPRISE_CA.json').write_text(json.dumps({'format': 1, 'ca_dir': str(self.ca)}))
        (self.runtime / 'endpoint.txt').write_text('tls://localhost:14222\n')

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self, agent='pc-dev-01'):
        return {'agent_id': agent, 'device_id': agent, 'employee_id': 'emp-1001', 'tool': 'codex',
                'projects': ['demo-project'], 'enabled': True}

    def test_shared_enrollment_and_export_use_authority(self):
        from scripts.export_client_enrollment import export
        shared=self.runtime/'authority';shared.mkdir()
        (shared/'registry.json').write_text('{"schema_version":1,"agents":[]}')
        local_before=(self.runtime/'registry.json').read_bytes()
        with patch.dict(os.environ, {'POC_AUTHZ_RUNTIME':str(shared)}):
            enroll(self.runtime,self._row('pc-shared-01'))
            output=self.runtime/'shared-enrollment.zip'
            export(self.runtime,'pc-shared-01',output)
        self.assertEqual(json.loads((shared/'registry.json').read_text())['agents'][0]['agent_id'],'pc-shared-01')
        self.assertEqual((self.runtime/'registry.json').read_bytes(),local_before)
        self.assertTrue(output.is_file())
        self.assertTrue((self.runtime/'certs/pc-shared-01.key').exists())

    def test_device_is_issued_from_the_enterprise_ca(self):
        enroll(self.runtime, self._row())
        registry = json.loads((self.runtime / 'registry.json').read_text())
        self.assertIn('pc-dev-01', {a['agent_id'] for a in registry['agents']})
        config = json.loads((self.runtime / 'nats.conf').read_text())
        self.assertIn('pc-dev-01', [u.get('user') for u in config['accounts']['POC']['users']])
        cert = self.runtime / 'certs/pc-dev-01.crt'
        self.assertIn('BEGIN CERTIFICATE', cert.read_text())
        # chain file contains leaf + intermediate
        self.assertEqual(cert.read_text().count('BEGIN CERTIFICATE'), 2)
        self.assertIn('OK', ca_tool.verify_chain(self.ca, 'pc-dev-01'))

    def test_bundle_is_exported(self):
        from scripts.export_client_enrollment import export
        enroll(self.runtime, self._row('pc-fin-01'))
        bundle = self.runtime / 'pc-fin-01-enrollment.zip'
        export(self.runtime, 'pc-fin-01', bundle)
        self.assertTrue(bundle.exists())
        self.assertGreater(bundle.stat().st_size, 0)


if __name__ == '__main__':
    unittest.main()
