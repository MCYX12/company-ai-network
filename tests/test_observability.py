import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.task_store import WorkerStore
from scripts import service_manifests
from scripts.healthcheck_alert import run
from scripts.rotate_logs import rotate


class RotateLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_large_log_is_copied_and_truncated(self):
        path = self.runtime / 'router.log'
        path.write_bytes(b'x' * 2048)
        path.chmod(0o600)
        results = rotate(self.runtime, max_bytes=1024, keep=3)
        self.assertEqual([entry['file'] for entry in results], ['router.log'])
        self.assertTrue(results[0]['rotated'])
        self.assertEqual(path.read_bytes(), b'')
        self.assertEqual((self.runtime / 'router.log.1').read_bytes(), b'x' * 2048)
        self.assertEqual((self.runtime / 'router.log.1').stat().st_mode & 0o777, 0o600)

    def test_small_log_untouched_and_dry_run_writes_nothing(self):
        path = self.runtime / 'ag-a.log'
        path.write_bytes(b'y' * 100)
        self.assertEqual(rotate(self.runtime, max_bytes=1024), [])
        path.write_bytes(b'y' * 2048)
        results = rotate(self.runtime, max_bytes=1024, dry_run=True)
        self.assertFalse(results[0]['rotated'])
        self.assertEqual(path.read_bytes(), b'y' * 2048)
        self.assertFalse((self.runtime / 'ag-a.log.1').exists())

    def test_keep_limit_prunes_oldest(self):
        path = self.runtime / 'nats.log'
        for index in range(1, 4):
            (self.runtime / f'nats.log.{index}').write_bytes(str(index).encode())
        path.write_bytes(b'z' * 2048)
        rotate(self.runtime, max_bytes=1024, keep=3)
        self.assertEqual((self.runtime / 'nats.log.1').read_bytes(), b'z' * 2048)
        self.assertEqual((self.runtime / 'nats.log.2').read_bytes(), b'1')
        self.assertEqual((self.runtime / 'nats.log.3').read_bytes(), b'2')
        self.assertFalse((self.runtime / 'nats.log.4').exists())


class HealthcheckAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ok_report_writes_state_without_alert(self):
        report = {'status': 'ok', 'checks': [{'check': 'disk_space', 'status': 'ok'}]}
        with patch('scripts.healthcheck_alert.check', new=AsyncMock(return_value=report)):
            result = run('ag-a', self.runtime)
        self.assertEqual(result['status'], 'ok')
        saved = json.loads((self.runtime / 'health/latest.json').read_text())
        self.assertEqual(saved['checks'][0]['check'], 'disk_space')
        self.assertFalse((self.runtime / 'health/alerts.jsonl').exists())

    def test_warn_report_appends_alert(self):
        report = {'status': 'warn', 'checks': [{'check': 'certificate:ca.crt', 'status': 'warn'}]}
        with patch('scripts.healthcheck_alert.check', new=AsyncMock(return_value=report)), \
             patch('scripts.healthcheck_alert.shutil.which', return_value=None):
            run('ag-a', self.runtime)
        alerts = (self.runtime / 'health/alerts.jsonl').read_text().strip().splitlines()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(json.loads(alerts[0])['status'], 'warn')


class WorkerOutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = WorkerStore(Path(self.tmp.name) / 'worker-x.sqlite3')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_pending_reports_unacked_backlog(self):
        envelope = {'task_id': 't1', 'target': 'x', 'operation': 'ask_peer', 'payload': {}, 'deadline': 9e9}
        self.assertTrue(self.store.claim(envelope))
        self.store.finish('t1', {'code': 'OK', 'data': {'a': 1}})
        self.assertEqual(self.store.pending()['unacked'], 1)
        self.assertIsNotNone(self.store.pending()['oldest_updated_at'])
        self.store.acknowledge('t1')
        self.assertEqual(self.store.pending()['unacked'], 0)

    def test_legacy_work_table_migrates(self):
        import sqlite3
        path = Path(self.tmp.name) / 'legacy-worker.sqlite3'
        legacy = sqlite3.connect(path)
        legacy.execute('CREATE TABLE work(task_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,state TEXT NOT NULL,result TEXT,acked INTEGER NOT NULL DEFAULT 0)')
        legacy.commit()
        legacy.close()
        migrated = WorkerStore(path)
        columns = {row['name'] for row in migrated.db.execute('PRAGMA table_info(work)')}
        self.assertIn('updated_at', columns)
        migrated.db.close()


class ServiceManifestTests(unittest.TestCase):
    def test_ubuntu_defines_healthcheck_service_and_timer(self):
        definitions = service_manifests.render('ubuntu', Path('/opt/company-agent'))
        self.assertIn('company-agent-healthcheck.service', definitions)
        timer = definitions['company-agent-healthcheck.timer'].decode()
        self.assertIn('OnUnitActiveSec=5min', timer)
        self.assertIn('WantedBy=timers.target', timer)
        service = definitions['company-agent-healthcheck.service'].decode()
        self.assertIn('run_healthcheck.sh', service)

    def test_mac_defines_healthcheck_agent(self):
        definitions = service_manifests.render('mac', Path('/Users/mac/CompanyAgent'))
        self.assertIn('com.companyagent.healthcheck.plist', definitions)
        text = definitions['com.companyagent.healthcheck.plist'].decode()
        self.assertIn('StartInterval', text)
        self.assertIn('run_healthcheck.sh', text)


if __name__ == '__main__':
    unittest.main()
