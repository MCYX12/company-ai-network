"""Review quality scorer: matching, accuracy, recall and upgrade detection."""
import unittest

from scripts import score_review


class ScoreReviewTests(unittest.TestCase):
    def test_matches_recall_and_accuracy(self):
        report={'chunks':[{'file':'a.md','status':'processed','start_line':1,'end_line':2,'facts':[
            {'summary':'实测显示迁移后 P50 从 6 秒降到 3.5 秒','evidence_ids':['E1']},
            {'summary':'一条与标注无关的陈述','evidence_ids':['E1']}]}]}
        labels={'docs':[{'file':'a.md','facts':[
            {'id':'f1','text':'实测显示迁移后 P50 从 6 秒降到 3.5 秒','level':'measured'}]}]}
        result=score_review.score(report,labels)
        self.assertEqual(result['metrics']['important_facts'],1)
        self.assertEqual(result['metrics']['key_recall_heuristic'],1.0)
        self.assertEqual(result['metrics']['extracted_facts'],2)
        self.assertEqual(result['metrics']['heuristic_label_match_rate'],0.5)
        self.assertEqual(result['metrics']['citation_present_rate'],1.0)
        self.assertEqual(result['metrics']['upgrade_errors'],0)

    def test_upgrade_detected_and_negation_excluded(self):
        labels={'docs':[{'file':'a.md','facts':[
            {'id':'f1','text':'企业 IAM 接入计划','level':'planned'}]}]}
        upgraded={'chunks':[{'file':'a.md','status':'processed','facts':[
            {'summary':'企业 IAM 接入已完成并投入生产','evidence_ids':['E1']}]}]}
        self.assertEqual(score_review.score(upgraded,labels)['metrics']['upgrade_errors'],1)
        negated={'chunks':[{'file':'a.md','status':'processed','facts':[
            {'summary':'企业 IAM 接入已完成？但尚未通过验收','evidence_ids':['E1']}]}]}
        self.assertEqual(score_review.score(negated,labels)['metrics']['upgrade_errors'],0)


if __name__=='__main__':
    unittest.main()
