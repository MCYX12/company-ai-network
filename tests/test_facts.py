import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app import facts
from scripts import build_facts


class FactModelTests(unittest.TestCase):
    def test_status_is_highest_true_level(self):
        self.assertEqual(facts.fact('x', {'claimed_done': True})['status'], 'claimed_done')
        self.assertEqual(facts.fact('x', {'claimed_done': True, 'code_present': True, 'tests_pass': True})['status'], 'tests_pass')
        self.assertEqual(facts.fact('x', {})['status'], 'unknown')

    def test_claim_never_reaches_tests_or_accepted(self):
        record = facts.fact('x', {'docs_claim': True, 'claimed_done': True})
        self.assertNotIn(record['status'], ('tests_pass', 'accepted'))

    def test_explain_includes_provenance(self):
        record = facts.fact('x', {'code_present': True}, source='app/x.py', evidence=['app/x.py'],
                            verified_at=123.0, limits='仅证明代码存在')
        text = facts.explain(record)
        for token in ['来源：app/x.py', '证据：app/x.py', '核验时间：123.0', '限制：仅证明代码存在']:
            self.assertIn(token, text)

    def test_explain_separates_the_three_clocks(self):
        record = facts.fact('x', {'tests_pass': True}, verified_at=100.0,
                            evidence_executed_at=100.0, evidence_read_at=200.0, generated_at=300.0)
        text = facts.explain(record)
        self.assertIn('证据执行时间：100.0', text)
        self.assertIn('证据读取时间：200.0', text)
        self.assertIn('事实生成时间：300.0', text)


class BuildFactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'app').mkdir(); (self.root / 'app/x.py').write_text('x=1')
        (self.root / 'tests').mkdir()
        (self.root / 'SPEC.md').write_text('claim')

    def tearDown(self):
        self.tmp.cleanup()

    def _item(self, **kwargs):
        base = {'item': 'X'}; base.update(kwargs); return base

    def _test_evidence(self, name='tests/x.json', **overrides):
        payload = {'kind': 'test_run', 'scope': 'item X', 'result': 'OK', 'ran': 3,
                   'executed_at': time.time(), 'recorded_at': time.time(),
                   'revision': 'rev-evidence', 'covered': ['test_x.XTests.test_run']}
        payload.update(overrides)
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return name

    def _acceptance(self, name='accept.json', **overrides):
        payload = {'kind': 'acceptance', 'conclusion': 'accepted', 'scope': 'X',
                   'accepted_at': time.time(), 'artifact_version': 'rev-evidence',
                   'confirmed_by': 'reviewer'}
        payload.update(overrides)
        path = self.root / name
        path.write_text(json.dumps(payload))
        return name

    def test_levels_are_computed_from_artifacts(self):
        self.assertEqual(build_facts.compute_item(self._item(docs_claim='README 声称'), self.root)['status'], 'claimed_done')
        self.assertEqual(build_facts.compute_item(self._item(code_paths=['app/x.py']), self.root)['status'], 'code_present')
        evidence = self._test_evidence()
        item = self._item(code_paths=['app/x.py'], tests=['test_x'], tests_evidence=evidence)
        self.assertEqual(build_facts.compute_item(item, self.root)['status'], 'tests_pass')
        acceptance = self._acceptance()
        self.assertEqual(build_facts.compute_item(self._item(acceptance=acceptance), self.root)['status'], 'accepted')

    def test_missing_artifacts_do_not_upgrade(self):
        record = build_facts.compute_item(self._item(code_paths=['app/missing.py'], tests_evidence='tests/missing.json'), self.root)
        self.assertEqual(record['status'], 'unknown')

    def test_failed_evidence_never_yields_tests_pass(self):
        evidence = self._test_evidence(result='FAILED')
        item = self._item(code_paths=['app/x.py'], tests=['test_x'], tests_evidence=evidence)
        record = build_facts.compute_item(item, self.root)
        self.assertFalse(record['levels']['tests_pass'])
        self.assertNotEqual(record['status'], 'tests_pass')

    def test_unrelated_test_success_does_not_upgrade(self):
        evidence = self._test_evidence(covered=['test_other.YTests.test_something'])
        item = self._item(code_paths=['app/x.py'], tests=['test_x'], tests_evidence=evidence)
        record = build_facts.compute_item(item, self.root)
        self.assertFalse(record['levels']['tests_pass'])

    def test_auto_evidence_must_cover_declared_selectors(self):
        folder = self.root / 'facts/evidence'; folder.mkdir(parents=True)
        (folder / 'run.json').write_text(json.dumps({'kind': 'test_run', 'result': 'OK',
                                                     'executed_at': time.time(), 'covered': ['test_other.Y.test_z']}))
        item = self._item(code_paths=['app/x.py'], tests=['test_x'], auto_tests_evidence=True)
        self.assertFalse(build_facts.compute_item(item, self.root)['levels']['tests_pass'])

    def test_source_newer_than_evidence_is_stale(self):
        evidence = self._test_evidence(executed_at=1.0, recorded_at=1.0, revision=None)
        item = self._item(code_paths=['app/x.py'], tests=['test_x'], tests_evidence=evidence)
        self.assertFalse(build_facts.compute_item(item, self.root)['levels']['tests_pass'])

    def test_acceptance_requires_matching_scope_and_conclusion(self):
        rejected = self._acceptance(name='rejected.json', conclusion='rejected')
        self.assertFalse(build_facts.compute_item(self._item(acceptance=rejected), self.root)['levels']['accepted'])
        other = self._acceptance(name='other.json', scope='Another item')
        self.assertFalse(build_facts.compute_item(self._item(acceptance=other), self.root)['levels']['accepted'])

    def test_evidence_without_a_revision_does_not_upgrade(self):
        evidence = self._test_evidence(revision=None)
        item = self._item(code_paths=['app/x.py'], tests=['test_x'], tests_evidence=evidence)
        self.assertFalse(build_facts.compute_item(item, self.root)['levels']['tests_pass'])

    def test_all_skipped_evidence_does_not_upgrade(self):
        item = self._item(code_paths=['app/x.py'], tests=['test_x'])
        for overrides in ({'ran': 3, 'skipped': 3}, {'skipped': ['test_x.XTests.test_run']}):
            evidence = self._test_evidence(name='skip-%s.json' % len(str(overrides)), **overrides)
            item = self._item(code_paths=['app/x.py'], tests=['test_x'], tests_evidence=evidence)
            self.assertFalse(build_facts.compute_item(item, self.root)['levels']['tests_pass'], overrides)

    def test_acceptance_without_a_version_or_scope_does_not_upgrade(self):
        missing_version = self._acceptance(name='noversion.json', artifact_version=None, revision=None)
        missing_scope = self._acceptance(name='noscope.json', scope=None)
        for path in (missing_version, missing_scope):
            self.assertFalse(build_facts.compute_item(self._item(acceptance=path), self.root)['levels']['accepted'], path)

    def test_computed_item_binds_source_and_evidence_digests(self):
        evidence = self._test_evidence()
        item = self._item(code_paths=['app/x.py'], tests=['test_x'], tests_evidence=evidence)
        first = build_facts.compute_item(item, self.root)
        self.assertTrue(first['code_digest'])
        self.assertTrue(first['evidence_digest'])
        (self.root / 'app/x.py').write_text('x=2')
        self.assertNotEqual(first['code_digest'], build_facts.compute_item(item, self.root)['code_digest'])

    def test_rebuild_does_not_forge_verification_time(self):
        evidence = self._test_evidence()
        item = self._item(code_paths=['app/x.py'], tests=['test_x'], tests_evidence=evidence)
        first = build_facts.compute_item(item, self.root, now=time.time())
        later = build_facts.compute_item(item, self.root, now=time.time() + 1000)
        self.assertEqual(first['verified_at'], first['evidence_executed_at'])
        self.assertEqual(first['verified_at'], later['verified_at'])
        self.assertGreater(later['generated_at'], first['generated_at'])

    def test_fact_file_records_revision(self):
        data = build_facts.build({'project_id': 'demo', 'items': [self._item(docs_claim='x')]}, self.root, revision='abc123')
        self.assertEqual(data['revision'], 'abc123')
        self.assertEqual(data['items'][0]['status'], 'claimed_done')


class PromptFactsTests(unittest.TestCase):
    def test_build_prompt_includes_facts_section(self):
        from app.peer_question import build_prompt
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'demo-project.json').write_text(json.dumps({'project_id': 'demo-project', 'items': [
                facts.fact('事项A', {'code_present': True}, source='app/x.py', limits='仅代码存在')]}))
            row = {'agent_id': 'peer-x', 'projects': ['demo-project']}
            progress = ('OK', {'project_id': 'demo-project', 'source_id': 's', 'updated_at': '2026-10-01',
                               'freshness': 'fresh', 'summary': 'x', 'completed': [], 'blockers': []})
            with patch.dict(os.environ, {'POC_FACTS_DIR': folder}), \
                 patch('app.peer_question.read_progress', return_value=progress):
                prompt, sources = build_prompt(row, {'question': '进展如何', 'projects': ['demo-project']})
        self.assertIn('已核验事实', prompt)
        self.assertIn('当前代码存在', prompt)


if __name__ == '__main__':
    unittest.main()
