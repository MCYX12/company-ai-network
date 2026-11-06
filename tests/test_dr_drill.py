"""The DR drill restores the authority databases and measures RTO/RPO."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import dr_drill


def make_db(path, table, rows=1):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE %s (id INTEGER PRIMARY KEY)' % table)
    for _ in range(rows):
        db.execute('INSERT INTO %s DEFAULT VALUES' % table)
    db.commit(); db.close()


class MissingAuthorityTests(unittest.TestCase):
    def test_missing_authority_databases_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            runtime = folder / 'runtime'; runtime.mkdir()
            (runtime / 'certs').mkdir()
            db = sqlite3.connect(runtime / 'tasks.sqlite3')
            db.execute('CREATE TABLE tasks (task_id TEXT PRIMARY KEY, state TEXT, created_at REAL, result TEXT)')
            db.execute("INSERT INTO tasks VALUES('t1','succeeded',1.0,NULL)")
            db.commit(); db.close()
            result = dr_drill.run(runtime, folder / 'work')
            self.assertEqual(result['invalid_databases'], [])
            self.assertIn('roster.sqlite3', result['missing_authority_databases'])
            self.assertIn('accounts.sqlite3', result['missing_authority_databases'])


class DrDrillTests(unittest.TestCase):
    def test_drill_restores_authority_and_reports_rto_rpo(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            runtime = folder / 'runtime'; runtime.mkdir()
            (runtime / 'certs').mkdir()
            db = sqlite3.connect(runtime / 'tasks.sqlite3')
            db.execute('CREATE TABLE tasks (task_id TEXT PRIMARY KEY, state TEXT, created_at REAL, result TEXT)')
            db.execute("INSERT INTO tasks VALUES('t1','succeeded',1.0,NULL)")
            db.execute('CREATE TABLE task_audit (task_id TEXT, event TEXT, timestamp REAL)')
            db.commit(); db.close()
            make_db(runtime / 'roster.sqlite3', 'employment', 2)
            db = sqlite3.connect(runtime / 'history-access.sqlite3')
            db.execute('CREATE TABLE requests (id INTEGER PRIMARY KEY)')
            db.execute('CREATE TABLE events (id INTEGER PRIMARY KEY)')
            db.execute('INSERT INTO requests DEFAULT VALUES')
            db.commit(); db.close()
            make_db(runtime / 'replay.sqlite3', 'seen', 3)
            result = dr_drill.run(runtime, folder / 'work')
            self.assertGreaterEqual(result['rto_seconds'], 0)
            self.assertIsNotNone(result['recovery_point_seconds'])
            self.assertEqual(result['invalid_databases'], [])
            self.assertEqual(result['authority_restored'],
                             {'employment': 2, 'history_requests': 1, 'replay_seen': 3})
            self.assertIn('roster.sqlite3', result['restored_databases'])


if __name__ == '__main__':
    unittest.main()
