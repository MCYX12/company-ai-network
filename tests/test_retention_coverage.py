"""Lifecycle selection protects control state, today budgets, held data and shared layouts."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app.history_access import HistoryAccess
from app.model_budget import ModelBudget
from scripts import retention

class LifecycleTests(unittest.TestCase):
    def test_shared_authority_and_today_budget_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);local=root/'local';authority=root/'auth';local.mkdir();authority.mkdir()
            history=HistoryAccess(authority/'history-access.sqlite3')
            row=history.request('caller','target');history.decide(row['id'],'denied',60,'admin','keep veto')
            budget=ModelBudget(authority/'model-budget.sqlite3',limit=1)
            budget.spend('caller','target');budget.spend('old','target',now=time.time()-400*86400)
            with patch.dict(os.environ,{'POC_AUTHZ_RUNTIME':str(authority)}):
                preview=retention.plan(local)
                self.assertEqual(preview['budget_usage'],1)
                applied=retention.apply(local,confirm=True)
            self.assertEqual(applied['deleted']['budget_usage'],1)
            self.assertFalse(budget.allow('caller','target'))
            self.assertEqual(history.listing()[0]['state'],'denied')
    def test_retired_archives_legal_hold_and_same_name(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in ('artifact-archive','project-archive','active'):(root/name).mkdir()
            for name in ('artifact-archive','project-archive','active'):
                path=root/name/'same.json';path.write_text('old');os.utime(path,(1,1))
            (root/'lifecycle.json').write_text(json.dumps({'retired_roots':[
                {'path':'artifact-archive','days':30,'legal_hold':False},
                {'path':'project-archive','days':30,'legal_hold':True}]}))
            self.assertEqual(len(retention.plan(root)['retired_files']),1)
            result=retention.apply(root,confirm=True)
            self.assertEqual(result['deleted']['retired_files'],1)
            self.assertTrue((root/'project-archive/same.json').exists())
            self.assertTrue((root/'active/same.json').exists())
    def test_invalid_policy_or_escape_fails_before_deleting(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'lifecycle.json').write_text(json.dumps({'retired_roots':[{'path':'../other','days':30,'legal_hold':False}]}))
            with self.assertRaises(ValueError):retention.apply(root,confirm=True)
            (root/'lifecycle.json').unlink()
            with self.assertRaises(ValueError):retention.plan(root,windows={'tasks_days':0})
    def test_unknown_database_is_explicit_coverage_blocker(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'unknown.sqlite3').write_bytes(b'unknown')
            coverage=retention.inventory(root)
            self.assertFalse(coverage['coverage_complete']);self.assertIn('unknown.sqlite3',coverage['blockers'][0])
    def test_ha_apply_cannot_silently_omit_kv(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'POC_HA':'1'}):
            with self.assertRaisesRegex(ValueError,'with-kv'):retention.apply(Path(folder),confirm=True)

if __name__=='__main__':unittest.main()
