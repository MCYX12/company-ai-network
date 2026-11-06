"""Adjudication calculator: canonical labels, intersection recall, completeness and exit codes."""
import unittest

from scripts import adjudicate_review


def fact(fid,verdict,**extra):
    return {'fact_id':fid,'verdict':verdict,**extra}


class ComputeTests(unittest.TestCase):
    def test_metrics_use_all_outputs_and_intersection_recall(self):
        adjudication={'schema_version':1,'important_facts':['a.md:l1','a.md:l2'],'facts':[
            fact('1','supported',citation_ok=True,label_id='a.md:l1'),
            fact('2','derived',citation_ok=True,label_id='a.md:l2'),
            fact('3','duplicate',citation_ok=False),
            fact('4','unsupported',citation_ok=True)]}
        result=adjudicate_review.compute(adjudication,gold_labels=['a.md:l1','a.md:l2'])
        self.assertEqual(result['metrics']['total_facts'],4)
        self.assertEqual(result['metrics']['non_redundant_precision'],0.5)   # 2 of 4 outputs
        self.assertEqual(result['metrics']['key_recall'],1.0)                # intersection
        self.assertEqual(result['metrics']['citation_correctness'],0.75)
        self.assertIsNone(result['pass'])                                    # no thresholds -> computed

    def test_legacy_label_form_is_canonicalised(self):
        adjudication={'schema_version':1,'facts':[fact('1','supported',label_id='l1@a.md')]}
        result=adjudicate_review.compute(adjudication,gold_labels=['a.md:l1'])
        self.assertEqual(result['metrics']['key_recall'],1.0)

    def test_unknown_label_and_missing_verdict(self):
        with self.assertRaises(ValueError):
            adjudicate_review.compute({'schema_version':1,'facts':[fact('1','supported',label_id='x.md:l9')]},
                                      gold_labels=['a.md:l1'])
        result=adjudicate_review.compute({'schema_version':1,'facts':[{'fact_id':'1','citation_ok':False}]})
        self.assertEqual(result['metrics']['undecided'],1)
        self.assertEqual(result['metrics']['counts']['unresolved'],1)

    def test_completeness_uniqueness_and_threshold_errors(self):
        with self.assertRaises(ValueError):
            adjudicate_review.compute({'schema_version':1,'facts':[]},
                                      report_facts_list=[{'fact_id':'f01'},{'fact_id':'f02'}])
        with self.assertRaises(ValueError):
            adjudicate_review.compute({'schema_version':1,'facts':[fact('1','supported'),fact('1','supported')]})
        with self.assertRaises(ValueError):
            adjudicate_review.compute({'schema_version':1,'facts':[fact('1','supported')]},
                                      thresholds={'non_redundant_precision':1.5})

    def test_required_metric_unknown_fails(self):
        adjudication={'schema_version':1,'facts':[fact('1','supported')]}
        result=adjudicate_review.compute(adjudication,thresholds={'key_recall':1.0})
        self.assertFalse(result['pass'])
        self.assertIn('REQUIRED_METRIC_UNKNOWN_KEY_RECALL',result['failures'])

    def test_report_binding_rejects_content_and_hash_mismatch(self):
        facts=[{'fact_id':'f01','verdict':'supported','citation_ok':True,'label_id':'a.md:l1',
                'summary':'alpha','file':'a.md','evidence_ids':['E1']}]
        report=[{'fact_id':'f01','file':'a.md','summary':'alpha','evidence_ids':['E1']}]
        adjudication={'schema_version':1,'facts':facts,'report_hash':adjudicate_review.canonical_hash(report)}
        adjudicate_review.compute(adjudication,gold_labels=['a.md:l1'],report_facts_list=report)
        changed=dict(adjudication);changed['report_hash']='deadbeef'
        with self.assertRaises(ValueError):
            adjudicate_review.compute(changed,gold_labels=['a.md:l1'],report_facts_list=report)
        missing={k:v for k,v in adjudication.items() if k!='report_hash'}
        with self.assertRaises(ValueError):
            adjudicate_review.compute(missing,gold_labels=['a.md:l1'],report_facts_list=report)
        wrong=dict(adjudication);wrong['facts']=[dict(facts[0],summary='beta')]
        with self.assertRaises(ValueError):
            adjudicate_review.compute(wrong,gold_labels=['a.md:l1'],report_facts_list=report)
        evidence=dict(adjudication);evidence['facts']=[dict(facts[0],evidence_ids=['E9'])]
        with self.assertRaises(ValueError):
            adjudicate_review.compute(evidence,gold_labels=['a.md:l1'],report_facts_list=report)

    def test_dataset_documents_and_labels_hash_are_verified(self):
        import hashlib, json, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            doc=folder/'a.md';doc.write_text('# a\n')
            doc_hash=hashlib.sha256(doc.read_bytes()).hexdigest()[:32]
            manifest={'dataset_id':'d','docs':[{'name':'a.md','sha256':doc_hash}]}
            facts=[{'fact_id':'f01','verdict':'supported','citation_ok':True,'label_id':'a.md:l1',
                    'file':'a.md','summary':'alpha','evidence_ids':['E1']}]
            report=[{'fact_id':'f01','file':'a.md','summary':'alpha','evidence_ids':['E1']}]
            adjudication={'schema_version':1,'facts':facts,
                          'report_hash':adjudicate_review.canonical_hash(report),
                          'dataset_hash':adjudicate_review.canonical_hash(manifest),
                          'labels_hash':'abc'}
            base={'gold_labels':['a.md:l1'],'report_facts_list':report,'dataset':manifest,
                  'labels_hash':'abc','dataset_dir':folder}
            adjudicate_review.compute(adjudication,**base)
            doc.write_text('# tampered\n')
            with self.assertRaises(ValueError):
                adjudicate_review.compute(adjudication,**base)
            doc.write_text('# a\n')
            with self.assertRaises(ValueError):
                adjudicate_review.compute({**adjudication,'labels_hash':'other'},**base)

    def test_pass_only_with_thresholds(self):
        adjudication={'schema_version':1,'facts':[fact('1','supported',citation_ok=True,label_id='a.md:l1')]}
        result=adjudicate_review.compute(adjudication,thresholds={'non_redundant_precision':0.9,
                                                                  'citation_correctness':1.0,
                                                                  'key_recall':1.0},
                                         gold_labels=['a.md:l1'])
        self.assertTrue(result['pass'])
        self.assertEqual(result['status'],'pass')


if __name__=='__main__':
    unittest.main()
