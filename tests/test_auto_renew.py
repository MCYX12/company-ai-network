import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts import bootstrap
from scripts.auto_renew import auto_renew, expiring

ROOT = Path(__file__).resolve().parents[1]
CERT_DIR = ROOT / '.runtime'


class AutoRenewTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.runtime = Path(tempfile.mkdtemp(prefix='test-autorenew-', dir=CERT_DIR))
        self.addCleanup(shutil.rmtree, self.runtime, ignore_errors=True)
        bootstrap.RUNTIME = self.runtime
        bootstrap.main()
        (self.runtime / 'certs/ENTERPRISE_CA.json').write_text('{"format":1}')

    def test_expiring_respects_threshold(self):
        self.assertTrue(expiring(self.runtime, 30))
        self.assertEqual(expiring(self.runtime, 0.1), [])

    def test_requires_enterprise_marker(self):
        (self.runtime / 'certs/ENTERPRISE_CA.json').unlink()
        with self.assertRaises(ValueError):
            auto_renew(self.runtime, 30)

    def test_not_due_writes_status_without_renewing(self):
        with patch('scripts.auto_renew.renew_runtime') as renewal:
            report = auto_renew(self.runtime, 0.1, apply=True)
        self.assertFalse(report['renewed'])
        self.assertIn('none', report['action'])
        renewal.assert_not_called()
        saved = json.loads((self.runtime / 'health/renewal.json').read_text())
        self.assertEqual(saved['expiring'], [])

    def test_dry_run_when_due(self):
        with patch('scripts.auto_renew.renew_runtime') as renewal:
            report = auto_renew(self.runtime, 30, apply=False)
        self.assertFalse(report['renewed'])
        self.assertIn('dry run', report['action'])
        renewal.assert_not_called()

    def test_due_applies_renewal_restart_and_mac_bundle(self):
        commands = []
        with (
            patch('scripts.auto_renew.expiring', return_value=[{'name': 'ag-a', 'seconds_remaining': 1}]),
            patch('scripts.auto_renew.renew_runtime', return_value={'applied': True, 'backup': 'x'}) as renewal,
            patch('scripts.auto_renew.subprocess.run', side_effect=lambda cmd, **kw: commands.append(cmd)),
            patch('scripts.export_cert_renewal.export') as export,
        ):
            report = auto_renew(self.runtime, 30, apply=True, restart=True, device='mac-demo',
                                bundle_dir=self.runtime / 'bundle')
        self.assertTrue(report['renewed'])
        self.assertTrue(report['restarted'])
        renewal.assert_called_once()
        export.assert_called_once()
        self.assertTrue(report['mac_bundle'].endswith('.zip'))
        self.assertIn(['systemctl', '--user', 'restart', 'company-agent-stack'], commands)

    def test_failure_alerts_and_raises(self):
        with patch('scripts.auto_renew.renew_runtime', side_effect=ValueError('boom')):
            with self.assertRaises(ValueError):
                auto_renew(self.runtime, 30, apply=True)
        alerts = (self.runtime / 'health/alerts.jsonl').read_text().strip().splitlines()
        self.assertEqual(len(alerts), 1)
        self.assertIn('renewal failed', json.loads(alerts[0])['message'])

    def test_mac_push_failure_alerts_but_keeps_renewal(self):
        with (
            patch('scripts.auto_renew.expiring', return_value=[{'name': 'ag-b', 'seconds_remaining': 1}]),
            patch('scripts.auto_renew.renew_runtime', return_value={'applied': True}),
            patch('scripts.export_cert_renewal.export'),
            patch('scripts.auto_renew._push_mac', side_effect=RuntimeError('ssh down')),
            patch('scripts.auto_renew.subprocess.run'),
        ):
            report = auto_renew(self.runtime, 30, apply=True, device='mac-demo',
                                bundle_dir=self.runtime / 'bundle', mac_host='mac@host')
        self.assertTrue(report['renewed'])
        self.assertIn('push failed', report['mac_distribution'])
        self.assertTrue((self.runtime / 'health/alerts.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
