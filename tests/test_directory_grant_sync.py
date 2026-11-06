"""Directory-grant flow: refusal reasons, canonicalisation, caller polling, auto-resubmit."""
import asyncio,json,os,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.directory_requests import DirectoryRequests,RootRejected,validate_root,reason_text,canonical_root
from app import peer_client


class RootReasonTests(unittest.TestCase):
    def test_every_refusal_carries_a_code_and_a_sentence(self):
        cases={'/': 'TOO_SHORT','/Users/mac': 'TOO_BROAD','/home/demo': 'TOO_BROAD',
               '/Users/mac/.ssh': 'HIDDEN_COMPONENT','/Users/mac/a/../b': 'PARENT_TRAVERSAL',
               '/Users/mac//a': 'NOT_NORMALIZED','/Users/mac/a/': 'NOT_NORMALIZED',
               'relative/path': 'NOT_ABSOLUTE','/Users/mac/back\\slash': 'BAD_CHARS',
               '/Users/'+('x'*1200): 'TOO_LONG'}
        for root,code in cases.items():
            with self.assertRaises(RootRejected) as caught:validate_root(root)
            self.assertEqual(caught.exception.code,code,root)
            self.assertTrue(reason_text(code),code)
    def test_specific_root_is_still_accepted(self):
        self.assertEqual(validate_root('/Users/mac/Qwen mac'),'/Users/mac/Qwen mac')

    def test_case_variant_is_canonicalised_against_the_local_filesystem(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
            real=Path(tmp)/'InnerDir';(real/'sub').mkdir(parents=True)
            wanted=str(real).replace('InnerDir','innerdir')
            self.assertNotEqual(wanted,str(real))
            canonical,changed=canonical_root(wanted)
            self.assertEqual(canonical,str(real));self.assertTrue(changed)
            # A path this host cannot resolve is never invented (remote device paths).
            self.assertEqual(canonical_root('/Users/mac/Nope'),('/Users/mac/Nope',False))

    def test_store_refuses_a_case_only_variant_and_names_the_real_path(self):
        """Discovery belongs to the agent on the device: no silent renaming by the server."""
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
            real=Path(tmp)/'Proj';real.mkdir()
            store=DirectoryRequests(Path(tmp)/'d.sqlite3')
            with self.assertRaises(RootRejected) as caught:store.request('a','b',str(real).replace('Proj','proj'))
            self.assertEqual(caught.exception.code,'CASE_MISMATCH')
            self.assertEqual(caught.exception.candidates,[str(real)])
            self.assertEqual(store.listing('a','b'),[])
            # The exact path the agent found is accepted as submitted.
            self.assertEqual(store.request('a','b',str(real))['root'],str(real))

    def test_autocanon_env_restores_server_side_renaming(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp, patch.dict(os.environ,{'DIRECTORY_AUTOCANON':'1'}):
            real=Path(tmp)/'Proj';real.mkdir()
            store=DirectoryRequests(Path(tmp)/'d.sqlite3')
            item=store.request('a','b',str(real).replace('Proj','proj'))
            self.assertEqual(item['root'],str(real));self.assertTrue(item['canonicalized'])

    def test_cross_caller_approval_is_reported_without_granting_it(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
            store=DirectoryRequests(Path(tmp)/'d.sqlite3')
            first=store.request('ag-a','ag-a','/Users/mac/proj')
            store.decide(first['id'],'approved',0,'yes','admin')
            second=store.request('ag-b','ag-a','/Users/mac/proj')
            self.assertEqual(second['also_approved_for'],['ag-a'])
            self.assertEqual(second['state'],'pending')
            self.assertFalse(store.permits_ids('ag-b','ag-a',[first['id']]))
            self.assertEqual([r['root'] for r in store.for_caller('ag-b')],['/Users/mac/proj'])
            self.assertEqual(store.for_caller('ag-c'),[])


class PromptPrefixTests(unittest.TestCase):
    def test_device_owner_prefix_is_prepended_to_the_platform_prompt(self):
        from app import harness_adapter
        row={'agent_id':'ag-a','tool':'dsh-harness','projects':['demo-project'],'device_id':'ubuntu-demo','employee_id':'e','enabled':True}
        rules=harness_adapter.PLATFORM_RULES
        with patch('app.peer_question.build_prompt',return_value=('平台提示',[])):
            self.assertEqual(harness_adapter._prompt(row,{'question':'q'},{'prompt_prefix':'规则'} ),(rules+'\n\n规则\n\n平台提示',[]))
            self.assertEqual(harness_adapter._prompt(row,{'question':'q'},{}),(rules+'\n\n平台提示',[]))
            self.assertEqual(harness_adapter._prompt(row,{'question':'q'},{'prompt_prefix':'   '}),(rules+'\n\n平台提示',[]))


class ClientAutoResumeTests(unittest.TestCase):
    """A pending directory request no longer ends the question: the client resubmits."""

    def _run(self, listing_states, rounds='1'):
        submitted=[];polls={'n':0}
        async def fake_query(nc,identity,body):
            action=body['action']
            if action=='list_agents':
                return {'code':'OK','data':{'agents':[{'agent_id':'ag-a','device_id':'d','tool':'t',
                                                       'presence':'online','available_task_actions':['ask_peer']}]}}
            if action=='submit_task':
                submitted.append(body['payload']['idempotency_key'])
                return {'code':'ACCEPTED','data':{'task_id':'t%d'%len(submitted)}}
            if action=='get_task':
                task=body['payload']['task_id']
                if task=='t1':
                    answer={'text':'需要新目录授权','directory_requests':[{'id':'r1','root':'/Users/mac/proj','state':'pending'}]}
                else:
                    answer={'text':'已按新授权回答','directory_requests':[{'id':'r1','root':'/Users/mac/proj','state':'approved'}]}
                return {'code':'OK','data':{'task_id':task,'state':'succeeded','result':{'code':'OK','data':answer}}}
            if action=='list_directory_requests':
                polls['n']+=1
                state=listing_states[min(polls['n']-1,len(listing_states)-1)]
                return {'code':'OK','data':{'requests':[{'id':'r1','root':'/Users/mac/proj','state':state}]}}
            raise AssertionError(action)
        class FakeNc:
            is_connected=True
            async def close(self):pass
        async def fake_connect(*a,**k):return FakeNc()
        args=SimpleNamespace(identity='ag-a',question='robotac 进度',target='ag-a',key=None,wait=True,
                             list=False,doctor=False,access_status=False,request_access=False,task_id=None)
        env={'AGENT_WAIT_SECONDS':'2','AGENT_DIRECTORY_WAIT_SECONDS':'30','AGENT_DIRECTORY_ROUNDS':rounds,
             'AGENT_TASK_TTL_SECONDS':'600'}
        with patch('app.peer_client.connect',fake_connect),patch('app.peer_client.query',fake_query),patch.dict(os.environ,env):
            result=asyncio.run(peer_client.run(args))
        return result,submitted,polls['n']

    def test_pending_request_is_polled_and_the_question_resubmitted(self):
        result,submitted,polls=self._run(['pending','approved'])
        self.assertEqual(result['data']['result']['data']['text'],'已按新授权回答')
        self.assertEqual(len(submitted),2)
        self.assertGreaterEqual(polls,2)

    def test_denied_request_returns_the_original_answer_without_resubmitting(self):
        result,submitted,_=self._run(['denied'])
        self.assertEqual(result['data']['result']['data']['text'],'需要新目录授权')
        self.assertEqual(len(submitted),1)

    def test_auto_resume_can_be_switched_off(self):
        result,submitted,_=self._run(['approved'],rounds='0')
        self.assertEqual(result['data']['result']['data']['text'],'需要新目录授权')
        self.assertEqual(len(submitted),1)


class RouterRejectionTests(unittest.TestCase):
    def test_refused_proposal_is_reported_instead_of_vanishing(self):
        from app.router import Router
        from app.common import encode,PREFIX
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as tmp,patch('app.router.RUNTIME',Path(tmp)),\
             patch('app.directory_requests.RUNTIME',Path(tmp)),patch('app.router.allowed',return_value=True):
            router=Router(SimpleNamespace(publish=AsyncMock()))
            row=router.tasks.submit('owner','peer','key','ask_peer',{'caller':'owner','projects':['p'],'question':'x'},120)
            body={'task_id':row['task_id'],'code':'OK','data':{'text':'x',
                  'directory_requests':[{'root':'/Users/mac'},{'root':'/Users/mac/ok'},{'root':'相对路径'}]}}
            asyncio.run(router.task_result(SimpleNamespace(subject=f'{PREFIX}.agent.peer.result',data=encode(body))))
            data=json.loads(router.tasks.get(row['task_id'])['result'])['data']
            self.assertEqual([r['code'] for r in data['directory_request_rejections']],['TOO_BROAD','NOT_ABSOLUTE'])
            self.assertEqual([r['root'] for r in data['directory_requests']],['/Users/mac/ok'])
            self.assertIn('资料访问申请被拒绝',data['text'])
            self.assertEqual(DirectoryRequests(Path(tmp)/'directory-requests.sqlite3').listing()[0]['root'],'/Users/mac/ok')


    def test_out_of_band_request_is_adopted_into_the_answer(self):
        """A harness that wrote the ledger directly must not hide the request from the caller."""
        from app.router import Router
        from app.common import encode,PREFIX
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as tmp,patch('app.router.RUNTIME',Path(tmp)),\
             patch('app.directory_requests.RUNTIME',Path(tmp)),patch('app.router.allowed',return_value=True):
            router=Router(SimpleNamespace(publish=AsyncMock()))
            row=router.tasks.submit('owner','peer','key-out','ask_peer',{'caller':'owner','projects':['p'],'question':'x'},120)
            DirectoryRequests(Path(tmp)/'directory-requests.sqlite3').request('owner','peer','/Users/mac/outofband')
            body={'task_id':row['task_id'],'code':'OK','data':{'text':'已登记'}}
            asyncio.run(router.task_result(SimpleNamespace(subject=f'{PREFIX}.agent.peer.result',data=encode(body))))
            data=json.loads(router.tasks.get(row['task_id'])['result'])['data']
            self.assertEqual([r['root'] for r in data['directory_requests']],['/Users/mac/outofband'])
            self.assertEqual(data['directory_requests'][0]['registered_by'],'platform_reconciliation')
            self.assertEqual(data['directory_requests'][0]['state'],'pending')
            self.assertIn('平台登记的资料访问申请',data['text'])


class HarnessProposalLiftingTests(unittest.TestCase):
    """A text-only harness answer must still reach the platform as a real request."""

    def test_instructed_and_llm_shapes_are_lifted_and_removed_from_the_text(self):
        from app.harness_adapter import _proposals
        text,items=_proposals('结论正文\nDIRECTORY_REQUESTS: ["/home/demo/projects"]\n结束语')
        self.assertEqual(items,[{'root':'/home/demo/projects'}]);self.assertIn('结束语',text);self.assertNotIn('DIRECTORY_REQUESTS',text)
        text,items=_proposals('**directory_requests**: [{"path": "/Users/mac/Qwen mac", "state": "requested"}]')
        self.assertEqual(items,[{'root':'/Users/mac/Qwen mac'}]);self.assertEqual(text,'')
        text,items=_proposals('普通回答，没有申请')
        self.assertEqual(items,[]);self.assertEqual(text,'普通回答，没有申请')
        text,items=_proposals('directory_requests: 这不是 JSON')
        self.assertEqual(items,[]);self.assertIn('不是 JSON',text)

    def test_worker_and_harness_agree_on_the_convention(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('device_worker',Path(__file__).resolve().parents[1]/'packaging/client/worker.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for line in ('DIRECTORY_REQUESTS: ["/Users/mac/proj"]','directory_requests: [{"root":"/Users/mac/proj"}]'):
            self.assertEqual(module._proposals(line)[1],[{'root':'/Users/mac/proj'}])


    def test_case_variant_is_reported_with_candidates_by_the_router(self):
        """A case-only variant must come back as a rejection with the real candidate path."""
        from app.router import Router
        from app.common import encode,PREFIX
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as tmp:
            real=Path(tmp)/'Proj';real.mkdir()
            with patch('app.router.RUNTIME',Path(tmp)),patch('app.directory_requests.RUNTIME',Path(tmp)),\
                 patch('app.router.allowed',return_value=True):
                router=Router(SimpleNamespace(publish=AsyncMock()))
                row=router.tasks.submit('owner','peer','key-case','ask_peer',{'caller':'owner','projects':['p'],'question':'x'},120)
                body={'task_id':row['task_id'],'code':'OK','data':{'text':'x',
                      'directory_requests':[{'root':str(real).replace('Proj','proj')}]}}
                asyncio.run(router.task_result(SimpleNamespace(subject=f'{PREFIX}.agent.peer.result',data=encode(body))))
                data=json.loads(router.tasks.get(row['task_id'])['result'])['data']
                self.assertNotIn('directory_requests',data)
                rejection=data['directory_request_rejections'][0]
                self.assertEqual(rejection['code'],'CASE_MISMATCH')
                self.assertEqual(rejection['candidates'],[str(real)])
                self.assertIn('候选',data['text'])


if __name__=='__main__':
    unittest.main()
