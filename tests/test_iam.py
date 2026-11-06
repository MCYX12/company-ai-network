import json
import os
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from app.iam import AccountsProvider, LocalProvider, RosterStore
from scripts import bootstrap
from scripts.auto_renew import auto_renew
from scripts.offboard_member import offboard

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


def members_db(path, rows):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE members(id TEXT PRIMARY KEY,name TEXT,device TEXT,projects TEXT,state TEXT)')
        for ident, state in rows:
            db.execute('INSERT INTO members VALUES(?,?,?,?,?)', (ident, ident, 'd', '[]', state))


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_manual_roster_overlays_account_state(self):
        store = RosterStore(self.dir / 'roster.sqlite3')
        store.set('emp-2', 'left')
        self.assertEqual(store.status('emp-2'), 'left')
        self.assertEqual(store.status('emp-9'), 'unknown')
        accounts_db = self.dir / 'accounts.sqlite3'
        members_db(accounts_db, [('emp-1', 'active'), ('emp-2', 'disabled'), ('emp-3', 'rejected'), ('emp-4', 'provisioning')])
        accounts = AccountsProvider(accounts_db)
        self.assertEqual(accounts.status('emp-1'), 'active')
        self.assertEqual(accounts.status('emp-2'), 'suspended')
        self.assertEqual(accounts.status('emp-3'), 'left')
        combined = LocalProvider(store, accounts)
        self.assertEqual(combined.status('emp-2'), 'left')       # manual roster wins
        self.assertEqual(combined.status('emp-1'), 'active')     # falls back to accounts
        self.assertEqual([row['employee_id'] for row in store.listing()], ['emp-2'])

    def test_invalid_status_rejected(self):
        store = RosterStore(self.dir / 'roster.sqlite3')
        with self.assertRaises(ValueError):
            store.set('emp-1', 'retired')


class AutoRenewSkipTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.runtime = Path(self.tmp.name)
        (self.runtime / 'certs').mkdir()
        (self.runtime / 'certs/ENTERPRISE_CA.json').write_text('{"format":1}')

    def tearDown(self):
        self.tmp.cleanup()

    def test_blocked_employee_certificate_is_not_renewed(self):
        registry = {'schema_version': 1, 'agents': [
            {'agent_id': 'ag-a', 'device_id': 'ubuntu-demo', 'employee_id': 'demo-a', 'tool': 'fixture', 'projects': ['demo-project'], 'enabled': True},
            {'agent_id': 'ag-b', 'device_id': 'mac-demo', 'employee_id': 'demo-b', 'tool': 'fixture', 'projects': ['demo-project'], 'enabled': True}]}
        (self.runtime / 'registry.json').write_text(json.dumps(registry))
        with (
            patch.dict(os.environ, {'AGENT_REGISTRY': str(self.runtime / 'registry.json')}),
            patch('app.iam.is_blocked', side_effect=lambda employee: employee == 'demo-a'),
            patch('scripts.auto_renew.expiring', return_value=[{'name': 'ag-a', 'seconds_remaining': 1}]),
            patch('scripts.auto_renew.renew_runtime', return_value={'applied': True}) as renewal,
        ):
            report = auto_renew(self.runtime, 30, apply=True)
        self.assertEqual(report['skipped_inactive'], ['ag-a'])
        self.assertEqual(renewal.call_args.kwargs.get('skip'), ['ag-a'])


class OffboardTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.runtime = Path(self.tmp.name)
        bootstrap.RUNTIME = self.runtime
        bootstrap.main()
        registry = {'schema_version': 1, 'agents': [
            {'agent_id': 'ag-a', 'device_id': 'ubuntu-demo', 'employee_id': 'demo-a', 'tool': 'fixture', 'projects': ['demo-project'], 'enabled': True},
            {'agent_id': 'ag-b', 'device_id': 'mac-demo', 'employee_id': 'demo-b', 'tool': 'fixture', 'projects': ['demo-project'], 'enabled': True},
            {'agent_id': 'ag-c', 'device_id': 'mac-demo', 'employee_id': 'demo-b', 'tool': 'fixture', 'projects': ['demo-project'], 'enabled': True}]}
        (self.runtime / 'registry.json').write_text(json.dumps(registry))

    def tearDown(self):
        self.tmp.cleanup()

    def test_offboard_disables_agents_and_removes_nats_users(self):
        registry = json.loads((self.runtime / 'registry.json').read_text())
        employee = registry['agents'][0]['employee_id']
        targets = [row['agent_id'] for row in registry['agents'] if row['employee_id'] == employee]
        report = offboard(self.runtime, employee, 'left company', apply=True)
        self.assertTrue(report['applied'])
        after = json.loads((self.runtime / 'registry.json').read_text())
        for row in after['agents']:
            if row['employee_id'] == employee:
                self.assertFalse(row['enabled'])
        config = json.loads((self.runtime / 'nats.conf').read_text())
        users = [u.get('user') for u in config['accounts']['POC']['users']]
        for name in targets:
            self.assertNotIn(name, users)
        self.assertEqual(RosterStore(self.runtime / 'roster.sqlite3').status(employee), 'left')

    def test_unknown_employee_changes_nothing(self):
        with self.assertRaises(ValueError):
            offboard(self.runtime, 'nobody', 'test')


if __name__ == '__main__':
    unittest.main()
