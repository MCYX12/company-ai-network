"""Published-record refresh: derive-only-facts, publish-only-on-change, and freshness alerting."""
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.healthcheck import local_checks
from app.project_records import validate
from scripts.refresh_project_record import (AUTO_PREFIX, CONFIRM_PREFIX, comparable, confirm,
                                            derive, facts_digest, load_meta, publish, save_meta)

SOURCE = {'schema_version': 1, 'project_id': 'demo-project', 'source_id': 'x/status',
          'updated_at': '2026-10-01T00:00:00+00:00', 'summary': '人工叙述',
          'completed': ['人工条目一', AUTO_PREFIX + '旧快照：HEAD=deadbee'],
          'blockers': ['人工阻塞'], 'evidence': ['docs/A.md', 'evidence/old-drill', 'evidence/old-drill/results.json']}


def facts(**overrides):
    value = {'head': 'abc1234', 'tag': 'v1.2.3', 'dirty': False, 'online': 7, 'visible': 7,
             'evidence': [{'name': 'drill-20261005T000000Z', 'passed': 3, 'total': 3}]}
    value.update(overrides)
    return value


class DeriveTests(unittest.TestCase):
    def test_human_fields_are_preserved_and_the_auto_line_is_replaced(self):
        record = derive(SOURCE, facts(), now=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(record['summary'], '人工叙述')
        self.assertEqual(record['blockers'], ['人工阻塞'])
        self.assertEqual([item for item in record['completed'] if item.startswith(AUTO_PREFIX)],
                         ['[自动快照] 2026-10-05T12:00Z：HEAD=abc1234、最新 tag v1.2.3、在线 7/7（health/latest.json）'
                          '、最新证据 drill-20261005T000000Z（3/3）'])
        self.assertIn('人工条目一', record['completed'])
        self.assertEqual(record['updated_at'], SOURCE['updated_at'])  # derive never touches the timestamp

    def test_auto_line_reports_uncommitted_state_and_missing_facts(self):
        line = derive(SOURCE, facts(dirty=True, tag='', online=None, visible=None, evidence=[]),
                      now=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc))['completed'][-1]
        self.assertIn('工作树有未提交改动', line)
        self.assertNotIn('最新 tag', line)
        self.assertNotIn('在线', line)

    def test_evidence_is_deduped_and_only_cites_paths_that_exist(self):
        record = derive(SOURCE, facts(evidence=[{'name': 'docs', 'passed': 1, 'total': 1}]),)
        self.assertIn('docs/A.md', record['evidence'])
        self.assertNotIn('evidence/old-drill', record['evidence'])          # human evidence/ paths are rebuilt
        self.assertNotIn('evidence/nonexistent-drill', record['evidence'])
        self.assertEqual(len(record['evidence']), len(set(record['evidence'])))

    def test_change_detection_ignores_the_timestamp(self):
        record = derive(SOURCE, facts())
        same = dict(record, updated_at='2030-01-01T00:00:00+00:00')
        self.assertEqual(comparable(record), comparable(same))
        changed = dict(record, summary='改过了')
        self.assertNotEqual(comparable(record), comparable(changed))


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_publish_is_private_keeps_a_backup_and_stays_valid(self):
        record = derive(SOURCE, facts())
        record['updated_at'] = datetime.now(timezone.utc).isoformat()
        target, _size = publish(record, self.runtime)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        validate(json.loads(target.read_text()), 'demo-project')
        publish(dict(record, summary='第二次发布'), self.runtime)
        backups = list(target.parent.glob('demo-project.json.bak-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text())['summary'], '人工叙述')

    def test_invalid_record_is_refused_before_any_write(self):
        record = derive(SOURCE, facts())
        record['updated_at'] = '2026-10-05T00:00:00'           # no timezone
        with self.assertRaises(ValueError):
            validate(record, 'demo-project')
        self.assertEqual(list(self.runtime.rglob('*.json')), [])


class ConfirmTests(unittest.TestCase):
    """Quiet periods cost one command, and the published file shows it was a human statement."""

    def test_confirmation_requires_a_note_and_is_recorded_verbatim(self):
        for bad in ['', '   ', 'x' * 201, None]:
            with self.assertRaises(ValueError):
                confirm(derive(SOURCE, facts()), bad)
        record = confirm(derive(SOURCE, facts()), '只跑了验收，状态未变',
                         now=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc))
        line = [item for item in record['completed'] if item.startswith(CONFIRM_PREFIX)]
        self.assertEqual(line, ['[人工确认] 2026-10-05T12:00Z：状态未变（只跑了验收，状态未变）'])

    def test_only_the_newest_confirmations_are_kept(self):
        record = derive(SOURCE, facts())
        for index in range(5):
            record = confirm(record, f'第 {index} 次确认', now=datetime(2026, 10, 5, index, tzinfo=timezone.utc))
        kept = [item for item in record['completed'] if item.startswith(CONFIRM_PREFIX)]
        self.assertEqual(len(kept), 3)
        self.assertIn('第 4 次确认', kept[-1])

    def test_confirmation_only_adds_a_human_line_and_leaves_the_facts_alone(self):
        base = derive(SOURCE, facts())
        after = confirm(base, '状态未变')
        self.assertEqual([i for i in after['completed'] if i.startswith(AUTO_PREFIX)],
                         [i for i in base['completed'] if i.startswith(AUTO_PREFIX)])
        self.assertEqual(after['evidence'], base['evidence'])
        self.assertEqual(after['summary'], base['summary'])
        self.assertEqual(len(after['completed']), len(base['completed']) + 1)


class FreshnessAlertTests(unittest.TestCase):
    def _runtime_with_age(self, hours):
        tmp = tempfile.TemporaryDirectory()
        runtime = Path(tmp.name)
        (runtime / 'project-records').mkdir()
        record = derive(SOURCE, facts())
        record['updated_at'] = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        (runtime / 'project-records/demo-project.json').write_text(json.dumps(record, ensure_ascii=False))
        return tmp, runtime

    def _stage(self, runtime):
        return next(c for c in local_checks(runtime) if c['check'] == 'project_record:demo-project')

    def test_warns_before_the_six_hour_window_closes(self):
        tmp, runtime = self._runtime_with_age(5.5)   # 6h 窗口，提前到 5h 提醒
        try:
            stage = self._stage(runtime)
            self.assertEqual(stage['status'], 'warn')
            self.assertEqual(stage['detail']['freshness'], 'current')     # still current, but refresh now
            self.assertEqual(stage['detail']['fresh_window_hours'], 6.0)
            self.assertIn('publish_record.sh', stage['detail']['refresh_with'])
        finally:
            tmp.cleanup()

    def test_fresh_record_is_ok_and_day_old_record_fails(self):
        for hours, expected in ((1, 'ok'), (25, 'fail')):
            tmp, runtime = self._runtime_with_age(hours)
            try:
                self.assertEqual(self._stage(runtime)['status'], expected, hours)
            finally:
                tmp.cleanup()

    def test_missing_record_warns_instead_of_staying_silent(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            stage = next(c for c in local_checks(Path(tmp.name)) if c['check'] == 'project_records')
            self.assertEqual(stage['status'], 'warn')
            self.assertEqual(stage['detail']['published'], 0)
        finally:
            tmp.cleanup()


class StableComparisonTests(unittest.TestCase):
    """A later minute with the same facts is not a change (assessment 2026-10-05)."""

    def test_repeat_run_in_a_later_minute_compares_equal(self):
        early = derive(SOURCE, facts(), now=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc))
        later = derive(SOURCE, facts(), now=datetime(2026, 10, 5, 13, 30, tzinfo=timezone.utc))
        self.assertNotEqual(early['completed'][-1], later['completed'][-1])  # the clock did move
        self.assertEqual(comparable(early), comparable(later))               # the facts did not

    def test_a_real_fact_change_still_compares_different(self):
        self.assertNotEqual(comparable(derive(SOURCE, facts())),
                            comparable(derive(SOURCE, facts(head='beef123'))))
        self.assertNotEqual(comparable(derive(SOURCE, facts())),
                            comparable(derive(SOURCE, facts(online=6))))

    def test_facts_digest_is_stable_and_reflects_only_facts(self):
        self.assertEqual(facts_digest(facts()), facts_digest(dict(facts())))
        self.assertNotEqual(facts_digest(facts()), facts_digest(facts(head='beef123')))
        moved = derive(SOURCE, facts(), now=datetime(2026, 10, 5, 23, 59, tzinfo=timezone.utc))
        self.assertEqual(facts_digest(facts()), facts_digest(facts()))
        self.assertEqual(comparable(moved), comparable(derive(SOURCE, facts())))


class RefreshMainTests(unittest.TestCase):
    """The three clocks: observation, human confirmation, and publication."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        self.source = self.root / 'demo-project.json'
        self.source.write_text(json.dumps(SOURCE, ensure_ascii=False))
        self.current = facts()
        # side_effect reads the current value, so a test can change the observed facts mid-run
        self.patch = patch('scripts.refresh_project_record.collect_facts', side_effect=lambda **_: self.current)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def run_refresh(self, *argv):
        import scripts.refresh_project_record as module
        with patch.object(sys, 'argv', ['refresh_project_record', '--source', str(self.source),
                                        '--publish', str(self.runtime), *argv]), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(), 0)
        folder = self.runtime / 'project-records'
        record = json.loads((folder / 'demo-project.json').read_text(encoding='utf-8'))
        return record, load_meta(folder, 'demo-project')

    def test_unchanged_cycle_does_not_pseudo_update_the_record(self):
        record, meta = self.run_refresh()
        first_updated = record['updated_at']
        self.assertIsNone(meta['confirmed_at'])
        self.assertIsNotNone(meta['observed_at'])          # the observation clock is recorded separately
        record2, meta2 = self.run_refresh()
        self.assertEqual(record2, record)                  # byte-for-byte the same record
        self.assertEqual(record2['updated_at'], first_updated)
        self.assertIsNotNone(meta2['observed_at'])

    def test_only_a_human_confirmation_re_stamps_the_written_record(self):
        record, _meta = self.run_refresh()
        first_updated = record['updated_at']
        confirmed, meta = self.run_refresh('--confirm', '状态未变')
        self.assertNotEqual(confirmed['updated_at'], first_updated)
        self.assertEqual(meta['confirmed_at'], confirmed['updated_at'])
        self.assertTrue(any(item.startswith(CONFIRM_PREFIX) and '状态未变' in item
                            for item in confirmed['completed']))

    def test_auto_fact_change_keeps_the_human_confirmation(self):
        first, meta = self.run_refresh('--confirm', '只跑了验收，状态未变')
        confirmed_at = meta['confirmed_at']
        self.current = facts(head='beef123')               # a real machine fact changed
        second, meta2 = self.run_refresh()
        self.assertNotEqual(second['updated_at'], first['updated_at'])
        self.assertIn('HEAD=beef123', second['completed'][-1])
        self.assertEqual(meta2['confirmed_at'], confirmed_at)   # automation did not re-confirm for the human
        self.assertTrue(any(item.startswith(CONFIRM_PREFIX) and '状态未变' in item
                            for item in second['completed']))
        # the confirmation exists only in the runtime record, never in the publish source
        self.assertNotIn(CONFIRM_PREFIX, json.dumps(json.loads(self.source.read_text(encoding='utf-8'))))

    def test_confirm_note_lands_even_when_the_facts_also_changed(self):
        self.current = facts(head='beef123')
        record, meta = self.run_refresh('--confirm', '事实与叙述都更新')
        self.assertEqual(meta['confirmed_at'], record['updated_at'])
        self.assertTrue(any(item.startswith(CONFIRM_PREFIX) and '事实与叙述都更新' in item
                            for item in record['completed']))
        self.assertIn('HEAD=beef123', record['completed'][-1])

    def test_confirm_is_refused_without_a_note(self):
        import scripts.refresh_project_record as module
        with patch.object(sys, 'argv', ['refresh_project_record', '--source', str(self.source),
                                        '--publish', str(self.runtime), '--confirm', '   ']):
            with self.assertRaises(SystemExit):
                module.main()


class NarrativeClockTests(unittest.TestCase):
    """A fact refresh must not present stale narrative as freshly confirmed."""

    def _runtime(self, record_age_hours, confirmed_age_hours=None, write_meta=True):
        tmp = tempfile.TemporaryDirectory()
        runtime = Path(tmp.name)
        folder = runtime / 'project-records'
        folder.mkdir()
        now = datetime.now(timezone.utc)
        record = derive(SOURCE, facts())
        record['updated_at'] = (now - timedelta(hours=record_age_hours)).isoformat()
        (folder / 'demo-project.json').write_text(json.dumps(record, ensure_ascii=False))
        if write_meta:
            confirmed = None if confirmed_age_hours is None else (now - timedelta(hours=confirmed_age_hours)).isoformat()
            save_meta(folder, 'demo-project', {'confirmed_at': confirmed})
        return tmp, runtime

    def _stage(self, runtime):
        return next(c for c in local_checks(runtime) if c['check'] == 'project_record:demo-project')

    def test_old_narrative_is_not_hidden_by_a_fresh_fact_republish(self):
        tmp, runtime = self._runtime(0.1, confirmed_age_hours=30)
        try:
            stage = self._stage(runtime)
            self.assertEqual(stage['status'], 'fail')
            self.assertEqual(stage['detail']['narrative_clock'], 'confirmed_at')
            self.assertEqual(stage['detail']['narrative_age_hours'], 30.0)
            self.assertIn('未再确认', stage['detail']['note'])
        finally:
            tmp.cleanup()

    def test_recent_confirmation_stays_ok_and_sidecar_is_not_a_record(self):
        tmp, runtime = self._runtime(0.1, confirmed_age_hours=1)
        try:
            checks = local_checks(runtime)
            names = [c['check'] for c in checks]
            self.assertNotIn('project_record:demo-project.meta', names)
            stage = self._stage(runtime)
            self.assertEqual(stage['status'], 'ok')
            self.assertEqual(stage['detail']['narrative_clock'], 'confirmed_at')
        finally:
            tmp.cleanup()

    def test_mirror_state_sidecar_is_not_a_record(self):
        tmp, runtime = self._runtime(1, write_meta=False)
        try:
            (runtime / 'project-records' / '.mirror-state.json').write_text('{"pending":{}}')
            names = [c['check'] for c in local_checks(runtime)]
            self.assertNotIn('project_record:.mirror-state', names)
            self.assertEqual(self._stage(runtime)['status'], 'ok')
        finally:
            tmp.cleanup()

    def test_without_a_sidecar_the_record_clock_is_used(self):
        tmp, runtime = self._runtime(1, write_meta=False)
        try:
            stage = self._stage(runtime)
            self.assertEqual(stage['status'], 'ok')
            self.assertEqual(stage['detail']['narrative_clock'], 'updated_at')
            self.assertEqual(stage['detail']['confirmed_at'], None)
        finally:
            tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
