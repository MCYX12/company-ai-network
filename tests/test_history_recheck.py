"""A history-derived answer must not stay readable after an administrator denies it.

The dispatch-time history snapshot is not enough: an admin can DENY or narrow the history scope
after the answer was stored. Both the ingest path and get_task must re-check the current
decision, so an old result cannot be read back around the veto (assessment 2026-10-05).
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.common import encode, request
from app.history_access import HistoryAccess

TASK = '22222222-2222-2222-2222-222222222222'
HISTORY_ROW = {'task_id': TASK, 'owner': 'ag-a', 'target': 'mac-1', 'operation': 'ask_peer',
               'state': 'succeeded', 'created_at': 1.0, 'deadline': time.time() + 600,
               'payload': json.dumps({'question': '进展', 'projects': ['demo-project']}),
               'result': json.dumps({'code': 'OK', 'data': {'source': 'local_claude_history_index',
                                                            'text': '来自本机历史索引'}})}


class _Tasks:
    def __init__(self, row=None):
        self.finished = []
        self.row = row or HISTORY_ROW

    async def get(self, task_id):
        return dict(self.row) if task_id == TASK else None

    async def finish(self, task_id, result):
        self.finished.append(result)

    async def finish_status(self, task_id, result):
        self.finished.append(result)
        return 'committed'

    async def history(self, task_id):
        return []

    @staticmethod
    def public(row):
        return {'task_id': row['task_id'], 'target': row['target'], 'state': row['state'],
                'result': json.loads(row['result'])}


class HistoryRecheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        # authz resolves its store base from app.common.RUNTIME, so patch both names.
        self.patcher = patch('app.router.RUNTIME', self.runtime)
        self.common = patch('app.common.RUNTIME', self.runtime)
        # directory_requests binds RUNTIME at import time; pin it per test so a torn-down temp
        # dir from an earlier test cannot leak in.
        self.dirs = patch('app.directory_requests.RUNTIME', self.runtime)
        self.patcher.start(); self.common.start(); self.dirs.start()
        from app.router import Router
        self.router = Router(AsyncMock())
        self.router.validator = SimpleNamespace(validate=lambda body: None)   # schema is covered elsewhere
        self.router.respond = AsyncMock()
        self.tasks = _Tasks()
        self.router.tasks = self.tasks
        self.store = HistoryAccess(self.runtime / 'history-access.sqlite3')

    def tearDown(self):
        self.patcher.stop(); self.common.stop(); self.dirs.stop()
        self.tmp.cleanup()

    def _deny(self):
        row = self.store.request('ag-a', 'mac-1', scope='demo-project')
        self.store.decide(row['id'], 'denied', 86400, 'veto', 'admin')

    async def _get_task(self):
        body = request('ag-a', 'mac-1')
        body['action'] = 'get_task'
        body['payload'] = {'task_id': TASK}
        with patch('app.router.allowed', return_value=True), \
                patch('app.router.load_registry',
                      return_value={'ag-a': {'enabled': True}, 'mac-1': {'enabled': True}}):
            await self.router.ingress(SimpleNamespace(subject='corp.dev.v1.ingress.ag-a.request',
                                                      data=encode(body)))
        return self.router.respond.call_args.args

    async def test_worker_material_rejections_are_terminal_and_acknowledged(self):
        for code in ('FORBIDDEN','LOCAL_SUMMARY_UNAVAILABLE','HISTORY_NOT_CONFIGURED','HISTORY_SCOPE_REQUIRED','HISTORY_UNAVAILABLE'):
            msg=SimpleNamespace(subject='corp.dev.v1.agent.mac-1.result',data=encode({'task_id':TASK,'code':code,'data':None}))
            with patch('app.router.allowed',return_value=True):
                await self.router.task_result(msg)
            self.assertEqual(self.tasks.finished[-1]['code'],code)
            sent=self.router.nc.publish.call_args
            self.assertEqual(sent.args[0],'corp.dev.v1.client.mac-1.response')
            self.assertEqual(json.loads(sent.args[1])['type'],'task_result_ack')

    async def test_get_task_refuses_a_stored_history_result_after_deny(self):
        code, data = (await self._get_task())[2:]
        self.assertEqual(code, 'OK')                     # no veto yet: the result is readable
        self._deny()
        code, data = (await self._get_task())[2:]
        self.assertEqual(code, 'FORBIDDEN')
        self.assertEqual(data['reason'], 'HISTORY_ACCESS_DENIED_BY_ADMIN')

    async def test_reviewed_history_source_is_refused_on_both_paths(self):
        row = dict(HISTORY_ROW, result=json.dumps({'code': 'OK', 'data': {
            'source': 'local_history_with_claude_review', 'text': '来自 Claude 复核'}}))
        tasks = _Tasks(row)
        self.router.tasks = tasks
        self._deny()
        code, data = (await self._get_task())[2:]
        self.assertEqual(code, 'FORBIDDEN')
        msg = SimpleNamespace(subject='corp.dev.v1.agent.mac-1.result',
                              data=encode({'task_id': TASK, 'code': 'OK',
                                           'data': {'source': 'local_history_with_claude_review', 'text': 'h'}}))
        with patch('app.router.allowed', return_value=True):
            await self.router.task_result(msg)
        self.assertEqual(tasks.finished[-1]['code'], 'FORBIDDEN')

    async def test_ingest_refuses_to_store_a_history_result_after_deny(self):
        self._deny()
        msg = SimpleNamespace(subject='corp.dev.v1.agent.mac-1.result',
                              data=encode({'task_id': TASK, 'code': 'OK',
                                           'data': {'source': 'local_claude_history_index', 'text': 'h'}}))
        with patch('app.router.allowed', return_value=True):
            await self.router.task_result(msg)
        self.assertEqual(self.tasks.finished[-1]['code'], 'FORBIDDEN')


if __name__ == '__main__':
    unittest.main()

class HarnessOwnerRecheckTests(unittest.TestCase):
    def test_malformed_heartbeat_consent_is_fail_closed(self):
        from app.router import history_consent_metadata
        good={'enabled':True,'expires':time.time()+60,'version':'a'*64}
        self.assertEqual(history_consent_metadata(good),good)
        for c in ({**good,'expires':float('nan')},{**good,'expires':float('inf')},
                  {**good,'version':None},{**good,'version':'z'*64},{**good,'expires':True}):
            self.assertEqual(history_consent_metadata(c),{})
    def test_owner_expiry_change_and_scope_recheck(self):
        from app.router import Router
        router=Router(AsyncMock());now=time.time();row=dict(HISTORY_ROW,target='win-1')
        router.presence['win-1']=now
        consent={'enabled':True,'version':'a'*64,'expires':now+60};router.runtime_history_consent['win-1']=consent
        r={'code':'OK','data':{'source':'local_harness_history_index','history_projects':['demo-project'],'owner_consent':dict(consent)}}
        with patch('app.authz.decide',return_value={'allowed':True}):
            self.assertFalse(router._history_result_denied('ag-a',row,r))
            router.runtime_history_consent['win-1']={**consent,'version':'b'*64};self.assertTrue(router._history_result_denied('ag-a',row,r))
            router.runtime_history_consent['win-1']={**consent,'expires':now-1};self.assertTrue(router._history_result_denied('ag-a',row,r))
            router.runtime_history_consent['win-1']=consent;r['data']['history_projects']=['private'];self.assertTrue(router._history_result_denied('ag-a',row,r))
    def test_new_source_missing_or_stale_owner_heartbeat_is_denied(self):
        from app.router import Router
        router=Router(AsyncMock());r={'data':{'source':'local_harness_history_index'}}
        self.assertTrue(router._history_result_denied('ag-a',HISTORY_ROW,r))
