"""Browser permission acceptance with explicit terminal/refusal checks and exit codes.

Task ids come from the page's own submit response, not from the ledger. Success requires the
server-side state to be succeeded with result code OK; a cross-identity exception only passes when
it carries the project's refusal code; revoke must exit 0 and be reflected before re-retrieval.
Exit code 0 only when every required assertion passed.

    python browser_acceptance.py --a-url ... --b-url ... --token-a=... --token-b=... --runtime ... \
        --revoke-cmd '...' --out result.json
"""
import argparse, json, subprocess, sys, time, uuid
from pathlib import Path

TERMINAL={'succeeded','failed','expired','cancelled'}
REFUSAL_CODES=('FORBIDDEN',)


def api(page,action,payload):
    return page.evaluate("async ([action,payload])=>api(action,payload)",[action,payload])


def wait_for_task(page,timeout=30):
    deadline=time.time()+timeout
    previous=set()
    stored=page.evaluate("()=>Object.entries(localStorage).filter(([k])=>k.indexOf('companyAgentLastTask')===0).map(([k,v])=>v)")
    previous=set(stored)
    while time.time()<deadline:
        current=page.evaluate("()=>Object.entries(localStorage).filter(([k])=>k.indexOf('companyAgentLastTask')===0).map(([k,v])=>v)")
        new=[value for value in current if value not in previous]
        if new:
            return new[0]
        time.sleep(0.5)
    return None


def wait_terminal(page,task_id,timeout=60):
    deadline=time.time()+timeout;last=None
    while time.time()<deadline:
        response=api(page,'task',{'task_id':task_id})
        data=response.get('data') if isinstance(response,dict) else None
        last=data or last
        if isinstance(data,dict) and data.get('state') in TERMINAL:
            return data
        time.sleep(1)
    return last or {}


def body_text(data):
    result=(data or {}).get('result') if isinstance(data,dict) else None
    inner=(result or {}).get('data') if isinstance(result,dict) else None
    return (inner or {}).get('text') if isinstance(inner,dict) else None


def retrieve(page,task_id):
    response=api(page,'task',{'task_id':task_id})
    data=response.get('data') if isinstance(response,dict) else None
    return {'code':response.get('code') if isinstance(response,dict) else None,
            'has_text':bool(body_text(data)),'state':(data or {}).get('state') if isinstance(data,dict) else None}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--a-url',required=True);parser.add_argument('--b-url',required=True)
    parser.add_argument('--token-a',required=True);parser.add_argument('--token-b',required=True)
    parser.add_argument('--runtime',required=True)
    parser.add_argument('--profile',default='full',choices=('full','cross-identity'))
    parser.add_argument('--revoke-cmd',default='')
    parser.add_argument('--out',required=True)
    args=parser.parse_args()
    results={};required={}
    from playwright.sync_api import sync_playwright
    with sync_playwright() as played:
        browser=played.firefox.launch()
        ctx_a=browser.new_context();ctx_a.add_cookies([{'name':'company_admin','value':args.token_a,'url':args.a_url}])
        ctx_b=browser.new_context();ctx_b.add_cookies([{'name':'company_admin','value':args.token_b,'url':args.b_url}])
        page_a=ctx_a.new_page();page_a.goto(args.a_url+'#chat');page_a.wait_for_selector('#chat-form',state='visible')
        page_b=ctx_b.new_page();page_b.goto(args.b_url+'#chat');page_b.wait_for_selector('#chat-form',state='visible')
        for page in (page_a,page_b):
            page.wait_for_function("()=>document.querySelector('#chat-target') && document.querySelector('#chat-target').options.length>0",timeout=20000)
        marker='权限验收-A-'+uuid.uuid4().hex[:8]
        captured={}
        def on_response(response):
            if '/api/chat/send' in response.url:
                try:captured['send']=response.json()
                except Exception:pass
        page_a.on('response',on_response)
        page_a.fill('#chat-question',marker);page_a.click('#chat-send')
        for _ in range(20):
            if 'send' in captured:
                break
            page_a.wait_for_timeout(200)
        task_a=((captured.get('send') or {}).get('data') or {}).get('task_id') or wait_for_task(page_a)
        results['A_task']=task_a
        results['A_task_source']=('submit-response' if (captured.get('send') or {}).get('data',{}).get('task_id') else 'localStorage')
        if not task_a:
            results['error']='submit produced no task id'
        else:
            state_a=wait_terminal(page_a,task_a)
            results['A_terminal']={'state':state_a.get('state'),'result_code':(state_a.get('result') or {}).get('code')}
            results['A_succeeded']=bool(state_a.get('state')=='succeeded'
                                        and (state_a.get('result') or {}).get('code')=='OK')
            required['A_succeeded']=results['A_succeeded']
            # cross-identity retrieval
            try:
                cross=retrieve(page_b,task_a)
                results['B_cross_identity']=cross
                results['B_cross_identity_pass']=bool(cross['code'] in REFUSAL_CODES and not cross['has_text'])
            except Exception as error:
                message=str(error)
                results['B_cross_identity']={'error':message[:80]}
                results['B_cross_identity_pass']=any(code in message for code in REFUSAL_CODES)
            required['B_cross_identity_pass']=results['B_cross_identity_pass']
            # B's own task: capture B's submit response too (no post-click diff race)
            captured_b={}
            def on_response_b(response):
                if '/api/chat/send' in response.url:
                    try:captured_b['send']=response.json()
                    except Exception:pass
            page_b.on('response',on_response_b)
            page_b.fill('#chat-question','权限验收-B');page_b.click('#chat-send')
            for _ in range(20):
                if 'send' in captured_b:
                    break
                page_b.wait_for_timeout(200)
            task_b=((captured_b.get('send') or {}).get('data') or {}).get('task_id') or wait_for_task(page_b)
            state_b=wait_terminal(page_b,task_b) if task_b else {}
            results['B_own']={'task':task_b,'state':state_b.get('state'),
                              'result_code':(state_b.get('result') or {}).get('code')}
            results['B_own_success']=bool(task_b and state_b.get('state')=='succeeded'
                                          and (state_b.get('result') or {}).get('code')=='OK')
            required['B_own_success']=results['B_own_success']
            # B's task list: require a well-formed OK response with a real array, a positive
            # control (B's own task present), no A task, and no leakage of A's unique marker.
            listing=api(page_b,'tasks',{})
            code=listing.get('code') if isinstance(listing,dict) else None
            data=listing.get('data') if isinstance(listing,dict) else None
            tasks=data.get('tasks') if isinstance(data,dict) else None
            valid=bool(code=='OK' and isinstance(tasks,list))
            ids={t.get('task_id') for t in tasks} if valid else set()
            results['B_list']={'code':code,'tasks_valid':valid,'count':(len(tasks) if valid else None)}
            results['B_list_has_own_task']=bool(task_b and task_b in ids)
            results['B_list_contains_A_task']=bool(task_a in ids)
            results['B_list_no_A_marker']=bool(marker not in json.dumps(listing,ensure_ascii=False))
            results['B_list_isolated']=bool(valid and results['B_list_has_own_task']
                                            and not results['B_list_contains_A_task']
                                            and results['B_list_no_A_marker'])
            required['B_list_isolated']=results['B_list_isolated']
            results['same_origin_identity_switch']='NOT_APPLICABLE (dashboard identity is fixed per process)'
            # post-revoke retrieval
            if args.revoke_cmd:
                revoke=subprocess.run(args.revoke_cmd,shell=True,capture_output=True,text=True,
                                      cwd=str(Path(__file__).resolve().parents[1]))
                try:
                    grant=json.loads(revoke.stdout.strip().splitlines()[-1])
                    revoked=revoke.returncode==0 and int(grant.get('revoked',0))>=1
                except (ValueError,IndexError,AttributeError):
                    revoked=False
                results['revoke']={'rc':revoke.returncode,'stdout':revoke.stdout.strip()[-80:],
                                   'revoked_confirmed':revoked}
                if revoked:
                    after=retrieve(page_a,task_a)
                    results['A_after_revoke']=after
                    results['A_after_revoke_pass']=bool(after['code'] in REFUSAL_CODES and not after['has_text'])
                else:
                    results['A_after_revoke_pass']=False
                required['A_after_revoke_pass']=results['A_after_revoke_pass']
            elif args.profile=='full':
                results['A_after_revoke_pass']='NOT_RUN'
                results['blocked']='full profile requires --revoke-cmd'
            else:
                results['A_after_revoke_pass']='NOT_RUN (cross-identity profile)'
        browser.close()
    results['profile']=args.profile
    results['required']=required
    results['all_pass']=bool(required) and all(required.values()) and not results.get('blocked')
    results['exit_code']=0 if results['all_pass'] else 1
    Path(args.out).write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print(json.dumps(results,ensure_ascii=False))
    return results['exit_code']


if __name__=='__main__':
    raise SystemExit(main())
