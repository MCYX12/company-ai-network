"""FIND-01/02/03: backup includes authority DBs, NATS_URL is a list, units name cwd/paths."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import common
from scripts import multihost_plan, runtime_backup


class BackupInclusionTests(unittest.TestCase):
    def _db(self, path, table):
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE %s (id INTEGER PRIMARY KEY)' % table)
        db.execute('INSERT INTO %s DEFAULT VALUES' % table)
        db.commit(); db.close()

    def test_authority_databases_are_backed_up(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / 'runtime'; runtime.mkdir()
            (runtime / 'certs').mkdir()
            self._db(runtime / 'tasks.sqlite3', 'tasks')
            self._db(runtime / 'roster.sqlite3', 'employment')
            self._db(runtime / 'history-access.sqlite3', 'requests')
            db = sqlite3.connect(runtime / 'history-access.sqlite3')
            db.execute('CREATE TABLE events (id INTEGER PRIMARY KEY)'); db.commit(); db.close()
            self._db(runtime / 'replay.sqlite3', 'seen')
            records = runtime / 'project-records'; records.mkdir()
            (records / 'demo-project.json').write_text('{"project_id": "demo-project"}')
            (records / 'demo-project.meta.json').write_text('{"confirmed_at": "2026-10-01T00:00:00+00:00"}')
            out = Path(folder) / 'backup'
            runtime_backup.backup(runtime, out)
            names = {p.name for p in out.rglob('*.sqlite3')}
            self.assertIn('roster.sqlite3', names)
            self.assertIn('history-access.sqlite3', names)
            self.assertIn('replay.sqlite3', names)
            # The human-confirmation sidecar must survive too, or a restore loses the clock.
            self.assertTrue((out / 'project-records/demo-project.meta.json').exists())


class AddressParsingTests(unittest.TestCase):
    def test_comma_separated_url_becomes_a_server_list(self):
        self.assertEqual(common.parse_servers('tls://a:14222,tls://b:14222'),
                         ['tls://a:14222', 'tls://b:14222'])
        self.assertEqual(common.parse_servers(' tls://a:14222 , tls://b:14222 '),
                         ['tls://a:14222', 'tls://b:14222'])


class UnitTemplateTests(unittest.TestCase):
    def test_unit_names_cwd_and_install_dir(self):
        unit = multihost_plan.systemd_unit('d', '/srv/x/.venv/bin/python -u -m app.router',
                                           '/srv/x/runtime/router.env',
                                           working_directory='/srv/x',
                                           writable_paths=['/srv/x/runtime'])
        self.assertIn('WorkingDirectory=/srv/x', unit)
        self.assertIn('ReadWritePaths=/srv/x/runtime', unit)
        self.assertNotIn('/opt/company-agent', unit)


if __name__ == '__main__':
    unittest.main()
