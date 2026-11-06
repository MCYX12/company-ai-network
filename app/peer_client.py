"""Skill-facing CLI for durable remote-agent questions, no shell or remote paths."""
import argparse,asyncio,json,time,uuid,os,re
from .common import connect,query,request

class ResponseTimeout(TimeoutError):
    """TLS was established, but the control-plane/target response did not arrive."""

try:
    import nats as _nats
    # Only transport-level failures are transient. A programming error must not be retried.
    TRANSIENT=(asyncio.TimeoutError,TimeoutError,OSError,_nats.errors.Error)
except ImportError:                                   # pragma: no cover - nats is a dependency
    TRANSIENT=(asyncio.TimeoutError,TimeoutError,OSError)


def approval_granted(rows, approval_id):
    """True once the administrator approved the given pending request."""
    return any(r.get('id')==approval_id and r.get('state')=='approved' for r in rows)


def readiness_of(candidate):
    """The worker's business readiness; a missing field means an older ready worker."""
    value=candidate.get('readiness')
    return value if value in {'starting','rejoining','draining','offline'} else 'ready'


def _not_ready(candidate):
    state=readiness_of(candidate)
    return {'code':'TARGET_NOT_READY','data':{
        'target':candidate['agent_id'],'readiness':state,
        # starting/rejoining recover on their own; draining is leaving and must not be waited
        # on. The caller must not silently switch to another device.
        'retryable':state in {'starting','rejoining'},
        'hint':'目标当前状态为 %s，暂不开放问答；不要改用其他设备，稍后用同一目标重试。' % state}}


def select_target(candidates, requested=None, default=None):
    """Pick exactly one ask_peer target from the live directory.

    No device or tool is hardcoded and no peer is guessed between computers:
    an explicit --target wins, then the configured default, then a single
    eligible online peer; anything else asks the caller to choose.
    A target that is present but still starting/rejoining reports its real state
    instead of hiding behind "no eligible agent".
    Returns (agent_id, None) or (None, result).
    """
    pool=[a for a in candidates if a.get('presence')=='online' and 'ask_peer' in (a.get('available_task_actions') or [])]
    def present_not_ready(agent_id):
        return next((a for a in candidates if a.get('agent_id')==agent_id
                     and a.get('presence')=='online' and readiness_of(a)!='ready'),None)
    if requested:
        matches=[a for a in pool if a['agent_id']==requested]
        if matches:
            return matches[0]['agent_id'],None
        candidate=present_not_ready(requested)
        if candidate:
            return None,_not_ready(candidate)
        return None,{'code':'NO_ELIGIBLE_AGENT','data':{'hint':'No authorized online peer supports ask_peer','target':requested}}
    if default:
        matches=[a for a in pool if a['agent_id']==default]
        if matches:
            return matches[0]['agent_id'],None
        candidate=present_not_ready(default)
        if candidate:
            return None,_not_ready(candidate)
        return None,{'code':'NO_ELIGIBLE_AGENT','data':{'hint':'The configured default target is not an eligible online peer','default_target':default}}
    if not pool:
        return None,{'code':'NO_ELIGIBLE_AGENT','data':{'hint':'No authorized online peer supports ask_peer'}}
    if len(pool)>1:
        return None,{'code':'CLARIFY_TARGET','data':{'hint':'Multiple online peers support ask_peer; pass --target. No question was sent.',
            'candidates':[{'agent_id':a['agent_id'],'device_id':a['device_id'],'tool':a['tool']} for a in sorted(pool,key=lambda a:a['agent_id'])]}}
    return pool[0]['agent_id'],None


async def wait_task(send, wanted, wait_seconds, poll=0.5, clock=time.monotonic, sleep=asyncio.sleep):
    """Poll one task id until it reaches a terminal state or the window closes.

    The old loop slept a fixed number of 2s intervals, so per-query latency stretched the
    real wait and a shorter poll silently changed the total window. A monotonic deadline
    keeps the bound honest and trims the final sleep to the time actually left.
    """
    deadline=clock()+max(0.0,float(wait_seconds))
    terminal={'succeeded','failed','expired','cancelled'}
    while True:
        try:
            row=await send('get_task',{'task_id':wanted})
        except TRANSIENT:
            # The Router or broker is briefly unavailable (for example a supervised worker
            # restart during a real interruption). Keep waiting to the deadline; the retry
            # linkage is discovered on the next successful poll. A non-transport exception
            # propagates instead of being silently swallowed.
            row=None
        if row is not None and (row['code']!='OK' or row['data']['state'] in terminal):
            return row
        remaining=deadline-clock()
        if remaining<=0:break
        await sleep(min(max(0.01,float(poll)),remaining))
    return {'code':'PENDING','data':{'task_id':wanted,'hint':'Query this task ID again; do not submit a new task'}}


async def run(args):
    async def quiet(*_):pass
    identity=args.identity
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}',identity):raise ValueError('Invalid identity')
    if args.doctor:
        from .client_doctor import diagnose
        return await diagnose(identity)
    # A waiting question must survive a router/worker restart to follow a scheduled retry,
    # so reconnection is enabled exactly for the --wait path; the initial connect stays
    # bounded and a one-shot query still fails fast.
    nc=await connect(identity,timeout=float(os.environ.get('AGENT_CONNECT_TIMEOUT','10') or 10),
                     allow_reconnect=bool(args.wait),max_reconnect_attempts=(-1 if args.wait else 0),
                     error_cb=quiet,disconnected_cb=quiet)
    async def send(action,payload,target=None):
        body=request(identity,target or 'ag-b');body['action']=action;body['payload']=payload
        if target is None:body.pop('target_agent_id')
        try:return await query(nc,identity,body)
        except (asyncio.TimeoutError,TimeoutError):raise ResponseTimeout('No response for '+action) from None
    try:
        if args.access_status:return await send('list_access',{})
        if args.request_access:
            if not args.target:raise ValueError('--target required for approval request')
            return await send('request_access',{},args.target)
        if args.list:return await send('list_agents',{})
        if args.task_id:return await send('get_task',{'task_id':args.task_id})
        if getattr(args,'status',False):
            if not args.target:raise ValueError('--target required for current work status')
            return await send('get_status',{},args.target)
        if getattr(args,'project_progress',None):
            if not args.target:raise ValueError('--target required for project progress')
            return await send('get_project_progress',{'project_id':args.project_progress},args.target)
        question=args.question
        if not 1<=len(question.strip())<=1200:raise ValueError('Question must be 1..1200 characters')
        ttl=int(os.environ.get('AGENT_TASK_TTL_SECONDS','600') or 600)
        # The request schema caps payload.ttl_seconds at 900; anything larger is rejected
        # by the Router as INVALID_REQUEST. Fail here with the reason instead.
        # 600s keeps a long directory-grant investigation (and the human approval that
        # may land in the middle of it) inside the task deadline instead of timing out.
        if not 5<=ttl<=900:
            raise ValueError('AGENT_TASK_TTL_SECONDS must be 5..900 (schemas/query.json caps ttl_seconds at 900); got %d' % ttl)
        directory=await send('list_agents',{})
        if directory['code']!='OK':return directory
        default_target=os.environ.get('AGENT_DEFAULT_TARGET','').strip() or None
        ready_wait=float(os.environ.get('AGENT_READY_WAIT_SECONDS','30') or 0)
        until=time.monotonic()+max(0.0,ready_wait)
        while True:
            target,error=select_target(directory['data']['agents'],args.target,default_target)
            if not error:break
            detail=error.get('data') or {}
            if (error.get('code')!='TARGET_NOT_READY' or not detail.get('retryable')
                    or time.monotonic()>=until):return error
            # The same explicit target is starting/rejoining: wait a bounded window and report
            # real progress instead of silently switching device or retrying forever.
            print(json.dumps({'event':'target_not_ready','target':detail.get('target'),
                              'readiness':detail.get('readiness'),
                              'wait_seconds':round(max(0.0,until-time.monotonic()),1)},ensure_ascii=False),flush=True)
            await asyncio.sleep(min(2.0,max(0.0,until-time.monotonic())))
            directory=await send('list_agents',{})
            if directory['code']!='OK':return directory
        key=args.key or str(uuid.uuid4())
        result=await send('submit_task',{'operation':'ask_peer','question':question,'idempotency_key':key,'ttl_seconds':ttl},target)
        if result['code']=='APPROVAL_REQUIRED':
            print(json.dumps({'event':'approval_required',**(result.get('data') or {})},ensure_ascii=False),flush=True)
            approval=(result.get('data') or {}).get('approval_id')
            wait=float(os.environ.get('AGENT_APPROVAL_WAIT_SECONDS','600') or 0)
            if not args.wait or not approval or wait<=0:return result
            # Keep the question in flight: poll the approval and submit automatically
            # once the administrator grants it, so the user only had to ask once.
            until=time.time()+wait
            while time.time()<until:
                await asyncio.sleep(3)
                status=await send('list_access',{})
                rows=(status.get('data') or {}).get('requests') or [] if status.get('code')=='OK' else []
                if approval_granted(rows,approval):
                    print(json.dumps({'event':'approval_granted','approval_id':approval},ensure_ascii=False),flush=True)
                    result=await send('submit_task',{'operation':'ask_peer','question':question,'idempotency_key':key,'ttl_seconds':ttl},target)
                    break
            else:
                return {'code':'APPROVAL_PENDING','data':{'approval_id':approval,'target':target,'hint':'Waiting for administrator approval; ask again later with the same question.'}}
        if result['code']!='ACCEPTED':return result
        task_id=result['data']['task_id']
        print(json.dumps({'event':'submitted','task_id':task_id,'target':target,'idempotency_key':key},ensure_ascii=False),flush=True)
        if not args.wait:return result
        # Wait at least as long as the task's own TTL. A fixed 96s window reported a
        # remote agent that was still working as PENDING while its task was valid.
        wait_seconds=float(os.environ.get('AGENT_WAIT_SECONDS', str(max(96,ttl+15))) or 0)
        # Poll fast: the old fixed 2s sleep added up to 2s of pure waiting to every answer.
        # AGENT_POLL_SECONDS keeps the total wait window unchanged while cutting the latency.
        poll=max(0.1,float(os.environ.get('AGENT_POLL_SECONDS','0.5') or 0.5))
        # An answer that asked for a new directory used to end there: the user had to
        # watch the dashboard, approve, and ask the same question again. Keep the
        # question alive instead and resubmit it as soon as the approval lands.
        rounds=int(os.environ.get('AGENT_DIRECTORY_ROUNDS','1') or 1)
        followed=set();retry_followed=0
        # A retry is the SAME logical question, so it shares this deadline rather than
        # getting a fresh full wait window.
        question_deadline=time.monotonic()+max(0.0,wait_seconds)
        pending_wait=wait_seconds
        while True:
            result=await wait_task(send,task_id,pending_wait,poll=poll)
            if result.get('code')!='OK':return result
            # get_task returns the task row: the answer body (and its directory_requests)
            # lives under data.result.data.
            task_row=result.get('data') or {}
            retry_id=task_row.get('retry_task_id')
            # Retry following is independent of AGENT_DIRECTORY_ROUNDS: disabling directory
            # resubmission must not disable interruption recovery.
            if (retry_id and retry_followed<1 and task_id not in followed
                    and retry_id not in followed
                    and task_row.get('state') in {'failed','expired'}):
                followed.add(task_id);followed.add(retry_id);retry_followed+=1
                print(json.dumps({'event':'retrying','task_id':task_id,'retry_task_id':retry_id,
                                  'target':target,'attempt':task_row.get('retry_attempt')},
                                 ensure_ascii=False),flush=True)
                task_id=retry_id
                pending_wait=max(0.0,question_deadline-time.monotonic())
                continue
            if rounds<=0:return result
            answer=(task_row.get('result') or {}).get('data') or {}
            rejected=[r for r in (answer.get('directory_request_rejections') or []) if isinstance(r,dict)]
            if rejected:
                # The agent on the device has to search and resubmit the real path; make the
                # refusal and its candidates visible instead of looking like a silent failure.
                print(json.dumps({'event':'directory_request_rejected','target':target,'items':rejected},
                                 ensure_ascii=False),flush=True)
            pending=[r for r in (answer.get('directory_requests') or [])
                     if isinstance(r,dict) and r.get('state')=='pending' and r.get('id')]
            if not pending:return result
            rounds-=1
            pending_wait=wait_seconds
            wait=float(os.environ.get('AGENT_DIRECTORY_WAIT_SECONDS','1800') or 0)
            print(json.dumps({'event':'directory_request_pending','target':target,
                              'roots':[r.get('root') for r in pending],'ids':[r['id'] for r in pending],
                              'wait_seconds':wait},ensure_ascii=False),flush=True)
            if wait<=0:return result
            until=time.time()+wait;approved=False
            while time.time()<until:
                await asyncio.sleep(3)
                listing=await send('list_directory_requests',{})
                if listing.get('code')!='OK':return result   # older server: keep the first answer
                rows=(listing.get('data') or {}).get('requests') or []
                states={r['id']:r.get('state') for r in rows if isinstance(r,dict) and r.get('id')}
                if any(states.get(r['id']) in {'denied','revoked'} for r in pending):
                    print(json.dumps({'event':'directory_request_denied','ids':[r['id'] for r in pending]},ensure_ascii=False),flush=True)
                    return result
                if all(states.get(r['id'])=='approved' for r in pending):approved=True;break
            if not approved:
                print(json.dumps({'event':'directory_request_wait_expired','ids':[r['id'] for r in pending],
                                  'roots':[r.get('root') for r in pending],
                                  'hint':'管理员批准后重新提出同一问题即可；或调大 AGENT_DIRECTORY_WAIT_SECONDS 再试'},
                                 ensure_ascii=False),flush=True)
                return result
            print(json.dumps({'event':'directory_approved','ids':[r['id'] for r in pending],
                              'hint':'resubmitting the same question automatically'},ensure_ascii=False),flush=True)
            key=str(uuid.uuid4())
            submitted=await send('submit_task',{'operation':'ask_peer','question':question,'idempotency_key':key,'ttl_seconds':ttl},target)
            if submitted['code']!='ACCEPTED':return submitted
            task_id=submitted['data']['task_id']
            print(json.dumps({'event':'resubmitted','task_id':task_id,'target':target,'idempotency_key':key},ensure_ascii=False),flush=True)
            # A newly accepted task starts a NEW execution chain: give it its own budget, while
            # a retry inside that chain still shares this deadline instead of getting a fresh one.
            question_deadline=time.monotonic()+max(0.0,wait_seconds)
            pending_wait=wait_seconds
    finally:await nc.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--identity',default=os.environ.get('AGENT_IDENTITY','ag-a'));group=p.add_mutually_exclusive_group(required=True);group.add_argument('--doctor',action='store_true');group.add_argument('--request-access',action='store_true');group.add_argument('--access-status',action='store_true');group.add_argument('--list',action='store_true');group.add_argument('--task-id');group.add_argument('--question-stdin',action='store_true');p.add_argument('--target');p.add_argument('--key');p.add_argument('--wait',action='store_true')
    group.add_argument('--project-progress',metavar='PROJECT_ID')
    group.add_argument('--status',action='store_true',help='Read-only worker activity and tool status; no model call')
    # Parse after registering the extra read-only operation.
    args=p.parse_args()
    if args.question_stdin:
        import sys
        # Windows PowerShell may prepend a UTF-8 BOM to a native stdin pipe.
        args.question=sys.stdin.read(1202).removeprefix('\ufeff')
    try:result=asyncio.run(run(args))
    except Exception as e:
        detail=str(e)
        hint='Check connection. If a submitted event was printed, query its task_id before retrying.'
        # A rejected certificate is not a transient network fault: retrying silently
        # looks like a hang with no output. Say what actually has to be fixed.
        if isinstance(e,ResponseTimeout):
            hint=('已建立 TLS 连接，但服务器或目标未在限时内回包；刚重启时可先用 --list 查看准备状态。'
                  '已提交任务仍按 task_id 取回，不要重复提交。')
        elif isinstance(e,(asyncio.TimeoutError,TimeoutError)) or 'TimeoutError' in type(e).__name__:
            hint=('在限时内没能连上公司服务器（端点不可达/被拦，或 TLS 握手不完成）。'
                  '请核对 NATS_URL、网络与接入包后再重试；不要在这个状态下长时间空等。')
        elif 'SSL' in type(e).__name__ or 'ssl' in detail.lower() or 'CERTIFICATE' in detail or 'UNKNOWN_CA' in detail:
            hint=('本机证书未被服务器信任（CA 轮换或接入包过期），重试不会成功。'
                  '请重新导入管理员交付的接入包，确认 .runtime/certs 下的 ca.crt、<身份>.crt、<身份>.key 与服务器当前 CA 一致；'
                  '不要在这个状态下长时间等待。')
        result={'code':'QUERY_FAILED','data':{'error_type':type(e).__name__,'detail':detail,'hint':hint}}
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
