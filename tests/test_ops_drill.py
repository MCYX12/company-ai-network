"""The local operations drill covers alerts, off-site anchor, retention and RTO/RPO targets."""
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from scripts import ops_drill


def make_db(path, table):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE %s (id INTEGER PRIMARY KEY)' % table)
    db.execute('INSERT INTO %s DEFAULT VALUES' % table)
    db.commit(); db.close()


class OpsDrillTests(unittest.TestCase):
    def test_all_local_operations_checks_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            runtime = folder / 'runtime'; runtime.mkdir()
            (runtime / 'certs').mkdir()
            db = sqlite3.connect(runtime / 'tasks.sqlite3')
            db.execute('CREATE TABLE tasks (task_id TEXT PRIMARY KEY, state TEXT, created_at REAL, result TEXT)')
            db.execute("INSERT INTO tasks VALUES('t1','succeeded',1.0,NULL)")
            db.commit(); db.close()
            make_db(runtime / 'roster.sqlite3', 'employment')
            db = sqlite3.connect(runtime / 'accounts.sqlite3')
            for table in ('members', 'sessions', 'login_attempts'):
                db.execute('CREATE TABLE %s (id INTEGER PRIMARY KEY)' % table)
            db.commit(); db.close()
            db = sqlite3.connect(runtime / 'history-access.sqlite3')
            db.execute('CREATE TABLE requests (id INTEGER PRIMARY KEY)')
            db.execute('CREATE TABLE events (id INTEGER PRIMARY KEY)')
            db.commit(); db.close()
            make_db(runtime / 'replay.sqlite3', 'seen')
            result = ops_drill.run(runtime, folder / 'work', rto_target=60, rpo_target=86400,
                                   retention_days=30)
            self.assertTrue(result['checks']['alert_delivered'])
            self.assertTrue(result['checks']['offsite_ok'])
            self.assertTrue(result['checks']['retention_deleted_old'])
            self.assertTrue(result['checks']['retention_kept_new'])
            self.assertTrue(result['checks']['dr_no_invalid'])
            self.assertTrue(result['checks']['dr_complete'])
            self.assertTrue(result['ok'])

    def test_alert_repeats_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            first = ops_drill.alert_delivery(folder / 'alerts', {'id': 'a1', 'text': 'x'})
            second = ops_drill.alert_delivery(folder / 'alerts', {'id': 'a1', 'text': 'x'})
            self.assertFalse(first['deduplicated'])
            self.assertTrue(second['deduplicated'])
            self.assertEqual(first['copies'], second['copies'])
            log = (folder / 'alerts' / 'alerts.jsonl').read_text().splitlines()
            self.assertEqual(len([row for row in log if row.strip()]), 1)

    def test_retention_does_not_delete_a_fresh_same_named_file_elsewhere(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            old_dir = folder / 'a'; old_dir.mkdir()
            new_dir = folder / 'b'; new_dir.mkdir()
            old = old_dir / 'same.log'; old.write_text('old')
            os.utime(old, (time.time() - 40 * 86400,) * 2)
            fresh = new_dir / 'same.log'; fresh.write_text('new')
            result = ops_drill.retention(folder, 30)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertIn('same.log', result['deleted'])
            self.assertIn('same.log', result['kept'])


if __name__ == '__main__':
    unittest.main()
