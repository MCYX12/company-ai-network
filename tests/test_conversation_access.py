import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from app.conversation_access import AccessStore
from app.policy import allowed

class AccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=AccessStore(Path(self.tmp.name)/'access.db')
    def tearDown(self):self.tmp.cleanup()
    def test_lifecycle_and_boundaries(self):
        s=self.store;r=s.request('alice','mac',['p']);ident=r['approval_id']
        self.assertEqual(s.request('alice','mac',['p'])['approval_id'],ident)
        self.assertFalse(s.permits('alice','mac',['p']))
        s.decide(ident,'approved',60,'pilot')
        self.assertTrue(s.permits('alice','mac',['p']))
        for caller,target,projects in [('bob','mac',['p']),('alice','other',['p']),('alice','mac',['q']),('alice','mac',[])]:self.assertFalse(s.permits(caller,target,projects))
        with patch('app.conversation_access.time.time',return_value=time.time()+61):self.assertFalse(s.permits('alice','mac',['p']))
        s.decide(ident,'revoked',1,'done');self.assertFalse(s.permits('alice','mac',['p']))
        with self.assertRaises(ValueError):s.decide(ident,'approved',60,'again')
    def test_no_expiry_approval_stays_valid(self):
        s=self.store;r=s.request('alice','mac',['p'])
        s.decide(r['approval_id'],'approved',0,'standing')
        self.assertTrue(s.permits('alice','mac',['p']))
        far=time.time()+50*365*86400
        with patch('app.conversation_access.time.time',return_value=far):
            self.assertTrue(s.permits('alice','mac',['p']))
            self.assertEqual([row['state'] for row in s.listing('alice')],['approved'])
        pending=s.request('bob','mac',['p'])
        with self.assertRaises(ValueError):s.decide(pending['approval_id'],'approved',-1,'bad')

    def test_policy_blocks_alternative_text_operation(self):
        registry={k:dict(enabled=True,projects=['p'],tool='codex') for k in ['alice','mac']}
        with patch('app.policy.approved',side_effect=self.store.permits):
            for op,payload in [('ask_peer',{'projects':['p']}),('generate_text',{'prompt':'hi'})]:
                self.assertFalse(allowed('alice','mac',op,payload,registry))
                r=self.store.request('alice','mac',['p']);self.store.decide(r['approval_id'],'approved',60,'test')
                self.assertTrue(allowed('alice','mac',op,payload,registry))
                self.store.decide(r['approval_id'],'revoked',1,'test')
    def test_owner_filtered_listing(self):
        self.store.request('alice','mac',['p']);self.store.request('bob','mac',['p'])
        self.assertEqual(len(self.store.listing('alice')),1)

class RouterAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoked_pending_task_not_dispatched(self):
        from app.router import Router
        from unittest.mock import AsyncMock
        import app.router as router_module
        with tempfile.TemporaryDirectory() as tmp, patch.object(router_module,'RUNTIME',Path(tmp)):
            router=Router(AsyncMock())
            row=router.tasks.submit('alice','mac','one','ask_peer',{'question':'hi','projects':['p']},60)
            router.presence['mac']=time.time();router.runtime_tasks['mac']=['ask_peer']
            with patch.object(router_module,'allowed',return_value=False):await router.dispatch_tasks()
            router.nc.publish.assert_not_called()
            self.assertEqual(router.tasks.get(row['task_id'])['state'],'failed')
