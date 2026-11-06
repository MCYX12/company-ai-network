import unittest
from app.progress_query import remote_progress,remote_work
from app.progress_mcp import handle
class Bridge:
    identity='ag-a'
    def __init__(self):self.calls=[]
    def call(self,action,payload,target):
        self.calls.append((action,payload,target))
        if action=='list_agents':return {'code':'OK','data':{'agents':[{'agent_id':'mac-codex-01','device_id':'mac-demo','presence':'online'}]}}
        if action=='get_project_progress':return {'code':'DATA_UNAVAILABLE','data':None}
        return {'code':'OK','data':{'tasks':[{'target':'mac-codex-01','state':'succeeded'},{'target':'other','state':'accepted'}]}}
class Tests(unittest.TestCase):
    def test_platform_execution_survives_aggregate_without_private_fields(self):
        import time
        class LiveBridge(Bridge):
            def call(self,action,payload,target):
                if action=='list_agents':return {'code':'OK','data':{'agents':[{'agent_id':'mac-codex-01','device_id':'device','presence':'online','capabilities':['get_status']} ]}}
                if action=='get_status':return {'code':'OK','data':{'native_work':{'status':'idle','observed_at':time.time(),'text':'PRIVATE','background_jobs':{'status':'running','executing_count':1,'prompt':'PRIVATE'},'scheduled_tasks':{'status':'scheduled','active_count':1,'title':'PRIVATE'}},'worker_activity':{'state':'running','executing_count':1,'observed_at':time.time(),'prompt':'PRIVATE'}}}
                return super().call(action,payload,target)
        b=LiveBridge()
        for result in (remote_progress(b,'mac-codex-01','demo-project'),remote_work(b,'mac-codex-01')):
            self.assertEqual(result['data']['worker_activity']['state'],'running')
            self.assertNotIn('PRIVATE',str(result));self.assertEqual(result['data']['native_work']['status'],'idle')
    def test_status_failure_is_unknown_and_does_not_create_access(self):
        class Unavailable(Bridge):
            def call(self,action,payload,target):
                if action=='list_agents':return {'code':'OK','data':{'agents':[{'agent_id':'mac-codex-01','presence':'online','capabilities':['get_status']} ]}}
                self.calls.append(action);return {'code':'TIMEOUT','data':None}
        b=Unavailable();data=remote_work(b,'mac-codex-01')['data']
        self.assertEqual(data['status_query_error'],'TIMEOUT');self.assertEqual(data['native_work']['status'],'unknown')
        self.assertEqual(data['worker_activity']['state'],'unknown');self.assertEqual(b.calls,['get_status'])
    def test_unknown_native_progress_never_inferred_from_online(self):
        b=Bridge();r=remote_progress(b,'mac-codex-01','demo-project')['data']
        self.assertEqual(r['native_work']['status'],'not_integrated');self.assertEqual(r['project']['code'],'DATA_UNAVAILABLE');self.assertEqual(len(r['recent_own_tasks']),1)
        self.assertIn(('get_project_progress',{'project_id':'demo-project'},'mac-codex-01'),b.calls)
    def test_hidden_target_not_queried(self):
        b=Bridge();self.assertEqual(remote_progress(b,'hidden','demo-project')['code'],'FORBIDDEN');self.assertEqual(len(b.calls),1)
    def test_mcp_rejects_write_and_identity_override(self):
        b=Bridge()
        for name,args in [('submit_task',{}),('ask_agent_progress',{'target':'mac-codex-01','project_id':'demo-project','identity':'router'})]:
            self.assertTrue(handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}},b)['result']['isError'])
        self.assertEqual(b.calls,[])
    def test_mcp_notification_and_initialize(self):
        self.assertIsNone(handle({'jsonrpc':'2.0','method':'notifications/initialized'},Bridge()))
        self.assertIn('tools',handle({'jsonrpc':'2.0','id':1,'method':'initialize'},Bridge())['result']['capabilities'])
if __name__=='__main__':unittest.main()
