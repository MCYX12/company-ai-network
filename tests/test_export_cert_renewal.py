import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from scripts import bootstrap
from scripts.export_cert_renewal import export

ROOT = Path(__file__).resolve().parents[1]
CERT_DIR = ROOT / '.runtime'


class ExportCertRenewalTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.runtime = Path(tempfile.mkdtemp(prefix='test-cert-export-', dir=CERT_DIR))
        self.addCleanup(shutil.rmtree, self.runtime, ignore_errors=True)
        bootstrap.RUNTIME = self.runtime
        bootstrap.main()
        shutil.copy2(ROOT / 'tests/fixtures/registry.demo.json', self.runtime / 'registry.json')
        self.output = self.runtime / 'mac-certs.zip'

    def test_exports_only_requested_device_identities(self):
        export(self.runtime, 'mac-demo', self.output)
        with zipfile.ZipFile(self.output) as archive:
            names = set(archive.namelist())
        self.assertIn('certs/ca.crt', names)
        self.assertIn('certs/ag-b.crt', names)
        self.assertIn('certs/ag-b.key', names)
        self.assertIn('install-certs.command', names)
        self.assertIn('README.txt', names)
        self.assertNotIn('certs/ag-a.crt', names)
        self.assertNotIn('certs/ag-a.key', names)
        self.assertNotIn('certs/ag-c.key', names)
        installer = zipfile.ZipFile(self.output).read('install-certs.command').decode()
        self.assertIn('launchctl kickstart', installer)
        self.assertIn('history', installer)

    def test_generated_installer_updates_simulated_runtime(self):
        export(self.runtime, 'mac-demo', self.output)
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            with zipfile.ZipFile(self.output) as archive:
                archive.extractall(work)
            certs = work / 'CompanyAgent/.runtime/certs'
            certs.mkdir(parents=True)
            (certs / 'ag-b.crt').write_text('old-certificate')
            (certs / 'ag-b.key').write_text('old-key')
            environment = dict(os.environ, COMPANY_AGENT_RUNTIME=str(work / 'CompanyAgent'))
            completed = subprocess.run(['bash', str(work / 'install-certs.command')],
                                       env=environment, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((certs / 'ag-b.crt').read_text(), (work / 'certs/ag-b.crt').read_text())
            self.assertEqual((certs / 'ag-b.key').stat().st_mode & 0o777, 0o600)
            self.assertTrue(list((certs / 'history').glob('*/ag-b.crt')))
            self.assertTrue(list((certs / 'history').glob('*/ag-b.key')))

    def test_existing_output_and_unknown_device_are_rejected(self):
        self.output.write_bytes(b'x')
        with self.assertRaises(ValueError):
            export(self.runtime, 'mac-demo', self.output)
        with self.assertRaises(ValueError):
            export(self.runtime, 'missing-device', self.runtime / 'other.zip')


if __name__ == '__main__':
    unittest.main()
