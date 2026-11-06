"""Tests for the drill gate and the "every drill is gated" invariant.

The invariant exists because a release candidate audited earlier had 12 of 26 drill scripts
that recorded ``pass`` fields but always exited 0 — the verdicts were never consumed.
"""
import io
import json
from pathlib import Path
import re
import unittest

from tests import drill_gate

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / 'tests'

# Scripts that only sample or measure and deliberately have no pass/fail verdict to consume.
MEASUREMENT_ONLY = {
    'wifi_poll.py',          # 采样 Wi-Fi 出口源地址，无阈值
    'jetstream_experiment.py',  # 时延/重投行为采样
}
# load_capacity.py 不再属于"仅采样"：它现在带 `--threshold`（按 V1.2 PERF-01/02 的
# 原文阈值判定并作为退出码）；不带该开关时仍是采样模式、不产生闸门。


def _drill_scripts():
    for path in sorted(TESTS.glob('*.py')):
        if path.name.startswith('test_') or path.name in {'drill_gate.py', '__init__.py'}:
            continue
        yield path


class DrillGateTests(unittest.TestCase):
    def test_explicit_verdicts_are_consumed(self):
        checks = [{'check': 'baseline', 'pass': True},
                  {'check': 'quorum', 'result': 'ok'},
                  {'check': 'majority down', 'pass': False},
                  {'check': 'no majority', 'result': 'FAIL TimeoutError'}]
        failures = drill_gate.judge(checks)
        self.assertEqual([label for label, _ in failures], ['majority down', 'no majority'])
        self.assertEqual(drill_gate.finish('demo', checks, stream=io.StringIO()), 1)

    def test_informational_values_are_not_failures(self):
        checks = [{'check': 'offline task before outage', 'state': 'accepted'},
                  {'check': 'outage seconds', 'value': 60.2},
                  {'check': 'reconnect within 30s', 'value': 12.5},
                  {'check': 'released commit', 'ok': True}]
        self.assertEqual(drill_gate.judge(checks), [])

    def test_dict_shaped_checks_are_supported(self):
        checks = {'key byte-identical': True, 'intermediate present': False}
        failures = drill_gate.judge(checks)
        self.assertEqual(failures, [('intermediate present', 'value=False')])

    def test_skip_and_blocked_are_separate_verdicts_not_passes(self):
        checks = [drill_gate.record('a', drill_gate.PASS),
                  drill_gate.skipped('b', '目标离线'),
                  drill_gate.blocked('c', '环境缺代理'),
                  drill_gate.record('d', drill_gate.FAIL)]
        self.assertEqual(drill_gate.judge(checks), [('d', 'pass=False')])
        counts = drill_gate.tally(checks)
        self.assertEqual((counts['passed'], counts['failed'], counts['skipped'], counts['blocked']),
                         (1, 1, 1, 1))
        for entry in (drill_gate.skipped('x', 'y'), drill_gate.blocked('x', 'y')):
            self.assertNotIn('pass', entry)          # cannot be summed into a pass count
            self.assertIn(entry['status'], (drill_gate.SKIP, drill_gate.BLOCKED))
        self.assertEqual(drill_gate.status_of(drill_gate.skipped('x', 'y')), drill_gate.SKIP)
        self.assertEqual(drill_gate.status_of(drill_gate.blocked('x', 'y')), drill_gate.BLOCKED)

    def test_legacy_skip_detail_still_reads_as_skip(self):
        legacy = {'check': 'H', 'pass': True, 'detail': 'SKIP: 未提供 SKILL_MAC_SSH'}
        self.assertEqual(drill_gate.status_of(legacy), drill_gate.SKIP)
        self.assertEqual(drill_gate.judge([legacy]), [])
        self.assertEqual(drill_gate.tally([legacy])['passed'], 0)
        self.assertEqual(drill_gate.tally([legacy])['skipped'], 1)

    def test_finish_reports_all_four_counts(self):
        checks = [drill_gate.record('a', drill_gate.PASS), drill_gate.skipped('b', '离线'),
                  drill_gate.record('c', drill_gate.PASS)]
        out = io.StringIO()
        self.assertEqual(drill_gate.finish('demo', checks, stream=out), 0)
        text = out.getvalue()
        self.assertIn('PASS 2 / FAIL 0 / SKIP 1 / BLOCKED 0', text)
        self.assertIn('[drill-gate] OK', text)

    def test_all_skip_evidence_is_not_a_pass_for_the_record_refresh(self):
        from scripts.refresh_project_record import _counts
        payload = {'checks': [drill_gate.skipped('a', '离线'), drill_gate.skipped('b', '离线')]}
        self.assertEqual(_counts(payload, payload['checks']), (0, 2))

    def test_gate_from_evidence_reads_results_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'results.json').write_text(json.dumps({'checks': [{'check': 'x', 'pass': False}]}))
            self.assertEqual(drill_gate.gate_from_evidence('demo', path, stream=io.StringIO()), 1)

    def test_every_drill_is_gated_or_documented_as_measurement(self):
        """No script may record verdicts without consuming them (the A-to-A lesson)."""
        ungated = []
        for path in _drill_scripts():
            text = path.read_text(encoding='utf-8')
            gated = 'drill_gate' in text or re.search(r'\bassert\b|\bsys\.exit\b|\braise\b', text)
            if not gated and path.name not in MEASUREMENT_ONLY:
                ungated.append(path.name)
        self.assertEqual(ungated, [], '未接闸门且未标注为"仅采样"的演练：' + ', '.join(ungated))

    def test_measurement_only_list_is_accurate(self):
        for name in MEASUREMENT_ONLY:
            text = (TESTS / name).read_text(encoding='utf-8')
            self.assertNotIn('drill_gate', text, '%s 已接闸门，请从 MEASUREMENT_ONLY 移除' % name)


if __name__ == '__main__':
    unittest.main()
