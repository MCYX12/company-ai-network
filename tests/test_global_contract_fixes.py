"""Enterprise review: exercise real consumers and failure boundaries, not helper existence."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from app import authz, registry
from app.history_access import HistoryAccess
from app.model_budget import ModelBudget
from app.task_store import TaskStore
from app.kv_task_store import KvTaskStore
from scripts import multihost_plan, ops_drill, retention, runtime_backup, oidc_sync
from test_kv_task_store import FakeKv, FakeJs
from test_multihost_plan import PROFILE, HOSTS


class LocalContracts(unittest.TestCase):
    def test_retention_preserves_new_and_old_explicit_denial(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=False):
            os.environ.pop('POC_AUTHZ_RUNTIME', None)
            base = Path(root)
            history = HistoryAccess(base / 'history-access.sqlite3')
            for caller in ('new', 'old'):
                row = history.request(caller, 'target')
                history.decide(row['id'], 'denied', 60, 'admin', 'explicit veto')
            with history.db() as db:
                db.execute("UPDATE requests SET created=1 WHERE caller='old'")
            for caller in ('new', 'old'):
                self.assertFalse(authz.decide(caller, 'target', 'history', runtime=base)['allowed'])
            self.assertEqual(retention.plan(base)['history_requests'], 0)
            result = retention.apply(base, confirm=True)
            self.assertEqual(result['deleted']['history_requests'], 0)
            for caller in ('new', 'old'):
                self.assertFalse(authz.decide(caller, 'target', 'history', runtime=base)['allowed'])

    def test_oidc_cli_writes_shared_authority(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); local = root / 'local'; shared = root / 'shared'
            local.mkdir(); shared.mkdir(); token = root / 'token'; token.write_text('verified-fixture')
            class Provider:
                @classmethod
                def from_environment(cls, roster):
                    cls.roster = roster; return cls()
                def sync(self, _token):
                    self.roster.set('employee', 'suspended', source='oidc')
                    return {'synced': True}
            with patch.dict(os.environ, {'POC_AUTHZ_RUNTIME': str(shared)}), patch.object(oidc_sync, 'OidcProvider', Provider), patch('sys.argv', ['oidc_sync','--runtime',str(local),'--token-file',str(token),'--apply']):
                self.assertEqual(oidc_sync.main(), 0)
            from app.iam import RosterStore
            self.assertEqual(RosterStore(shared / 'roster.sqlite3').status('employee'), 'suspended')
            self.assertFalse((local / 'roster.sqlite3').exists())

    def test_default_budget_and_authz_use_same_shared_authority(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);shared=root/'authority';local=root/'local';shared.mkdir();local.mkdir()
            with patch.dict(os.environ,{'POC_AUTHZ_RUNTIME':str(shared),'POC_MODEL_CALL_BUDGET':'1'}), patch('app.model_budget.RUNTIME',local):
                budget=ModelBudget(limit=1);budget.spend('caller','target')
                self.assertFalse(authz.decide('caller','target','model_egress',runtime=local,require_conversation=False)['allowed'])
            self.assertFalse((local/'model-budget.sqlite3').exists())

    def test_missing_shared_registry_cannot_use_local_or_override(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); local = root / 'local'; shared = root / 'shared'
            local.mkdir(); shared.mkdir()
            (local / 'registry.json').write_text('{"agents":[]}')
            with patch.dict(os.environ, {'POC_AUTHZ_RUNTIME': str(shared), 'AGENT_REGISTRY': str(local/'registry.json')}), patch.object(registry, 'RUNTIME', local):
                self.assertEqual(registry.registry_path(), shared / 'registry.json')
                with self.assertRaises(FileNotFoundError):registry.load_registry()

    def test_generated_ha_env_passes_real_router_gate(self):
        with tempfile.TemporaryDirectory() as root:
            shared = Path(root) / 'authority'; shared.mkdir()
            (shared / 'registry.json').write_text('{"schema_version":1,"agents":[]}')
            plan = multihost_plan.render_plan(PROFILE, HOSTS, authz_runtime=str(shared))
            env = dict(line.split('=',1) for line in plan['files']['router/r1/env'].splitlines())
            class NC:
                def jetstream(self):return object()
            from app.router import Router
            with patch.dict(os.environ, env), patch('app.router.RUNTIME', Path(root)):
                router = Router(NC())
                self.assertTrue(router.ha)
                self.assertIsInstance(router.tasks, KvTaskStore)
            self.assertIn(str(shared), plan['files']['router/r1/company-agent-router.service'])
            self.assertIn('test -r', plan['commands']['r1'][0])

    def test_alert_id_dedup_and_crash_receipt_repair(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / 'alerts.jsonl').write_text('{"id":"same","text":"original"}\n')
            first = ops_drill.alert_delivery(base, {'id':'same','text':'original','checked_at':1})
            second = ops_drill.alert_delivery(base, {'id':'same','text':'original','checked_at':2})
            self.assertTrue(first['delivered']); self.assertTrue(second['deduplicated'])
            self.assertEqual(second['receipts'], 1)
            self.assertEqual(len((base/'alerts.jsonl').read_text().splitlines()),1)
            self.assertEqual(json.loads((base/'receipts.jsonl').read_text())['alert'],'same')
            (base/'receipts.jsonl').unlink()
            self.assertEqual(ops_drill.alert_delivery(base, {'id':'same'})['receipts'],1)
            self.assertTrue((base/'receipts.jsonl').exists())

    def test_backup_restores_budget_and_shared_roster(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); local = root/'local'; shared=root/'authority'
            local.mkdir(); shared.mkdir()
            TaskStore(local/'tasks.sqlite3')
            budget = ModelBudget(local/'model-budget.sqlite3',limit=1)
            budget.spend('caller','target'); self.assertFalse(budget.allow('caller','target'))
            from app.iam import RosterStore
            RosterStore(shared/'roster.sqlite3').set('employee','left',source='manual')
            with patch.dict(os.environ, {'POC_AUTHZ_RUNTIME':str(shared)}):
                manifest = runtime_backup.backup(local,root/'snapshot')
            names={row['name'] for row in manifest['files']}
            self.assertIn('authority/roster.sqlite3',names)
            self.assertIn('model-budget.sqlite3',names)
            result = runtime_backup.restore(root/'snapshot',root/'restored')
            self.assertEqual(result['ledgers']['employment'],1)
            self.assertFalse(ModelBudget(root/'restored/model-budget.sqlite3',limit=1).allow('caller','target'))
            self.assertEqual(RosterStore(root/'restored/authority/roster.sqlite3').status('employee'),'left')

    def test_ha_backup_refuses_missing_jetstream(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'POC_HA':'1'}):
            base=Path(root); TaskStore(base/'tasks.sqlite3')
            with self.assertRaisesRegex(ValueError,'HA backup requires'):
                runtime_backup.backup(base,base/'snapshot')

    def test_incomplete_backup_cli_is_nonzero(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);TaskStore(base/'tasks.sqlite3')
            (base/'unknown.sqlite3').write_bytes(b'unknown')
            with patch('sys.argv',['backup','backup','--runtime',str(base),'--output',str(base/'snapshot')]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runtime_backup.main(),2)

    def test_backup_authority_layout_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);TaskStore(base/'tasks.sqlite3')
            runtime_backup.backup(base,base/'snapshot')
            path=base/'snapshot/manifest.json';manifest=json.loads(path.read_text())
            manifest['authority_layout']='../../other';path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):runtime_backup.restore(base/'snapshot',base/'restored')


class KvContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.kv = FakeKv(); self.store = KvTaskStore(FakeJs(self.kv))
        async def put(key, value):
            if key in self.kv.store:
                return await self.kv.update(key, value, last=self.kv.store[key][1])
            return await self.kv.create(key, value)
        self.kv.put = put

    async def test_compacted_task_remains_readable_and_scoped_and_idempotent(self):
        body={'projects':['project'], 'question':'secret' * 5000}
        row=await self.store.submit('caller','target','key','ask_peer',body,600)
        await self.store.finish(row['task_id'],{'code':'OK','data':'large'*5000})
        old=(await self.store.quota_status())['bucket_bytes']
        await self.store.enforce_quota(before=time.time()+1)
        current=await self.store.get(row['task_id'])
        self.assertEqual(self.store.public(current)['operation'],'ask_peer')
        self.assertEqual(json.loads(current['payload']),{'projects':['project']})
        self.assertEqual((await self.store.history(row['task_id']))[0]['event'],'content_purged')
        page=await self.store.list_owned('caller',lambda r: json.loads(r['payload'])['projects']==['project'])
        self.assertEqual(page['tasks'][0]['task_id'], row['task_id'])
        self.assertEqual((await self.store.submit('caller','target','key','ask_peer',body,600))['task_id'],row['task_id'])
        with self.assertRaisesRegex(ValueError,'IDEMPOTENCY_CONFLICT'):
            await self.store.submit('caller','target','key','ask_peer',{'projects':['different']},600)
        self.assertLess((await self.store.quota_status())['bucket_bytes'],old)

    async def test_release_write_failure_reports_failure_then_recovers_same_child(self):
        root=await self.store.submit('caller','target','root','ask_peer',{'projects':['p']},600)
        original=self.kv.update
        async def update(key,value,last=None):
            if key.startswith('t.'):
                raise ConnectionError('only child release write down')
            return await original(key,value,last)
        self.kv.update=update
        with self.assertRaisesRegex(ValueError,'RETRY_RELEASE_UNAVAILABLE'):
            await self.store.submit_retry(root['task_id'],'caller','target','ask_peer',{'projects':['p']},500)
        link=await self.store.retry_for(root['task_id'])
        child=await self.store.get(link['retry_task_id'])
        self.assertTrue(child['retry_pending'])
        self.assertNotIn(child['task_id'],[r['task_id'] for r in await self.store.due()])
        self.kv.update=original
        recovered=await self.store.submit_retry(root['task_id'],'caller','target','ask_peer',{'projects':['p']},400)
        self.assertEqual(recovered['task_id'],child['task_id']);self.assertFalse(recovered['retry_pending'])

    async def test_router_does_not_audit_scheduled_on_release_failure(self):
        from app.router import Router
        with tempfile.TemporaryDirectory() as root, patch('app.router.RUNTIME',Path(root)):
            router=Router(object());router.tasks=self.store
            audit=[];router.audit=lambda *values:audit.append(values)
            row=await self.store.submit('caller','target','root','ask_peer',{'projects':['p']},600)
            original=self.kv.update
            async def fail(key,value,last=None):
                if key.startswith('t.'):raise ConnectionError('child release down')
                return await original(key,value,last)
            self.kv.update=fail
            self.assertIsNone(await router._schedule_retry(row))
            self.assertTrue(any(values[1]=='RETRY_FAILED' for values in audit))
            self.assertFalse(any(values[1]=='RETRY_SCHEDULED' for values in audit))

    async def test_scheduler_recovers_pending_retry_without_result_redelivery(self):
        root=await self.store.submit('caller','target','root','ask_peer',{'projects':['p']},600)
        await self.store.finish(root['task_id'],{'code':'WORKER_INTERRUPTED','data':None})
        original=self.kv.update
        async def fail(key,value,last=None):
            if key.startswith('t.'):raise ConnectionError('release down')
            return await original(key,value,last)
        self.kv.update=fail
        with self.assertRaises(ValueError):
            await self.store.submit_retry(root['task_id'],'caller','target','ask_peer',{'projects':['p']},500)
        self.assertEqual(await self.store.due(),[])
        self.kv.update=original
        due=await self.store.due()
        self.assertEqual(len(due),1)
        self.assertEqual(due[0]['retry_of'],root['task_id'])
        self.assertFalse(due[0]['retry_pending'])
        self.assertEqual((await self.store.retry_for(root['task_id']))['retry_task_id'],due[0]['task_id'])

    async def test_old_tombstones_are_readable_without_inventing_authority(self):
        row=await self.store.submit('caller','target','key','ask_peer',{'projects':['p']},60)
        old={k:row[k] for k in ('task_id','owner','target','idempotency_key','fingerprint','created_at','deadline')}
        old.update(state='succeeded',result='{"code":"OK","purged":true}',compacted=True)
        await self.kv.update('t.'+row['task_id'],json.dumps(old).encode(),last=self.kv.store['t.'+row['task_id']][1])
        self.assertEqual(self.store.public(await self.store.get(row['task_id']))['operation'],'purged')
        self.assertEqual(await self.store.history(row['task_id']),[])
        self.assertEqual((await self.store.list_owned('caller',lambda r:r['operation']=='ask_peer'))['tasks'],[])

if __name__ == '__main__':unittest.main()
