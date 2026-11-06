import http.client
import json
import threading
import unittest
from app.dashboard import DashboardServer

class FakeBridge:
    identity='ag-a'
    def __init__(self):self.calls=[]
    def call(self,*args):
        self.calls.append(args)
        return {'code':'OK','data':{'agents':[]}}

class DashboardTests(unittest.TestCase):
    def test_project_defaults_follow_site_without_demo_fallback(self):
        from unittest.mock import patch
        for project in ('enterprise-project',None):
            with patch('app.site.defaults',return_value={'project':project}):
                status,raw,_=self.call('/api/bootstrap')
                self.assertEqual(status,200)
                self.assertEqual(json.loads(raw)['defaults']['project'],project)
    def test_invalid_health_shapes_return_unknown_instead_of_disconnecting(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        fixtures=[None,[],{'status':'ok','checks':{}},{'status':'ok','checks':[None]},
                  {'status':'ok','checks':[{'check':'task_ledger','status':'unexpected'}]},
                  {'status':'ok','checks':[{'check':'task_ledger','status':'fail'}]}]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'health').mkdir()
            for value in fixtures:
                with self.subTest(value=value),patch('app.common.RUNTIME',root):
                    (root/'health/latest.json').write_text(json.dumps(value))
                    status,raw,_=self.call('/api/health')
                    result=json.loads(raw)
                    self.assertEqual(status,200)
                    self.assertFalse(result['available'])
                    self.assertEqual(result['status'],'unknown')
                    self.assertEqual(result['snapshot_error'],'HEALTH_SNAPSHOT_INVALID')

    def test_work_route_does_not_bypass_token_or_target_visibility(self):
        self.assertEqual(self.call('/api/work',{'target':'hidden'},authenticated=False)[0],403)
        status,raw,_=self.call('/api/work',{'target':'hidden'})
        self.assertEqual(status,200);self.assertEqual(json.loads(raw)['code'],'FORBIDDEN')
        self.assertEqual(self.bridge.calls,[('list_agents',{},None)])
        self.assertEqual(self.call('/api/work',{'target':'../escape'})[0],400)
    def setUp(self):
        self.bridge=FakeBridge();self.server=DashboardServer(('127.0.0.1',0),self.bridge)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def call(self,path,body=None,headers=None,authenticated=True):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        h={'Content-Type':'application/json'}
        if authenticated:h['X-Dashboard-Token']=self.server.token
        h.update(headers or {})
        conn.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,h)
        r=conn.getresponse();out=(r.status,r.read(),dict(r.headers));conn.close();return out
    def test_admin_http_lifecycle(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from app.conversation_access import AccessStore
        self.server.admin=True
        with tempfile.TemporaryDirectory() as tmp:
            store=AccessStore(Path(tmp)/'db')
            request=store.request('a','b',['p'])
            registry={k:dict(enabled=True,projects=['p']) for k in ['a','b']}
            with patch('app.admin_access.AccessStore',return_value=store),patch('app.admin_access.load_registry',return_value=registry):
                body=dict(id=request['approval_id'],decision='approved',seconds=60,reason='HTTP test')
                self.assertEqual(self.call('/api/admin/access',body)[0],200)
                self.assertTrue(store.permits('a','b',['p']))
                self.assertEqual(self.call('/api/admin/access',body)[0],409)
                body['decision']='revoked'
                self.assertEqual(self.call('/api/admin/access',body)[0],200)
                self.assertFalse(store.permits('a','b',['p']))
    def test_directory_admin_route(self):
        from unittest.mock import patch
        self.assertEqual(self.call('/api/admin/directories',{})[0],403)
        self.server.admin=True
        self.assertEqual(self.call('/api/admin/directories',{},authenticated=False)[0],403)
        with patch('app.directory_requests.handle',return_value={'requests':[]}) as handle:
            self.assertEqual(self.call('/api/admin/directories',{})[0],200)
            handle.assert_called_once_with('local-admin',{})
    def test_admin_disabled_by_default(self):
        self.assertEqual(self.call('/api/admin/access',{})[0],403)
    def test_admin_requires_token_and_origin(self):
        self.server.admin=True
        self.assertEqual(self.call('/api/admin/access',{},authenticated=False)[0],403)
        self.assertEqual(self.call('/api/admin/access',{},headers={'Origin':'https://example.com'})[0],403)
    def test_index_is_local_and_has_security_headers(self):
        code,raw,headers=self.call('/')
        self.assertEqual(code,200);self.assertNotIn(b'__TOKEN__',raw)
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'],'no-store')
    def test_cross_origin_rebinding_and_no_token_rejected(self):
        for h in [{'Origin':'https://untrusted.example'},{'Host':'untrusted.example'},{'Sec-Fetch-Site':'cross-site'}]:
            self.assertEqual(self.call('/api/agents',{},h)[0],403)
        self.assertEqual(self.call('/api/agents',{},authenticated=False)[0],403)
        self.assertEqual(self.bridge.calls,[])
    def test_fixed_identity_cannot_be_overridden(self):
        self.assertEqual(self.call('/api/agents',{'identity':'router'})[0],404)
        self.assertEqual(self.bridge.calls,[])
    def test_api_routes_through_existing_bridge(self):
        self.assertEqual(self.call('/api/agents',{})[0],200)
        self.assertEqual(self.bridge.calls,[('list_agents',{},None)])
    def test_no_arbitrary_file_or_action(self):
        self.assertEqual(self.call('/.runtime-lan/certs/ag-a.key')[0],404)
        body={'target':'mac-codex-01','operation':'execute_shell','prompt':'x','idempotency_key':'00000000-0000-0000-0000-000000000000'}
        self.assertEqual(self.call('/api/submit',body)[0],404)
        self.assertEqual(self.bridge.calls,[])
    def test_chat_maps_only_to_bounded_question(self):
        body={'target':'mac-codex-01','question':'项目进展？','idempotency_key':'00000000-0000-0000-0000-000000000000'}
        self.assertEqual(self.call('/api/chat/send',body)[0],200)
        self.assertEqual(self.bridge.calls[0][1]['operation'],'ask_peer')
        self.assertNotIn('prompt',self.bridge.calls[0][1])
        body['question']='a'*1201
        self.assertEqual(self.call('/api/chat/send',body)[0],400)
    def test_execution_submission_disabled(self):
        self.assertEqual(self.call('/api/submit',{'target':'mac-codex-01','operation':'generate_text','prompt':'hi','idempotency_key':'00000000-0000-0000-0000-000000000000'})[0],404)
        self.assertFalse(self.bridge.calls)
    def test_bootstrap_returns_plan_and_identity(self):
        code,raw,_=self.call('/api/bootstrap');data=json.loads(raw)
        self.assertEqual(code,200);self.assertEqual(data['identity'],'ag-a');self.assertEqual(len(data['plan']),6)
    def test_delivery_health_reports_snapshot_or_absence(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            with patch('app.common.RUNTIME', Path(folder)):
                status, raw, _ = self.call('/api/health')
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(raw)['available'])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'health').mkdir()
            (root / 'health/latest.json').write_text(json.dumps({
                'status': 'ok', 'generated_at': '2026-10-03T11:40:06Z',
                'checks': [{'check': 'task_ledger', 'status': 'ok', 'detail': {
                    'pending': 2, 'running': 1, 'dead_letter_delivery_failed': 3,
                    'dead_letter_delivery_failed_24h': 1,
                    'dead_letter_by_target_24h': {'mac-codex-01': 1}}}]}))
            with patch('app.common.RUNTIME', root):
                status, raw, _ = self.call('/api/health')
            body = json.loads(raw)
            self.assertTrue(body['available'])
            self.assertEqual(body['task_ledger']['dead_letter_delivery_failed_24h'], 1)
            self.assertEqual(body['task_ledger']['dead_letter_by_target_24h'], {'mac-codex-01': 1})


if __name__=='__main__':unittest.main()
