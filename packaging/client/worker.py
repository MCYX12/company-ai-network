"""Standalone answerer for an enrolled device.

Lives beside client.py in the installed client directory. It reads the same
client.json and .runtime/certs, plus an optional worker.json that fixes the one
local command used to answer:

    {"prefix": "corp.dev.v1",
     "command": ["dsh", "--profile", "headless", "-"],
     "capabilities": ["get_status", "ask_peer"],
     "task_actions": ["ask_peer"],
     "timeout_seconds": 120,
     "env": {"DSH_HOME": "/home/demo/.dsh"}}

A caller's text is only ever handed to that command on stdin; the command itself
is fixed by the device owner. This process never approves access.

Reliability contract: this worker uses the SAME kernel as the built-in worker
(app/worker_kernel.py, shipped inside the client package). It validates the envelope, refuses
stale dispatch epochs, claims each task exactly once in a local SQLite ledger, checks grant
state/expiry before reading any approved material, persists its result and keeps republishing
it until the Router acknowledges it -- so a Router outage during execution no longer loses the
answer and a redelivery never runs the model twice.
"""
import argparse, asyncio, hashlib, json, os, re, shutil, signal, ssl, subprocess, tempfile, time, stat
from pathlib import Path
import nats

from app.worker_kernel import CONTRACT_VERSION, ReliableTaskRunner, WorkerLedger, sandbox_prefix, process_options, terminate_process_tree
from app import worker_kernel as _worker_kernel
from app.project_records import read_progress

ROOT = Path(__file__).resolve().parent
KERNEL_FILE=Path(_worker_kernel.__file__).resolve()


def install_digest():
    """Digest of the installed worker files, fixed at startup.

    The heartbeat otherwise only carries a configured packaging_version, so a running standalone
    client could not be checked without comparing files by hand. This lets an operator compare the
    reported install_digest with the files on disk.
    """
    digest=hashlib.sha256()
    paths=[Path(__file__).resolve(), KERNEL_FILE]
    # Backup names such as peer_client.before-utf8.py are retained, never imported.
    paths.extend(p for p in sorted((Path(__file__).resolve().parent/'app').glob('*.py')) if p.stem.isidentifier())
    paths.extend(Path(__file__).resolve().parent/n for n in ('ollama_adapter.py','dsh_desktop_adapter.py','dsh_desktop_profile.cjs','dsh_desktop_status.cjs') if (Path(__file__).resolve().parent/n).is_file())
    for path in paths:
        try:
            digest.update(path.read_bytes())
        except OSError:
            return None
    return digest.hexdigest()[:16]


INSTALL_DIGEST=install_digest()

DEFAULTS = {'prefix': 'corp.dev.v1',
            'command': ['dsh', '--profile', 'headless', '-'],
            'capabilities': ['get_status', 'ask_peer'],
            'task_actions': ['ask_peer'],
            'timeout_seconds': 120,
            'prompt_prefix': '',
            'cwd': '',
            # Device-owner opt-in: explicit extra documents added to project_notes_v1.
            'notes_docs': []}
TALK = {'get_status', 'generate_text', 'ask_peer'}
KNOWN = {'get_status', 'generate_text', 'ask_peer', 'get_project_progress'}
MAX_STDERR = 2048
# The fixed command inherits only these variables; the device owner adds anything
# else (for example DEEPSEEK_API_KEY) explicitly under "env" in worker.json.
ENV_ALLOW = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TERM', 'TMPDIR', 'TZ', 'SHELL', 'USER',
             'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'COMSPEC', 'PATHEXT')
# The versioned project_notes_v1 profile. Keep this list identical to app/local_progress.py.
NOTES_DOCS = ('README.md', 'README.txt', 'TODO.md', 'TODO.txt', 'PROGRESS.md', 'STATUS.md', 'CHANGELOG.md')
PROFILE_NAME = 'project_notes_v1'
DOC_SUFFIXES = ('.md', '.markdown', '.txt', '.rst', '.text')
MAX_NOTES_DIGESTS = 5       # 一轮最多嵌入几段已授权目录摘要
NOTES_TOTAL_BUDGET = 2700   # 摘要总长上限（与旧的 3 段各 900 的最坏情况持平）


def safe_docs(value):
    """Owner-declared extra documents: bounded, relative, document-like only.

    Mirrors app.local_progress.extra_docs. A directory approval must not silently widen
    the read set, so absolute paths, "..", hidden segments and code files are refused.
    """
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:40]:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if (not name or name.startswith('/') or '\\' in name or ':' in name or len(name) > 200
                or not name.lower().endswith(DOC_SUFFIXES)):
            continue
        parts = name.split('/')
        if len(parts) > 3 or any(p in ('', '.', '..') or p.startswith('.') for p in parts):
            continue
        if name not in out:
            out.append(name)
    return out


def effective_docs(config):
    """The device's document allowlist: the base profile plus owner-declared extras."""
    extra = safe_docs(config.get('notes_docs'))
    return list(NOTES_DOCS) + [d for d in extra if d not in NOTES_DOCS]


def _root_rank(root, question):
    """Rank one granted root: roots the question names first, then shallower, then path."""
    text = str(question or '').casefold()
    path = str(root).casefold()
    relevant = bool(text) and (path in text or any(len(s) > 1 and s in text for s in path.split('/')))
    return (0 if relevant else 1, len(Path(root).parts), path)
# The answering contract belongs to the platform, not to each device's worker.json.
# Keep this text identical to app/harness_adapter.PLATFORM_RULES.
PLATFORM_RULES = (
    '平台提问协议（必须遵守）：\n'
    '1) 需要新目录授权时，在回答最后单独一行输出：DIRECTORY_REQUESTS: ["绝对路径"]'
    '（一行合法 JSON 数组，最多 6 个；Unix 使用 / 路径，Windows 使用盘符路径并正确转义 JSON 反斜杠；路径必须是你在本设备上搜索确认存在的真实绝对路径，'
    '多候选或找不到就只列候选并结束，绝不提交猜测路径）。\n'
    '2) 不要读取、修改或直接调用平台运行目录与账本（如 .runtime-lan/ 下的文件），也不要用命令行去“直接登记”或“直查账本”。\n'
    '3) 只依据本次随附的授权资料（含已授权目录的清单/摘要）作答；未授权内容说明不足，不编造进度或百分比。\n'
    '4) 默认精简作答：先给结论（1-3 句），再列已完成/进行中或卡点/来源与时间；只保留影响结论的证据，并说明资料限制。'
    '用户明确要求详细分析时才展开，否则不要复述整份资料。')


def settings(root=None):
    root = Path(root) if root else ROOT
    value = {key: (list(val) if isinstance(val, list) else val) for key, val in DEFAULTS.items()}
    path = root / 'worker.json'
    if path.is_file() and not path.is_symlink():
        raw = path.read_bytes()
        if len(raw) > 16384:
            raise ValueError('worker.json too large')
        extra = json.loads(raw)
        if not isinstance(extra, dict):
            raise ValueError('worker.json must be an object')
        value.update(extra)
    return value


def usable_actions(capabilities, task_actions):
    caps = [c for c in capabilities if isinstance(c, str) and c in KNOWN]
    actions = [a for a in task_actions if a in TALK and a in caps]
    return caps, actions


def query_result(config, identity, body, root, now=None,activity=None):
    """Router-authorized read-only queries; never invoke the answering command."""
    now=time.time() if now is None else now
    if not isinstance(body,dict) or body.get('target_agent_id')!=identity:
        return 'FORBIDDEN',None
    expires=body.get('expires_at')
    if isinstance(expires,bool) or not isinstance(expires,(int,float)) or expires<=now:
        return 'EXPIRED',None
    action=body.get('action')
    if action not in config.get('capabilities',[]):
        return 'UNSUPPORTED_ACTION',None
    if action=='get_status':
        status,hint=preflight(config)
        return 'OK',{'synthetic':False,'agent_id':identity,'tool':config.get('tool','device-worker'),
                     'source':'device_worker_status','adapter_status':status,'hint':hint,
                     'worker_activity':activity or {'state':'unknown','progress':None,'scope':'platform_managed_queries_only'},
                     'native_work':__import__('app.native_work',fromlist=['desktop_status']).desktop_status(config,root)}
    if action=='get_project_progress':
        payload=body.get('payload')
        projects=config.get('projects')
        if not isinstance(payload,dict) or not isinstance(projects,list):
            return 'FORBIDDEN',None
        return read_progress({'agent_id':identity,'projects':projects},payload,runtime=root/'.runtime')
    return 'UNSUPPORTED_ACTION',None


def _base_env():
    return {key: value for key, value in os.environ.items() if key in ENV_ALLOW}


def preflight(config):
    """Resolve the fixed command before advertising ask_peer to the network."""
    command = config.get('command')
    # Empty arguments are allowed (claude's `--tools ''` disables tools); a blank program
    # or a non-list command is refused. Keep this rule identical to app/harness_adapter.py.
    if (not isinstance(command, list) or not command
            or not all(isinstance(x, str) for x in command) or not command[0]):
        return 'probe_failed', 'WORKER_COMMAND_INVALID'
    program = command[0]
    if os.path.sep not in program:
        resolved = shutil.which(program)
    else:
        resolved = program if os.path.isfile(program) and os.access(program, os.X_OK) else None
    if not resolved:
        return 'not_found', 'WORKER_COMMAND_NOT_FOUND'
    limit = config.get('timeout_seconds', 120)
    if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
        return 'probe_failed', 'WORKER_TIMEOUT_INVALID'
    try:
        sandbox_prefix(config)
    except ValueError:
        return 'probe_failed', 'WORKER_SANDBOX_INVALID'
    return 'ready', None


PROPOSAL_LINE = re.compile(r'(?im)^[ \t>*`_]*directory_requests[ \t]*[*_]{0,2}[ \t]*[:：][ \t]*'
                           r'(?P<body>\[.*\]|\{.*\})[ \t]*[*`_]*[ \t]*$')


def _proposals(text):
    """Lift a directory proposal out of a text-only device answer.

    Mirrors app/harness_adapter._proposals: the caller is told the platform recorded a
    request only if the server really receives one.
    """
    found = []
    def take(match):
        try:
            body = json.loads(match.group('body'))
        except ValueError:
            return match.group(0)
        items = body if isinstance(body, list) else [body]
        roots = []
        for item in items:
            if isinstance(item, str):
                roots.append(item)
            elif isinstance(item, dict):
                root = item.get('root') or item.get('path')
                if isinstance(root, str):
                    roots.append(root)
        if not roots or len(roots) > 6:
            return match.group(0)
        found.extend(roots)
        return ''
    remaining = PROPOSAL_LINE.sub(take, text)
    if not found:
        return text, []
    return remaining.strip(), [{'root': root} for root in found]


def note_path(base, name):
    """Refuse symlinks, Windows junctions/reparse points and hardlinked notes."""
    path=base/name
    for item in (path,*path.parents):
        info=item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
            raise ValueError('Linked note path')
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
        raise ValueError('Not an independent regular note file')
    path.resolve().relative_to(base.resolve())
    return path,info


def notes_digest(root, mode='safe', budget=900, docs=None, question='', coverage=None, check=lambda:None):
    from app.client_material import collect
    text,details=collect(root,list(NOTES_DOCS) if docs is None else list(docs),question,budget,mode,check)
    if coverage is not None:coverage.update(details)
    if text is None:return None
    # Preserve one bounded read-only Git observation; never infer completion from it.
    try:
        git=shutil.which('git')
        if git:
            p=subprocess.run([git,'-c','core.fsmonitor=false','-c','core.untrackedCache=false','-C',str(root),
                              'status','--porcelain=v1','-z','--untracked-files=normal'],
                             env={**os.environ,'GIT_OPTIONAL_LOCKS':'0'},capture_output=True,timeout=5)
            if p.returncode==0 and coverage is not None:
                rows=p.stdout.split(b'\0');coverage['git']={'changed':sum(bool(x) for x in rows),'untracked':sum(x.startswith(b'??') for x in rows)}
    except (OSError,subprocess.SubprocessError):pass
    return text


def run_command(config, question, deadline):
    command = config.get('command')
    # Same rule as preflight and app/harness_adapter.py: string arguments may be empty
    # (claude's `--tools ''`), the program itself may not be.
    if (not isinstance(command, list) or not command
            or not all(isinstance(x, str) for x in command) or not command[0]):
        return 'TOOL_UNAVAILABLE', None
    limit = config.get('timeout_seconds', 120)
    if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
        return 'TOOL_UNAVAILABLE', None
    timeout = min(float(limit), max(0.0, deadline - time.time() - 2))
    if timeout <= 0:
        return 'EXPIRED', None
    env = _base_env()
    for key, val in (config.get('env') or {}).items():
        if isinstance(key, str) and isinstance(val, str):
            env[key] = val
    cwd = config.get('cwd')
    if not (isinstance(cwd, str) and cwd and os.path.isdir(cwd)):
        cwd = None
    try:
        argv = sandbox_prefix(config) + list(command)
    except ValueError:
        return 'TOOL_UNAVAILABLE', {'hint': 'WORKER_SANDBOX_INVALID'}
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout,
                                    stderr=stderr, env=env, cwd=cwd,
                                    **process_options())
            command_started=time.time()
            try:
                proc.communicate(question.encode(), timeout=timeout)
                command_seconds=time.time()-command_started
            except subprocess.TimeoutExpired:
                terminate_process_tree(proc)
                proc.communicate()
                return 'EXECUTION_TIMEOUT', None
            if proc.returncode != 0:
                stderr.seek(0)
                tail = stderr.read(MAX_STDERR).decode('utf-8', errors='replace').strip()
                return 'EXECUTION_FAILED', {'exit_code': proc.returncode, 'stderr_tail': tail}
            stdout.seek(0)
            raw = stdout.read(16385)
    except FileNotFoundError:
        return 'TOOL_UNAVAILABLE', {'hint': 'WORKER_COMMAND_NOT_FOUND'}
    except (OSError, ValueError):
        return 'EXECUTION_FAILED', {'hint': 'WORKER_COMMAND_FAILED'}
    if len(raw) > 16384:
        return 'OUTPUT_TOO_LARGE', None
    text = raw.decode('utf-8', errors='replace').strip()
    if not text:
        return 'EXECUTION_FAILED', None
    # The standalone device worker also answers with text only: lift a directory proposal
    # out of it so the platform records the request instead of the caller being told
    # "I applied" while nothing exists on the server.
    text, proposals = _proposals(text)
    data = {'synthetic': False, 'tool': config.get('tool', 'device-worker'),
            'source': 'device_worker', 'text': text,
            'timings': {'command_seconds': round(command_seconds,3)}}
    if proposals:
        data['directory_requests'] = proposals
    return 'OK', data


def make_executor(config):
    """The device-specific part of the contract: build the prompt, run the fixed command.

    The kernel has already dropped any grant that is not approved or has expired, so this only
    reads directories that are valid at execution time.
    """
    async def execute(envelope, grants, history_grants):
        if envelope.get('operation') != 'ask_peer':
            return {'code': 'UNSUPPORTED_ACTION', 'data': None}
        question = (envelope.get('payload') or {}).get('question')
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1200:
            return {'code': 'INVALID_REQUEST', 'data': None}
        if config.get('history_exports_enabled') is True and any(x in question.casefold() for x in ('历史','会话','聊天','history')):
            from app.client_history import answer
            return await asyncio.to_thread(answer,Path(config.get('runtime_dir',str(ROOT/'.runtime'))),envelope,history_grants)
        mode = config.get('notes_mode', 'safe')
        docs = effective_docs(config)
        digests = []
        used_ids = []
        total = 0
        coverage=[];local_reports=[]
        selected=sorted(grants,key=lambda g:_root_rank(g['root'],question))[:MAX_NOTES_DIGESTS]
        per_root=min(2400,max(200,NOTES_TOTAL_BUDGET//max(1,len(selected))))
        def recheck():
            if time.time()>=float(envelope['deadline'])-2:raise TimeoutError('Material deadline')
            if any(g.get('expires',0)<=time.time() for g in selected):raise ValueError('Material grant expired')
        for grant in selected:
            recheck();details={'grant_id':grant.get('id')}
            local_mode=mode=='local'
            try:
                digest=notes_digest(grant['root'],mode='raw' if local_mode else mode,docs=docs,
                                    budget=per_root,question=question,coverage=details,check=recheck)
                recheck()
                if local_mode and digest:
                    from app.client_material import summarize
                    from ollama_adapter import generate
                    report=summarize(digest,details,config.get('local_summary_model'),float(envelope['deadline']),generate)
                    recheck();local_reports.append(report);digest='本机模型整理的文档声明（没有独立验证）：'+json.dumps(report,ensure_ascii=False)
                    if len(digest)>per_root:raise ValueError('Local summary over budget')
            except (OSError,ValueError,TypeError,KeyError,TimeoutError) as error:
                digest=None;details.update(complete=False,reason='LOCAL_SUMMARY_UNAVAILABLE' if local_mode else 'MATERIAL_UNAVAILABLE',error_type=type(error).__name__)
            coverage.append(details)
            if not digest:continue
            if total+len(digest)>NOTES_TOTAL_BUDGET:
                details.update(complete=False,reason='TOTAL_PROMPT_BUDGET');continue
            digests.append(digest);used_ids.append(grant.get('id'));total+=len(digest)
        if mode=='local' and selected and not digests:
            return {'code':'LOCAL_SUMMARY_UNAVAILABLE','data':{'hint':'No validated local summary; raw text was not sent to the answering command','material_coverage':coverage}}
        # Platform contract first, then device-owner local rules, then the authorized
        # material, then the caller's question.
        lead = config.get('prompt_prefix')
        parts = [PLATFORM_RULES]
        if isinstance(lead, str) and lead.strip():
            parts.append(lead.strip())
        parts.extend(digests)
        parts.append(question)
        prompt = '\n\n'.join(parts)
        recheck()
        code, data = await asyncio.to_thread(run_command, config, prompt, float(envelope['deadline']))
        recheck()
        if isinstance(data,dict):
            data['material_coverage']=coverage
            if local_reports:data['local_summaries']=local_reports
        if code == 'OK' and isinstance(data, dict) and used_ids:
            data['directory_grant_ids'] = [gid for gid in used_ids if gid]
        return {'code': code, 'data': data}
    return execute


async def publish_heartbeat(nc,subject,frame,interval=5):
    """Keep the heartbeat loop alive through temporary disconnect/buffer failures."""
    while True:
        if nc.is_connected:
            try:await nc.publish(subject,json.dumps(frame()).encode())
            except (OSError,nats.errors.Error,asyncio.TimeoutError):pass
        await asyncio.sleep(interval)


class ClientWorker:
    """NATS-agnostic task handler for one device, built on the shared reliable kernel."""

    def __init__(self, identity, ledger, execute, publish_started, publish_result):
        self.identity = identity
        self.ledger = ledger
        self.runner = ReliableTaskRunner(identity=identity, ledger=ledger, execute=execute,
                                         publish_started=publish_started, publish_result=publish_result)

    async def handle(self, body):
        if isinstance(body, dict) and body.get('type') == 'cancel':
            # A one-shot local command cannot be interrupted cooperatively; cancellation is
            # acknowledged by the built-in worker path only.
            return 'rejected'
        return await self.runner.handle(body)


async def serve(root=None):
    root = Path(root) if root else ROOT
    config = settings(root)
    config['runtime_dir']=str(root/'.runtime')
    client = json.loads((root / 'client.json').read_text())
    identity = client['identity']
    prefix = config['prefix']
    capabilities, actions = usable_actions(config.get('capabilities', []), config.get('task_actions', []))
    # Do not advertise ask_peer when the fixed command cannot be resolved; the
    # directory then shows not_found/probe_failed instead of failing a question.
    adapter_status, _hint = preflight(config)
    if adapter_status != 'ready':
        capabilities = [c for c in capabilities if c != 'ask_peer']
        actions = [a for a in actions if a != 'ask_peer']
    certs = root / '.runtime' / 'certs'
    context = ssl.create_default_context(cafile=str(certs / 'ca.crt'))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(certs / f'{identity}.crt'), str(certs / f'{identity}.key'))
    ledger = WorkerLedger(root / '.runtime' / f'worker-{identity}.sqlite3')

    state = {'draining': False, 'stopping': False}

    def update_drain():
        from app.maintenance import requested
        state['draining']=state['stopping'] or requested(identity,root/'.runtime')

    def frame():
        update_drain()
        value = {'agent_id': identity, 'status': 'online', 'capabilities': capabilities,
                 'history_consent':__import__('app.client_history',fromlist=['consent_status']).consent_status(root/'.runtime') if config.get('history_exports_enabled') is True else {'enabled':False,'expires':0,'version':None},
                 'task_actions': actions, 'adapter_status': adapter_status,
                 # 'ready' means the task subscription and outbox pump are up; 'draining'
                 # means a maintenance stop began and no new task is accepted.
                 'readiness': 'draining' if state['draining'] else 'ready',
                 # Which worker contract this installed client actually runs.
                 'build': {'contract_version': CONTRACT_VERSION,
                           'packaging_version': config.get('packaging_version'),
                           'install_digest': INSTALL_DIGEST}}
        if config.get('device_id'):
            value['device_id'] = config['device_id']
        return value

    async def beat(nc):
        await publish_heartbeat(nc,f'{prefix}.agent.{identity}.heartbeat',frame)

    async def publish_started(task_id):
        await nc.publish(f'{prefix}.agent.{identity}.result',
                         json.dumps({'type': 'task_started', 'task_id': task_id}).encode())

    async def publish_result(task_id, result):
        await nc.publish(f'{prefix}.agent.{identity}.result',
                         json.dumps({'task_id': task_id, **result}).encode())

    worker = ClientWorker(identity, ledger, make_executor(config), publish_started, publish_result)

    async def on_task(msg):
        update_drain()
        if state['draining']:
            # Claim no new work while draining; an unacked durable delivery is redelivered
            # once the worker is running again.
            return
        try:
            body = json.loads(msg.data)
        except ValueError:
            return
        await worker.handle(body)

    async def on_ack(msg):
        try:
            body = json.loads(msg.data)
        except ValueError:
            return
        if body.get('type') == 'task_result_ack' and isinstance(body.get('task_id'), str):
            ledger.acknowledge(body['task_id'])

    async def on_query(msg):
        try:
            envelope=json.loads(msg.data)
            dispatch=envelope.get('dispatch_id')
            if not isinstance(dispatch,str) or not dispatch or len(dispatch)>64:return
            activity=worker.runner.activity()
            code,data=await asyncio.to_thread(query_result,config,identity,envelope.get('request'),root,activity=activity)
            await nc.publish(f'{prefix}.agent.{identity}.reply',
                             json.dumps({'dispatch_id':dispatch,'code':code,'data':data}).encode())
        except (ValueError,TypeError,AttributeError):
            return

    async def pump(nc):
        """Republish unacknowledged results: a Router outage no longer loses the answer."""
        while True:
            if nc.is_connected:
                for task_id, result in ledger.outbox():
                    try:
                        await nc.publish(f'{prefix}.agent.{identity}.result',json.dumps({'task_id':task_id,**result}).encode())
                    except (OSError,nats.errors.Error,asyncio.TimeoutError):
                        break  # Durable outbox stays intact; retry on the next pump tick.
            await asyncio.sleep(1)

    nc = await nats.connect(servers=[client['endpoint']], tls=context, name=identity,
                            connect_timeout=5, max_reconnect_attempts=-1, reconnect_time_wait=2)
    task_sub = await nc.subscribe(f'{prefix}.agent.{identity}.task', cb=on_task)
    await nc.subscribe(f'{prefix}.agent.{identity}.query', cb=on_query)
    await nc.subscribe(f'{prefix}.client.{identity}.response', cb=on_ack)
    drain = asyncio.Event()
    def begin_drain(*_): drain.set()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try: loop.add_signal_handler(sig, begin_drain)
        except (NotImplementedError, RuntimeError): signal.signal(sig, begin_drain)
    heartbeat = asyncio.create_task(beat(nc))
    resend = asyncio.create_task(pump(nc))
    print(f'{identity} worker ready', flush=True)
    try:
        await drain.wait()
        state['stopping'] = True
        state['draining'] = True
        # Announce draining so the Router stops dispatching here, stop accepting new tasks,
        # and let the in-flight command finish instead of being killed by the service manager.
        await nc.publish(f'{prefix}.agent.{identity}.heartbeat', json.dumps(frame()).encode())
        try: await task_sub.unsubscribe()
        except Exception: pass
        grace = float(os.environ.get('POC_DRAIN_SECONDS', '15') or 0)
        if grace > 0:
            await worker.runner.wait_idle(grace)
    finally:
        heartbeat.cancel()
        resend.cancel()
        # A dead broker must not keep the process alive past the service manager's stop
        # timeout; close is best-effort and bounded.
        try: await asyncio.wait_for(nc.close(), timeout=3)
        except Exception: pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, help='installed client directory (default: this script)')
    args = parser.parse_args()
    asyncio.run(serve(args.root))
