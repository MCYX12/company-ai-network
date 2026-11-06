"""A client waiting on the original must follow the one platform-scheduled retry."""
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import peer_client


class RetryFollowTests(unittest.TestCase):
    def _run(self,responses,rounds='1'):
        submitted=[]
        async def fake_query(nc,identity,body):
            action=body['action']
            if action=='list_agents':
                return {'code':'OK','data':{'agents':[{'agent_id':'ag-a','device_id':'d','tool':'t',
                        'presence':'online','available_task_actions':['ask_peer']}]}}
            if action=='submit_task':
                submitted.append(body['payload']['idempotency_key'])
                return {'code':'ACCEPTED','data':{'task_id':'t0'}}
            if action=='get_task':
                return responses.get(body['payload']['task_id'],
                                     {'code':'OK','data':{'task_id':body['payload']['task_id'],'state':'running'}})
            raise AssertionError(action)
        class FakeNc:
            is_connected=True
            async def close(self):pass
        async def fake_connect(*a,**k):return FakeNc()
        args=SimpleNamespace(identity='ag-a',question='robotac 进度',target='ag-a',key=None,wait=True,
                             list=False,doctor=False,access_status=False,request_access=False,task_id=None)
        env={'AGENT_WAIT_SECONDS':'3','AGENT_DIRECTORY_ROUNDS':rounds,'AGENT_TASK_TTL_SECONDS':'600'}
        with patch('app.peer_client.connect',fake_connect),patch('app.peer_client.query',fake_query),patch.dict(os.environ,env):
            result=asyncio.run(peer_client.run(args))
        return result,submitted

    def test_client_follows_the_retry_to_the_recovered_answer(self):
        responses={
            't0':{'code':'OK','data':{'task_id':'t0','state':'failed','retry_task_id':'t1',
                                      'retry_attempt':2,'result':{'code':'WORKER_INTERRUPTED','data':None}}},
            't1':{'code':'OK','data':{'task_id':'t1','state':'succeeded','retry_of':'t0','attempt':2,
                                      'result':{'code':'OK','data':{'text':'恢复结果'}}}}}
        result,submitted=self._run(responses)
        self.assertEqual(result['data']['task_id'],'t1')
        self.assertEqual(result['data']['result']['data']['text'],'恢复结果')
        self.assertEqual(len(submitted),1)          # followed, never resubmitted

    def test_retry_is_followed_even_when_directory_rounds_is_zero(self):
        responses={
            't0':{'code':'OK','data':{'task_id':'t0','state':'failed','retry_task_id':'t1',
                                      'retry_attempt':2,'result':{'code':'WORKER_INTERRUPTED','data':None}}},
            't1':{'code':'OK','data':{'task_id':'t1','state':'succeeded','retry_of':'t0','attempt':2,
                                      'result':{'code':'OK','data':{'text':'恢复结果'}}}}}
        result,submitted=self._run(responses,rounds='0')
        self.assertEqual(result['data']['task_id'],'t1')
        self.assertEqual(result['data']['result']['data']['text'],'恢复结果')
        self.assertEqual(len(submitted),1)

    def test_resubmit_after_directory_approval_starts_a_new_chain_deadline(self):
        # Controlled clock: the approval outlives the first execution window, the resubmitted
        # chain gets a NEW budget, and its recovery subtask must first be running and only
        # succeed after a real (simulated) wait. Reverting the deadline reset makes this fail.
        submitted=[];state={'t2':0,'t3':0,'dir':0}
        async def fake_query(nc,identity,body):
            action=body['action']
            if action=='list_agents':
                return {'code':'OK','data':{'agents':[{'agent_id':'ag-a','device_id':'d','tool':'t',
                        'presence':'online','available_task_actions':['ask_peer']}]}}
            if action=='submit_task':
                submitted.append(body['payload']['idempotency_key'])
                return {'code':'ACCEPTED','data':{'task_id':'t0' if len(submitted)==1 else 't2'}}
            if action=='list_directory_requests':
                state['dir']+=1
                status='approved' if state['dir']>=3 else 'pending'   # 3 polls * 3s > first window
                return {'code':'OK','data':{'requests':[{'id':'r1','state':status}]}}
            if action=='get_task':
                wanted=body['payload']['task_id']
                if wanted=='t0':
                    return {'code':'OK','data':{'task_id':'t0','state':'succeeded',
                            'result':{'code':'OK','data':{'directory_requests':[
                                {'id':'r1','state':'pending','root':'/data/projects/demo'}]}}}}
                if wanted=='t2':
                    state['t2']+=1
                    if state['t2']==1:
                        return {'code':'OK','data':{'task_id':'t2','state':'running'}}
                    return {'code':'OK','data':{'task_id':'t2','state':'failed','retry_task_id':'t3',
                            'retry_attempt':2,'result':{'code':'WORKER_INTERRUPTED','data':None}}}
                if wanted=='t3':
                    state['t3']+=1
                    if state['t3']==1:
                        return {'code':'OK','data':{'task_id':'t3','state':'running'}}   # must wait
                    return {'code':'OK','data':{'task_id':'t3','state':'succeeded','retry_of':'t2',
                            'result':{'code':'OK','data':{'text':'新链恢复'}}}}
                raise AssertionError(wanted)
            raise AssertionError(action)
        class FakeNc:
            is_connected=True
            async def close(self):pass
        async def fake_connect(*a,**k):return FakeNc()
        args=SimpleNamespace(identity='ag-a',question='robotac 进度',target='ag-a',key=None,wait=True,
                             list=False,doctor=False,access_status=False,request_access=False,task_id=None)
        env={'AGENT_WAIT_SECONDS':'5','AGENT_DIRECTORY_ROUNDS':'1','AGENT_DIRECTORY_WAIT_SECONDS':'60',
             'AGENT_POLL_SECONDS':'0.5','AGENT_TASK_TTL_SECONDS':'600'}
        real_sleep=asyncio.sleep
        clock={'now':1000.0}
        async def fake_sleep(seconds):
            clock['now']+=max(float(seconds),0.001)
            await real_sleep(0)
        with patch('app.peer_client.connect',fake_connect),patch('app.peer_client.query',fake_query), \
             patch('app.peer_client.time') as fake_time, \
             patch('app.peer_client.asyncio.sleep',fake_sleep),patch.dict(os.environ,env):
            fake_time.monotonic.side_effect=lambda:clock['now']
            fake_time.time.side_effect=lambda:clock['now']
            result=asyncio.run(peer_client.run(args))
        self.assertEqual(result['data']['task_id'],'t3')
        self.assertEqual(result['data']['result']['data']['text'],'新链恢复')
        self.assertGreaterEqual(state['t3'],2)          # the recovery subtask really waited
        self.assertEqual(len(submitted),2)              # original + one resubmit after approval

    def test_at_most_one_retry_is_followed(self):
        responses={
            't0':{'code':'OK','data':{'task_id':'t0','state':'failed','retry_task_id':'t1',
                                      'retry_attempt':2,'result':{'code':'WORKER_INTERRUPTED','data':None}}},
            't1':{'code':'OK','data':{'task_id':'t1','state':'failed','retry_task_id':'t2',
                                      'retry_attempt':3,'result':{'code':'WORKER_INTERRUPTED','data':None}}},
            't2':{'code':'OK','data':{'task_id':'t2','state':'succeeded',
                                      'result':{'code':'OK','data':{'text':'不该到达'}}}}}
        result,_=self._run(responses)
        self.assertEqual(result['data']['task_id'],'t1')
        self.assertEqual(result['data']['result']['code'],'WORKER_INTERRUPTED')


if __name__=='__main__':
    unittest.main()
