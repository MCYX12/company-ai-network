"""The project_notes_v1 profile and its opt-in, device-owner document extension.

Approving a directory must not silently widen the read set, so the extension is bounded
and only accepts document-like relative paths; the digest must report which allowed files
it actually used and must not run the same read-only git status twice for one digest.
"""
import importlib.util
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import local_progress
from app.directory_context import CURRENT

GRANT = {'id': 'g1', 'caller': 'ag-a', 'target': 'ag-a', 'root': '',
         'profile': 'project_notes_v1', 'state': 'approved', 'expires': 4102444800.0}


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'README.md').write_text('# 标题\n')
    (path / 'cloud_server_algorithm_plan.md').write_text('CLOUD PLAN BODY SECRET\n')
    for argv in (['init', '-q'], ['config', 'user.email', 't@t'], ['config', 'user.name', 't'],
                 ['add', '-A'], ['commit', '-qm', 'init']):
        subprocess.run(['/usr/bin/git', *argv], cwd=path, check=True, capture_output=True)


class ExtraDocsParsingTests(unittest.TestCase):
    def test_rejects_unsafe_and_non_document_entries(self):
        raw = '/etc/passwd,../x.md,.hidden.md,a/b/c/d.md,run.py,plan.md'
        with patch.dict(os.environ, {local_progress.EXTRA_DOCS_ENV: raw}):
            self.assertEqual(local_progress.extra_docs(), ('plan.md',))

    def test_merges_without_duplicating_the_base_profile(self):
        with patch.dict(os.environ, {local_progress.EXTRA_DOCS_ENV: 'README.md,plan.md'}):
            docs = local_progress.notes_docs()
            self.assertEqual(docs[0], 'README.md')
            self.assertEqual(docs.count('README.md'), 1)
            self.assertIn('plan.md', docs)

    def test_profile_digest_changes_with_the_declared_list(self):
        base = local_progress.profile_digest()
        with patch.dict(os.environ, {local_progress.EXTRA_DOCS_ENV: 'plan.md'}):
            self.assertNotEqual(base, local_progress.profile_digest())


class ExtraDocsInDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'Proj'
        make_repo(self.root)
        CURRENT.set([dict(GRANT, root=str(self.root))])

    def tearDown(self):
        CURRENT.set(None)
        self.tmp.cleanup()

    def _digest(self, mode='safe', extra='', root=None, budget=4000, question=None, coverage=None):
        with patch('app.local_progress.RUNTIME', Path(self.tmp.name)), \
             patch.object(local_progress, 'NOTES_MODE', mode), \
             patch.dict(os.environ, {local_progress.EXTRA_DOCS_ENV: extra}):
            return local_progress.notes_digest('ag-a', 'ag-a', str(root or self.root), budget,
                                               question=question, coverage=coverage)

    def test_raw_digest_retrieves_a_late_chunk_instead_of_the_file_head(self):
        name='cloud_server_algorithm_plan.md'
        filler='\n'.join('第%d行 无关填充内容' % i for i in range(300))
        (self.root/name).write_text('# 开头\n'+filler+'\n## 结算规则\nAPRILTAG 自动打击按命中数结算\n')
        coverage={}
        block=self._digest('raw',name,budget=700,question='结算规则是什么',coverage=coverage)
        # The late section is retrieved even though it is far past the file head.
        self.assertIn('APRILTAG',block)
        self.assertIn('结算规则',[d.get('heading') for d in coverage['digested']])
        self.assertTrue(any(d.get('file')==name for d in coverage['digested']))
        self.assertTrue(all(d.get('sha256') for d in coverage['digested']))

    def test_coverage_reports_omitted_chunks_and_truncation(self):
        name='cloud_server_algorithm_plan.md'
        body='\n'.join('## 段%d\n内容 %d %s' % (i,i,'x'*400) for i in range(8))
        (self.root/name).write_text(body)
        coverage={}
        self._digest('raw',name,budget=400,question='段3 内容',coverage=coverage)
        self.assertGreater(coverage['omitted_total'],0)
        self.assertTrue(coverage['truncated'])
        self.assertTrue(all(o.get('reason')=='BUDGET' for o in coverage['omitted']))

    def test_small_document_is_not_falsely_marked_truncated(self):
        name='cloud_server_algorithm_plan.md'
        (self.root/name).write_text('## 小文档\n一句很短的话\n')
        coverage={}
        self._digest('raw',name,budget=1000,question='小文档',coverage=coverage)
        self.assertGreaterEqual(coverage['digested_total'],1)
        self.assertFalse(coverage['truncated'])

    def test_default_profile_reports_coverage_without_the_extra_document(self):
        block = self._digest('safe')
        self.assertNotIn('cloud_server_algorithm_plan.md', block)
        self.assertIn('命中 1 个', block)

    def test_owner_declared_document_is_selected_and_raw_embedded(self):
        block = self._digest('raw', 'cloud_server_algorithm_plan.md')
        self.assertIn('cloud_server_algorithm_plan.md', block)
        self.assertIn('CLOUD PLAN BODY SECRET', block)
        self.assertIn('已由设备所有者扩展', block)

    def test_unmatched_documents_are_reported_not_hidden(self):
        empty = Path(self.tmp.name) / 'Empty'
        empty.mkdir()
        CURRENT.set([dict(GRANT, root=str(empty))])
        block = self._digest('safe', root=empty)
        self.assertIn('未命中', block)

    def test_raw_digest_runs_git_only_once(self):
        # Both modules use the same subprocess module object, so one patch counts every git
        # invocation on this path. The digest reads git once and hands it to collect().
        with patch('subprocess.run', wraps=subprocess.run) as run:
            self._digest('raw')
        self.assertEqual(run.call_count, 1)


def load_client_worker():
    spec = importlib.util.spec_from_file_location(
        'notes_client_worker', Path(__file__).resolve().parents[1] / 'packaging/client/worker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClientWorkerDocsTests(unittest.TestCase):
    """The installed device worker must follow the same bounded profile."""

    def setUp(self):
        self.worker = load_client_worker()

    def test_safe_docs_rejects_unsafe_and_non_document_entries(self):
        raw = ['/etc/passwd', '../x.md', '.hidden.md', 'a/b/c/d.md', 'run.py', 'plan.md']
        self.assertEqual(self.worker.safe_docs(raw), ['plan.md'])

    def test_platform_rules_match_the_builtin_path(self):
        from app import harness_adapter
        self.assertEqual(self.worker.PLATFORM_RULES, harness_adapter.PLATFORM_RULES)

    def test_effective_docs_merges_without_duplicating_the_base_profile(self):
        docs = self.worker.effective_docs({'notes_docs': ['README.md', 'plan.md']})
        self.assertEqual(docs[0], 'README.md')
        self.assertEqual(docs.count('README.md'), 1)
        self.assertIn('plan.md', docs)

    def test_owner_document_is_listed_in_safe_mode_and_embedded_only_in_raw(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'Proj'
            make_repo(root)
            docs = self.worker.effective_docs({'notes_docs': ['cloud_server_algorithm_plan.md']})
            safe = self.worker.notes_digest(str(root), mode='safe', docs=docs)
            self.assertIn('cloud_server_algorithm_plan.md', safe)
            self.assertNotIn('CLOUD PLAN BODY SECRET', safe)
            raw = self.worker.notes_digest(str(root), mode='raw', budget=4000, docs=docs)
            self.assertIn('CLOUD PLAN BODY SECRET', raw)

    def test_the_default_profile_does_not_list_the_extra_document(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'Proj'
            make_repo(root)
            safe = self.worker.notes_digest(str(root), mode='safe')
            self.assertNotIn('cloud_server_algorithm_plan.md', safe)

    def test_linked_notes_and_linked_root_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'Project'
            root.mkdir()
            outside = Path(d) / 'outside.md'
            outside.write_text('OUTSIDE_SECRET', encoding='utf-8')
            (root / 'README.md').symlink_to(outside)
            os.link(outside, root / 'STATUS.md')
            block = self.worker.notes_digest(str(root), mode='raw')
            self.assertNotIn('OUTSIDE_SECRET', block)
            linked = Path(d) / 'Linked'
            linked.symlink_to(root, target_is_directory=True)
            self.assertIsNone(self.worker.notes_digest(str(linked), mode='raw'))

    def test_utf8_bom_off_mode_ads_and_budget(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'README.md').write_text('中文测试正文' * 500, encoding='utf-8-sig')
            block = self.worker.notes_digest(str(root), mode='raw', budget=500)
            self.assertIn('中文测试正文', block)
            self.assertNotIn('\ufeff', block)
            self.assertLessEqual(len(block), 500)
            with patch.object(self.worker, 'note_path', side_effect=AssertionError('read')):
                self.assertIsNone(self.worker.notes_digest(str(root), mode='off'))
            self.assertEqual(self.worker.safe_docs(['README.md:payload.md']), [])


if __name__ == '__main__':
    unittest.main()
