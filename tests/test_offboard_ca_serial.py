"""Real ca_tool integration: identify an already-revoked cert by current serial."""
import tempfile
import unittest
from pathlib import Path

from scripts import ca_tool, offboard_member


class CaSerialIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ca = Path(self.tmp.name) / 'ca'
        ca_tool.init_root(self.ca)
        ca_tool.init_intermediate(self.ca)

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_status_uses_cn_and_serial(self):
        ca_tool.issue(self.ca, 'ag-a')
        ca_tool.revoke(self.ca, 'ag-a')
        rows = ca_tool.status(self.ca)['issued']
        self.assertTrue(any(r['name'].startswith('/CN=') for r in rows))
        self.assertTrue(all('serial' in r for r in rows))

    def _current_hex(self):
        return '%X' % offboard_member._certificate_serial(self.ca, 'ag-a')

    def test_index_write_failure_leaves_original_intact(self):
        from unittest.mock import patch
        issued = ca_tool.issue(self.ca, 'ag-a')
        key_file = issued.get('key') or str(self.ca / 'intermediate' / 'private' / 'ag-a.key')
        ca_tool.renew(self.ca, 'ag-a', 'client', key_file)
        index = self.ca / 'intermediate' / 'index.txt'
        before = index.read_bytes()
        with patch('scripts.ca_tool.os.replace', side_effect=OSError(27, 'EFBIG')):
            with self.assertRaises(OSError):
                ca_tool.revoke_serial(self.ca, self._current_hex())
        self.assertEqual(index.read_bytes(), before)                 # replace failed: original intact
        self.assertEqual(len(ca_tool.status(self.ca)['issued']), 2)  # both rows still readable
        with patch('scripts.ca_tool.os.fsync', side_effect=OSError(27, 'EFBIG')):
            with self.assertRaises(OSError):
                ca_tool.revoke_serial(self.ca, self._current_hex())
        self.assertEqual(index.read_bytes(), before)                 # write failed: original intact
        self.assertEqual(ca_tool.revoke_serial(self.ca, self._current_hex())['outcome'], 'revoked')

    def test_ca_lock_is_reentrant(self):
        with ca_tool._ca_lock(self.ca):
            with ca_tool._ca_lock(self.ca):      # the same thread may hold it for a whole batch
                pass

    def test_renewal_rechecks_employment_under_the_lock(self):
        issued = ca_tool.issue(self.ca, 'ag-a')
        key_file = issued.get('key') or str(self.ca / 'intermediate' / 'private' / 'ag-a.key')
        with self.assertRaises(ValueError):
            ca_tool.renew(self.ca, 'ag-a', 'client', key_file, authorize=lambda name: False)

    def test_index_missing_current_row_with_old_v_fails(self):
        issued = ca_tool.issue(self.ca, 'ag-a')
        key_file = issued.get('key') or str(self.ca / 'intermediate' / 'private' / 'ag-a.key')
        ca_tool.renew(self.ca, 'ag-a', 'client', key_file)      # old V + current V
        current = offboard_member._certificate_serial(self.ca, 'ag-a')
        index = self.ca / 'intermediate' / 'index.txt'
        kept = [line for line in index.read_text().splitlines()
                if offboard_member._as_int(line.split('\t')[3]) != current]
        index.write_text('\n'.join(kept) + '\n')               # the current row is lost
        with self.assertRaises(ValueError):                     # first call must fail, not succeed
            offboard_member._revoke(ca_tool, self.ca, 'ag-a')

    def test_ca_lock_is_mutual_exclusion(self):
        import threading
        import time
        state = {'inside': 0, 'max': 0}

        def worker():
            with ca_tool._ca_lock(self.ca):
                state['inside'] += 1
                state['max'] = max(state['max'], state['inside'])
                time.sleep(0.05)
                state['inside'] -= 1

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(state['max'], 1)      # shared lock serializes issue/renew/revoke/crl

    def _states(self):
        return sorted(r['state'] for r in ca_tool.status(self.ca)['issued']
                      if offboard_member._subject_cn(r['name']) == 'ag-a')

    def test_already_revoked_by_serial_without_private_key(self):
        ca_tool.issue(self.ca, 'ag-a')
        ca_tool.revoke(self.ca, 'ag-a')
        rows = ca_tool.status(self.ca)['issued']
        expected = next(int(r['serial'], 16) for r in rows if offboard_member._subject_cn(r['name']) == 'ag-a')
        # independent check: the current certificate serial is the revoked row's serial
        self.assertEqual(offboard_member._certificate_serial(self.ca, 'ag-a'), expected)
        key = self.ca / 'intermediate' / 'intermediate.key'
        moved = key.with_name('intermediate.key.off')
        key.rename(moved)
        try:
            self.assertEqual(offboard_member._revoke(ca_tool, self.ca, 'ag-a')['outcome'], 'already_revoked')
        finally:
            moved.rename(key)

    def test_all_valid_serials_are_revoked(self):
        issued = ca_tool.issue(self.ca, 'ag-a')
        key_file = issued.get('key') or str(self.ca / 'intermediate' / 'private' / 'ag-a.key')
        ca_tool.renew(self.ca, 'ag-a', 'client', key_file)   # old V + new V, no revocation yet
        self.assertEqual(self._states(), ['V', 'V'])
        result = offboard_member._revoke(ca_tool, self.ca, 'ag-a')
        self.assertEqual(result['outcome'], 'revoked')
        self.assertEqual(len(result['revoked_serials']), 2)
        self.assertEqual(self._states(), ['R', 'R'])

    def test_renewed_old_r_new_v_must_revoke_the_current_cert(self):
        issued = ca_tool.issue(self.ca, 'ag-a')
        ca_tool.revoke(self.ca, 'ag-a')
        key_file = issued.get('key') or str(self.ca / 'intermediate' / 'private' / 'ag-a.key')
        ca_tool.renew(self.ca, 'ag-a', 'client', key_file)
        before = [r['state'] for r in ca_tool.status(self.ca)['issued'] if offboard_member._subject_cn(r['name']) == 'ag-a']
        self.assertIn('V', before)                       # the renewed cert is still valid
        self.assertEqual(offboard_member._revoke(ca_tool, self.ca, 'ag-a')['outcome'], 'revoked')
        self.assertEqual(self._states(), ['R', 'R'])


if __name__ == '__main__':
    unittest.main()
