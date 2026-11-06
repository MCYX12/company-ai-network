import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.task_store import TaskStore
from scripts import alerts

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


class AlertTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.runtime = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _runtime_with_data(self, health):
        store = TaskStore(self.runtime / 'tasks.sqlite3')
        task = store.submit('ag-a', 'peer-x', 'k', 'ask_peer', {'question': 'x'}, 600)
        with sqlite3.connect(self.runtime / 'tasks.sqlite3') as db:
            db.execute("INSERT INTO task_audit(task_id,event,timestamp) VALUES(?,'accepted',100)", (task['task_id'],))
            db.execute("INSERT INTO task_audit(task_id,event,timestamp) VALUES(?,'dispatched',260)", (task['task_id'],))
            db.execute("INSERT INTO task_audit(task_id,event,timestamp) VALUES(?,'succeeded',1000)", (task['task_id'],))
            for i in range(3):
                db.execute("INSERT INTO task_audit(task_id,event,timestamp) VALUES(?,'FORBIDDEN',?)", ('d%d' % i, 1100 + i))
        (self.runtime / 'project-records').mkdir()
        (self.runtime / 'project-records/demo.json').write_text('{"updated_at": "2020-01-01T00:00:00+00:00"}')
        (self.runtime / 'health').mkdir()
        (self.runtime / 'health/latest.json').write_text(json.dumps(health))

    def test_alerts_fire_above_thresholds(self):
        self._runtime_with_data({'status': 'fail', 'checks': [
            {'check': 'disk_space', 'status': 'ok', 'detail': {'free_gib': 1.0}},
            {'check': 'task_ledger', 'status': 'ok', 'detail': {'dead_letter_delivery_failed': 2}},
            {'check': 'authorized_directory', 'status': 'fail', 'detail': 'x'}], 'renewal': {'failures': 1}})
        report = alerts.evaluate(self.runtime, limits={
            'queue_seconds_max': 60, 'run_seconds_max': 600, 'auth_denied_total': 0,
            'project_age_seconds_max': 100, 'renewal_failures': 0, 'dead_letter_max': 0,
            'disk_free_gib_min': 2})
        rules = {a['rule'] for a in report['alerts']}
        self.assertEqual(report['status'], 'alert')
        self.assertTrue({'queue_seconds_max', 'run_seconds_max', 'auth_denied_total',
                         'project_age_seconds_max', 'renewal_failures', 'dead_letter',
                         'disk_free_gib', 'authorized_directory'} <= rules)

    def test_dead_letter_prefers_the_recent_window(self):
        self._runtime_with_data({'status': 'ok', 'checks': [
            {'check': 'disk_space', 'status': 'ok', 'detail': {'free_gib': 100.0}},
            {'check': 'task_ledger', 'status': 'ok', 'detail': {'dead_letter_delivery_failed': 9,
                                                                'dead_letter_delivery_failed_24h': 0,
                                                                'dead_letter_by_target_24h': {}}},
            {'check': 'authorized_directory', 'status': 'ok', 'detail': 'ok'}]})
        report = alerts.evaluate(self.runtime, limits={
            'queue_seconds_max': 1000, 'run_seconds_max': 10000, 'auth_denied_total': 100,
            'project_age_seconds_max': 10 ** 9, 'renewal_failures': 10, 'dead_letter_max': 0,
            'disk_free_gib_min': 1})
        self.assertEqual([a['rule'] for a in report['alerts']], [])
        self.assertEqual(report['measured']['dead_letter'], 0)
        self.assertEqual(report['measured']['dead_letter_by_target'], {})

    def test_no_alerts_when_healthy(self):
        self._runtime_with_data({'status': 'ok', 'checks': [
            {'check': 'disk_space', 'status': 'ok', 'detail': {'free_gib': 100.0}},
            {'check': 'task_ledger', 'status': 'ok', 'detail': {'dead_letter_delivery_failed': 0}},
            {'check': 'authorized_directory', 'status': 'ok', 'detail': 'ok'}]})
        report = alerts.evaluate(self.runtime, limits={
            'queue_seconds_max': 1000, 'run_seconds_max': 10000, 'auth_denied_total': 100,
            'project_age_seconds_max': 10 ** 9, 'renewal_failures': 10, 'dead_letter_max': 10,
            'disk_free_gib_min': 1})
        self.assertEqual(report['alerts'], [])
        self.assertEqual(report['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
