"""Benchmark math: percentiles, summaries and old/new deltas are pure and testable."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import bench_business


def attempt(category='short_confirm',wall=1.0,success=True,error=None,digested=0,omitted=0,
            truncated=False,answer_chars=10,query_code='OK',result_code='OK',timings=None,
            client_overhead_seconds=None):
    return {'category':category,'wall_seconds':wall,'success':success,'error':error,
            'digested':digested,'omitted':omitted,'truncated':truncated,
            'answer_chars':answer_chars,'query_code':query_code,'result_code':result_code,
            'timings':timings,'client_overhead_seconds':client_overhead_seconds}


class PercentileTests(unittest.TestCase):
    def test_percentiles_interpolate_and_handle_empty(self):
        self.assertIsNone(bench_business.percentile([],0.5))
        self.assertEqual(bench_business.percentile([4],0.95),4)
        self.assertAlmostEqual(bench_business.percentile([1,2,3,4],0.5),2.5)
        self.assertAlmostEqual(bench_business.percentile([1,2,3,4],0.95),3.85)


class SummaryTests(unittest.TestCase):
    def test_summary_keeps_failures_and_reports_coverage(self):
        attempts=[attempt(wall=1.0),attempt(wall=3.0),
                  attempt(category='project_progress',wall=5.0,success=False,error='TimeoutError: x'),
                  attempt(category='long_review',wall=9.0,digested=4,omitted=6,truncated=True,answer_chars=200)]
        summary=bench_business.summarize(attempts)
        self.assertEqual(summary['overall']['n'],4)
        self.assertEqual(summary['overall']['success'],3)
        self.assertEqual(summary['overall']['success_rate'],0.75)
        self.assertIn('TimeoutError: x',summary['overall']['errors'])
        self.assertEqual(summary['categories']['long_review']['digested_total'],4)
        self.assertEqual(summary['categories']['long_review']['omitted_total'],6)
        self.assertEqual(summary['categories']['long_review']['truncated'],1)

    def _report(self,p50,label='x'):
        return {'label':label,'target':'ag-a','samples':[{'id':'confirm','category':'short_confirm','question':'q'}],
                'meta':{'model':'m1'},'summary':{'categories':{'short_confirm':{
                    'p50_seconds':p50,'p95_seconds':p50*2,'success_rate':1.0,
                    'digested_total':2,'omitted_total':3,
                    'phases':{'total_seconds':{'p50_seconds':p50,'n':1,'unknown':0}}}}}}

    def test_compare_reads_the_run_report_summary_and_validates_consistency(self):
        delta=bench_business.compare_reports(self._report(10.0),self._report(7.0))['categories']['short_confirm']
        self.assertEqual(delta['p50_delta_percent'],-30.0)
        self.assertEqual(delta['coverage_digested_after'],2)
        self.assertEqual(delta['phase_p50_delta_percent']['total_seconds'],-30.0)

    def test_summary_reports_phase_timings_and_unknowns(self):
        attempts=[attempt(timings={'total_seconds':3.0,'harness_seconds':2.0,'model_seconds':1.0},
                          client_overhead_seconds=0.5),
                  attempt(timings={'total_seconds':5.0},client_overhead_seconds=1.5),
                  attempt(timings=None,client_overhead_seconds=None)]
        phases=bench_business.summarize(attempts)['overall']['phases']
        self.assertEqual(phases['total_seconds']['n'],2)
        self.assertEqual(phases['total_seconds']['unknown'],1)
        self.assertEqual(phases['total_seconds']['p50_seconds'],4.0)
        self.assertEqual(phases['model_seconds']['n'],1)
        self.assertEqual(phases['client_overhead_seconds']['p50_seconds'],1.0)

    def test_compare_refuses_mismatched_samples_meta_or_empty_categories(self):
        base_report=self._report(10.0)
        changed_meta=self._report(9.0);changed_meta['meta']={'model':'m2'}
        with self.assertRaises(ValueError):bench_business.compare_reports(base_report,changed_meta)
        changed_samples=self._report(9.0);changed_samples['samples']=[{'id':'other','category':'x','question':'q'}]
        with self.assertRaises(ValueError):bench_business.compare_reports(base_report,changed_samples)
        # Same id but a different question must not compare as the same sample.
        same_id=self._report(9.0);same_id['samples']=[{'id':'confirm','category':'short_confirm','question':'different'}]
        with self.assertRaises(ValueError):bench_business.compare_reports(base_report,same_id)
        empty_meta=self._report(9.0);empty_meta['meta']={}
        with self.assertRaises(ValueError):bench_business.compare_reports(base_report,empty_meta)
        with self.assertRaises(ValueError):bench_business.compare_reports({},self._report(9.0))

    def test_cmd_compare_accepts_run_output_and_rejects_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            old=Path(folder)/'old.json';new=Path(folder)/'new.json'
            old.write_text(json.dumps(self._report(10.0)))
            new.write_text(json.dumps(self._report(7.0)))
            self.assertEqual(bench_business.cmd_compare(SimpleNamespace(old=str(old),new=str(new))),0)
            old.write_text(json.dumps({}))
            self.assertNotEqual(bench_business.cmd_compare(SimpleNamespace(old=str(old),new=str(new))),0)

    def test_parse_meta_rejects_a_malformed_item(self):
        self.assertEqual(bench_business.parse_meta(['model=m1','material=abc']),{'model':'m1','material':'abc'})
        with self.assertRaises(ValueError):bench_business.parse_meta(['model'])


if __name__=='__main__':
    unittest.main()
