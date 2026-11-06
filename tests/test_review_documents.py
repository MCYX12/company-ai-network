"""Review extraction and authorization: only a valid shaped result is an empty result."""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import review_documents


class ParseFactsTests(unittest.TestCase):
    def test_plain_and_wrapped_json(self):
        self.assertEqual(review_documents.parse_facts('{"facts":[{"summary":"a"}]}'),[{'summary':'a'}])
        self.assertEqual(review_documents.parse_facts('```json\n{"facts":[{"summary":"a"}]}\n```'),[{'summary':'a'}])
        self.assertEqual(review_documents.parse_facts('好的：{"facts":[{"summary":"a"}]} 完'),[{'summary':'a'}])

    def test_a_valid_empty_facts_list_is_an_empty_result(self):
        self.assertEqual(review_documents.parse_facts('{"facts":[]}'),[])

    def test_garbage_refusal_and_wrong_shape_raise(self):
        for bad in ('not json at all','','抱歉，我无法回答','{"x":1}','{"facts":"no"}','[1,2,3]'):
            with self.assertRaises(ValueError, msg=bad):
                review_documents.parse_facts(bad)

    def test_extractor_returns_guarded_facts_and_raises_on_unparseable(self):
        chunk={'file':'README.md','heading':'','start_line':1,'end_line':2,'text':'项目说明'}
        with patch('scripts.summarize_local_project.request',
                   return_value={'response':'{"facts":[{"summary":"该项目是示例","basis":"documented","evidence_ids":["E1"]}]}'}):
            facts=review_documents.local_extractor(chunk)
        self.assertEqual([f['summary'] for f in facts],['该项目是示例'])
        with patch('scripts.summarize_local_project.request',return_value={'response':'抱歉，我无法回答'}):
            with self.assertRaises(ValueError):review_documents.local_extractor(chunk)
        with patch('scripts.summarize_local_project.request',return_value={'response':'{"facts":[]}'}):
            self.assertEqual(review_documents.local_extractor(chunk),[])


class ResolveDocsTests(unittest.TestCase):
    def test_reuses_profile_path_rules(self):
        for bad in ('../x.md','/etc/passwd','.hidden.md','a/b/c/d.md','run.py'):
            with self.assertRaises(ValueError, msg=bad):
                review_documents.resolve_docs(bad)
        self.assertEqual(review_documents.resolve_docs('README.md, docs/plan.md'),
                         ['README.md','docs/plan.md'])
        self.assertIsNone(review_documents.resolve_docs(None))


class AuthorizationPathTests(unittest.TestCase):
    def _listing(self,granted):
        store=patch('scripts.review_documents.DirectoryRequests')
        return store, [{'state':'approved','root':granted}]

    def test_traversal_root_is_refused_even_when_the_string_prefix_matches(self):
        store,rows=self._listing('/data/projects/demo')
        with store as factory:
            factory.return_value.listing.return_value=rows
            self.assertFalse(review_documents.authorized('ag-b','ag-a','/data/projects/demo/../other'))
            self.assertTrue(review_documents.authorized('ag-b','ag-a','/data/projects/demo'))

    def test_symlink_root_that_escapes_the_grant_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);granted=base/'demo';secret=base/'secret'
            granted.mkdir();secret.mkdir()
            (granted/'escape').symlink_to(secret, target_is_directory=True)
            store,rows=self._listing(str(granted))
            with store as factory:
                factory.return_value.listing.return_value=rows
                self.assertFalse(review_documents.authorized('ag-b','ag-a',str(granted/'escape')))
                self.assertTrue(review_documents.authorized('ag-b','ag-a',str(granted)))

    def test_non_normalized_or_relative_root_is_refused(self):
        store,rows=self._listing('/data/projects/demo')
        with store as factory:
            factory.return_value.listing.return_value=rows
            self.assertFalse(review_documents.authorized('ag-b','ag-a','relative/path'))
            self.assertFalse(review_documents.authorized('ag-b','ag-a','/data/projects/demo/'))


class CliRevocationDeliveryTests(unittest.TestCase):
    def _run(self,extra=(),side_effect=(True,False)):
        # Everything is checked while the temporary directory is still alive; the previous
        # --out content also proves a failed delivery never overwrites an existing report.
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            root=folder/'data'/'projects'/'demo'
            root.mkdir(parents=True)
            (root/'README.md').write_text('# hi\n')
            out=folder/'report.json'
            out.write_text('PREVIOUS')
            # One explicit document so the authorization call sequence reaches extraction.
            argv=['review_documents','--root',str(root),'--caller','ag-b','--target','ag-a',
                  '--extract','--allowed-docs','README.md','--out',str(out),*extra]
            buffer=io.StringIO();calls=[]
            def extractor(chunk):
                calls.append(chunk['file'])
                return [{'summary':'SECRETMARK'}]
            with patch.object(sys,'argv',argv), \
                 patch('scripts.review_documents.authorized',side_effect=list(side_effect)), \
                 patch('scripts.review_documents.local_extractor',extractor), \
                 redirect_stdout(buffer):
                code=review_documents.main()
            return {'code':code,'exists':out.exists(),'content':out.read_text() if out.exists() else None,
                    'stdout':buffer.getvalue(),'extractor_calls':list(calls)}

    def test_revocation_writes_no_facts_and_keeps_the_previous_file(self):
        result=self._run()
        self.assertEqual(result['code'],4)
        self.assertEqual(result['content'],'PREVIOUS')      # not overwritten
        self.assertIn('AUTHORIZATION_REVOKED',result['stdout'])
        self.assertEqual(result['extractor_calls'],[])       # refused before extraction

    def test_revocation_after_facts_were_generated_withholds_them(self):
        # Call order: CLI check, review read gate, file read, chunk extract, delivery check.
        # The extractor runs and produces SECRETMARK, then the final check revokes.
        result=self._run(side_effect=(True,True,True,True,False))
        self.assertEqual(result['code'],4)
        self.assertEqual(result['extractor_calls'],['README.md'])   # a fact was really generated
        self.assertEqual(result['content'],'PREVIOUS')
        self.assertNotIn('SECRETMARK',result['stdout'])

    def test_allow_partial_cannot_bypass_revocation(self):
        result=self._run(extra=('--allow-partial',),side_effect=(True,True,True,True,False))
        self.assertEqual(result['code'],4)
        self.assertEqual(result['extractor_calls'],['README.md'])
        self.assertEqual(result['content'],'PREVIOUS')
        self.assertNotIn('SECRETMARK',result['stdout'])


if __name__=='__main__':
    unittest.main()
