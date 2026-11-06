import json
from pathlib import Path
import tempfile
import unittest

from app.accounts import Accounts
from app.admin_channel import admins


class AdminUnificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        self.store = Accounts(self.runtime / 'accounts.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def _admin(self, name='admin-a'):
        code = self.store.setup_code().read_text().strip()
        self.store.register_admin(code, name, 'password-1234')
        return name

    def test_bind_and_disable_drops_authorization(self):
        name = self._admin()
        self.assertEqual(self.store.admin_identities(), set())
        self.store.bind_identity('root', name, 'admin-01')
        self.assertEqual(self.store.admin_identities(), {'admin-01'})
        listing = self.store.listing()['admins']
        self.assertEqual(listing[0]['mtls_identity'], 'admin-01')
        # a disabled web account immediately loses its mTLS authorization
        second = self.store.invite('root', 'admin', [])
        self.store.register_admin(second['code'], 'admin-b', 'password-5678')
        self.store.admin_status('root', name, False)
        self.assertEqual(self.store.admin_identities(), set())

    def test_bind_clears_and_validates(self):
        name = self._admin()
        self.store.bind_identity('root', name, 'admin-01')
        self.store.bind_identity('root', name, '')
        self.assertEqual(self.store.admin_identities(), set())
        with self.assertRaises(ValueError):
            self.store.bind_identity('root', name, 'BAD IDENTITY')
        with self.assertRaises(ValueError):
            self.store.bind_identity('root', 'nobody', 'admin-01')

    def test_channel_admins_union_of_accounts_and_file(self):
        name = self._admin()
        self.store.bind_identity('root', name, 'admin-01')
        (self.runtime / 'admin-identities.json').write_text(json.dumps({'admins': ['admin-02']}))
        self.assertEqual(admins(self.runtime), {'admin-01', 'admin-02'})
        # accounts-only
        (self.runtime / 'admin-identities.json').unlink()
        self.assertEqual(admins(self.runtime), {'admin-01'})
        # file-only
        (self.runtime / 'accounts.sqlite3').unlink()
        (self.runtime / 'admin-identities.json').write_text(json.dumps({'admins': ['admin-03']}))
        self.assertEqual(admins(self.runtime), {'admin-03'})

    def test_channel_survives_missing_accounts_db(self):
        self.assertEqual(admins(self.runtime), set())


if __name__ == '__main__':
    unittest.main()
