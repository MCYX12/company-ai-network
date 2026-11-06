"""PERF-01/02 threshold verdicts for the capacity drill (pure logic, no load).

Thresholds come from the V1.2 plan §16 and docs/CAPACITY_REPORT.md: PERF-01 is
1000 connections at 100 qps with 1 KiB read-only queries, success rate >= 99.9% and
P95 <= 500 ms; PERF-02 adds "no crash, no unbounded memory growth".
"""
import unittest

from tests.load_capacity import PERF01, perf01_checks


def scenario(**overrides):
    base = {'connections_requested': 1000, 'target_qps': 100, 'responses': 30000,
            'success_rate': 0.9995, 'codes': {'OK': 29985, 'TIMEOUT': 15},
            'latency_ms': {'p50': 21.0, 'p95': 480.0, 'p99': 610.0, 'max': 900.0},
            'router_rss_mb_before': 80.0, 'router_rss_mb_after': 96.0}
    base.update(overrides)
    return base


def verdict(checks, needle):
    matched = [c for c in checks if needle in c['check']]
    assert len(matched) == 1, matched
    return matched[0]


class PerfThresholdTests(unittest.TestCase):
    def test_plan_defaults_are_the_documented_numbers(self):
        self.assertEqual(PERF01['connections'], 1000)
        self.assertEqual(PERF01['qps'], 100)
        self.assertEqual(PERF01['success_rate_min'], 0.999)
        self.assertEqual(PERF01['p95_ms_max'], 500.0)

    def test_matching_load_passes_every_verdict(self):
        checks = perf01_checks(scenario())
        self.assertEqual([c['check'] for c in checks if c['pass'] is False], [])

    def test_small_sample_cannot_claim_perf01(self):
        """100 连接的样本即使全绿也不算 PERF-01 通过：形状单独判、且必须为 False。"""
        checks = perf01_checks(scenario(connections_requested=100, target_qps=50, latency_ms={'p95': 30.0}))
        self.assertFalse(verdict(checks, '负载形状')['pass'])
        self.assertTrue(verdict(checks, '成功率')['pass'])

    def test_success_rate_and_p95_failures_are_visible(self):
        low_rate = perf01_checks(scenario(success_rate=0.9980))
        self.assertFalse(verdict(low_rate, '成功率')['pass'])
        self.assertEqual(verdict(low_rate, '成功率')['timeouts'], 15)
        slow = perf01_checks(scenario(latency_ms={'p50': 40.0, 'p95': 620.0, 'p99': None, 'max': None}))
        self.assertFalse(verdict(slow, 'P95')['pass'])
        self.assertEqual(verdict(slow, 'P95')['p95_ms'], 620.0)

    def test_missing_measurements_fail_instead_of_passing(self):
        checks = perf01_checks(scenario(success_rate=None, latency_ms={}, router_rss_mb_after=None))
        self.assertFalse(verdict(checks, '成功率')['pass'])
        self.assertFalse(verdict(checks, 'P95')['pass'])
        self.assertFalse(verdict(checks, '未崩溃')['pass'])

    def test_overrides_replace_the_plan_defaults(self):
        checks = perf01_checks(scenario(success_rate=0.98, latency_ms={'p95': 900.0}),
                               success_rate_min=0.95, p95_ms_max=1000.0)
        self.assertEqual([c['check'] for c in checks if c['pass'] is False], [])

    def test_memory_growth_is_only_gated_when_a_limit_is_given(self):
        self.assertEqual([c['check'] for c in perf01_checks(scenario()) if '内存增长' in c['check']], [])
        over = perf01_checks(scenario(router_rss_mb_after=200.0), max_rss_growth_mb=50)
        self.assertFalse(verdict(over, '内存增长')['pass'])
        self.assertEqual(verdict(over, '内存增长')['rss_growth_mb'], 120.0)
        under = perf01_checks(scenario(), max_rss_growth_mb=50)
        self.assertTrue(verdict(under, '内存增长')['pass'])


if __name__ == '__main__':
    unittest.main()
