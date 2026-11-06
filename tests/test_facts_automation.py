import json
from pathlib import Path
import tempfile
import unittest

from scripts import audit_chain, build_facts, refresh_facts


class AnchorOffHostTests(unittest.TestCase):
    def test_push_command_shape(self):
        self.assertEqual(audit_chain.push_command('user@host:/var/anchor/', '/tmp/a.json'),
                         ['scp', '/tmp/a.json', 'user@host:/var/anchor/'])

    def test_export_outside_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'audit.jsonl'
            log.write_text(json.dumps({'event': 'a'}) + '\n')
            out = Path(folder) / 'anchors' / 'anchor.json'
            result = audit_chain.export_anchor(log, out)
            self.assertTrue(out.exists())
            self.assertTrue(audit_chain.check_anchor(log, out)['ok'])


class FactsAutomationTests(unittest.TestCase):
    def _root_with_evidence(self, folder, result='OK', covered=None):
        root = Path(folder)
        (root / 'facts/evidence').mkdir(parents=True)
        older = root / 'facts/evidence/test-run-1.json'
        newer = root / 'facts/evidence/test-run-2.json'
        older.write_text(json.dumps({'kind': 'test_run', 'result': result, 'executed_at': 200.0,
                                     'revision': 'rev-evidence', 'covered': covered or ['test_x.XTests.test_run']}))
        newer.write_text(json.dumps({'kind': 'test_run', 'result': 'OK', 'executed_at': 200.0,
                                     'revision': 'rev-evidence', 'covered': ['test_x.XTests.test_run']}))
        older.touch(); newer.touch()
        import os
        os.utime(older, (1, 1)); os.utime(newer, (2, 2))
        (root / 'app').mkdir(); (root / 'app/x.py').write_text('x=1')
        os.utime(root / 'app/x.py', (100, 100))
        return root, older, newer

    def test_newest_evidence_and_auto_item(self):
        with tempfile.TemporaryDirectory() as folder:
            root, older, newer = self._root_with_evidence(folder)
            self.assertEqual(build_facts.newest_evidence(root), newer)
            record = build_facts.compute_item({'item': 'X', 'code_paths': ['app/x.py'],
                                               'tests': ['test_x'], 'auto_tests_evidence': True}, root)
            self.assertEqual(record['status'], 'tests_pass')

    def test_auto_item_ignores_failed_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root, older, newer = self._root_with_evidence(folder, result='FAILED')
            newer.write_text(json.dumps({'kind': 'test_run', 'result': 'FAILED', 'executed_at': 200.0,
                                         'revision': None, 'covered': ['test_x.XTests.test_run']}))
            record = build_facts.compute_item({'item': 'X', 'code_paths': ['app/x.py'],
                                               'tests': ['test_x'], 'auto_tests_evidence': True}, root)
            self.assertFalse(record['levels']['tests_pass'])

    def test_parse_result(self):
        self.assertEqual(refresh_facts.parse_result('Ran 7 tests in 0.1s\n\nOK\n'),
                         {'ran': 7, 'skipped': 0, 'result': 'OK'})
        self.assertEqual(refresh_facts.parse_result('Ran 3 tests\nFAILED (failures=1)\n')['result'], 'FAILED')
        self.assertEqual(refresh_facts.parse_result('Ran 3 tests in 0.1s\n\nOK (skipped=1)\n'),
                         {'ran': 3, 'skipped': 1, 'result': 'OK'})


if __name__ == '__main__':
    unittest.main()
