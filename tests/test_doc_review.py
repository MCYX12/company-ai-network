"""Full-review coverage: every chunk has a recorded outcome, none vanish."""
import tempfile
import unittest
from pathlib import Path

from app import doc_review


class ReviewRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        (self.root/'a.md').write_text('# A\n'+'line\n'*50+'## Tail\nend of a\n')
        (self.root/'b.md').write_text('# B\nsmall\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_manifest_records_every_chunk_without_a_model(self):
        report=doc_review.review_root(str(self.root),docs=['a.md','b.md'])
        self.assertEqual(report['summary']['skipped'],0)
        self.assertEqual(report['summary']['by_status'].get('manifest'),len(report['chunks']))
        self.assertTrue(all(c['sha256'] for c in report['chunks']))

    def test_extractor_success_and_failure_are_both_recorded(self):
        def extractor(chunk):
            if chunk['file']=='b.md':
                raise RuntimeError('model down')
            return [{'summary':'ok'}]
        report=doc_review.review_root(str(self.root),docs=['a.md','b.md'],extractor=extractor)
        statuses=report['summary']['by_status']
        self.assertGreaterEqual(statuses.get('processed',0),1)
        self.assertEqual(statuses.get('failed'),1)
        failed=[c for c in report['chunks'] if c['status']=='failed'][0]
        self.assertIn('model down',failed['reason'])

    def test_chunk_limit_marks_the_rest_omitted(self):
        report=doc_review.review_root(str(self.root),docs=['a.md','b.md'],extractor=lambda c:[],max_chunks=1)
        self.assertEqual(report['summary']['by_status'].get('omitted'),len(report['chunks'])-1)
        self.assertTrue(all(c.get('reason')=='CHUNK_LIMIT' for c in report['chunks'] if c['status']=='omitted'))

    def test_complete_only_when_every_chunk_was_processed(self):
        # Manifest-only is not a completed review.
        self.assertFalse(doc_review.review_root(str(self.root),docs=['a.md','b.md'])['complete'])
        good=doc_review.review_root(str(self.root),docs=['a.md','b.md'],
                                    extractor=lambda c:[{'summary':'ok'}])
        self.assertTrue(good['complete'])
        def bad(chunk):
            if chunk['file']=='b.md':
                raise RuntimeError('model down')
            return []
        self.assertFalse(doc_review.review_root(str(self.root),docs=['a.md','b.md'],extractor=bad)['complete'])
        # A merely absent profile entry is reported but does not by itself block completeness.
        missing=doc_review.review_root(str(self.root),docs=['missing.md','b.md'],extractor=lambda c:[])
        self.assertEqual([s['name'] for s in missing['skipped']],['missing.md'])
        self.assertTrue(missing['complete'])

    def test_all_garbage_or_refusal_never_completes(self):
        def refusing(chunk):
            raise ValueError('PARSE_FAILED')
        report=doc_review.review_root(str(self.root),docs=['a.md','b.md'],extractor=refusing)
        self.assertFalse(report['complete'])
        self.assertTrue(report['chunks'])
        self.assertTrue(all(c['status']=='failed' for c in report['chunks']))

    def test_revocation_before_read_reads_nothing(self):
        def authz():
            raise PermissionError('NOT_AUTHORIZED')
        report=doc_review.review_root(str(self.root),docs=['a.md','b.md'],
                                      extractor=lambda c:[],authz=authz)
        self.assertFalse(report['complete'])
        self.assertIsNotNone(report['revoked'])
        self.assertEqual(report['chunks'],[])          # no document was opened
        self.assertTrue(report['facts_withheld'])

    def test_revocation_during_read_does_not_open_later_documents(self):
        calls={'n':0}
        def authz():
            calls['n']+=1
            if calls['n']>=3:                          # initial, a.md, then b.md check
                raise PermissionError('NOT_AUTHORIZED')
        report=doc_review.review_root(str(self.root),docs=['a.md','b.md'],
                                      extractor=lambda c:[],authz=authz)
        self.assertFalse(report['complete'])
        self.assertNotIn('b.md',{c['file'] for c in report['chunks']})

    def test_revocation_after_extraction_withholds_earlier_facts(self):
        calls={'n':0}
        def authz():
            calls['n']+=1
            if calls['n']>=5:                          # initial, 2 reads, chunk 1, chunk 2
                raise PermissionError('NOT_AUTHORIZED')
        report=doc_review.review_root(str(self.root),docs=['a.md','b.md'],
                                      extractor=lambda c:[{'summary':'secret'}],authz=authz)
        self.assertFalse(report['complete'])
        self.assertTrue(report['facts_withheld'])
        self.assertTrue(all('facts' not in c for c in report['chunks']))

    def test_coverage_evidence_and_quality_are_separate_from_complete(self):
        report=doc_review.review_root(str(self.root),docs=['missing.md','b.md'],extractor=lambda c:[])
        self.assertEqual(report['quality'],'not_evaluated')      # not an accuracy verdict
        self.assertFalse(report['coverage']['no_readable'])
        self.assertEqual(report['coverage']['valid_empty'],report['coverage']['processed'])
        self.assertEqual(report['evidence']['facts'],0)
        # No readable document must not pass as a content review.
        empty=doc_review.review_root(str(self.root),docs=['missing.md'],extractor=lambda c:[])
        self.assertTrue(empty['coverage']['no_readable'])
        self.assertFalse(empty['complete'])

    def test_evidence_linkage_is_counted(self):
        facts=[{'summary':'x','basis':'documented','evidence_ids':['E1']}]
        report=doc_review.review_root(str(self.root),docs=['b.md'],extractor=lambda c:facts)
        self.assertEqual(report['evidence'],{'facts':1,'with_evidence_ids':1})
        self.assertTrue(report['complete'])

    def test_missing_document_is_reported_as_skipped(self):
        report=doc_review.review_root(str(self.root),docs=['missing.md','b.md'])
        self.assertEqual([s['name'] for s in report['skipped']],['missing.md'])
        self.assertEqual(report['summary']['chunks'],len([c for c in report['chunks'] if c['file']=='b.md']))


if __name__=='__main__':
    unittest.main()
