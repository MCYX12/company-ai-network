import asyncio
import json
import math
import os
import socket
import time
import uuid
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from .common import authority_base, ROOT, RUNTIME, PREFIX, connect, encode, decode
from .policy import allowed
from .conversation_access import AccessStore
from .registry import load_registry, visible, capabilities
from .adapters import describe
from .task_store import TaskStore,TERMINAL
from .lease import LeaseStore, maybe_await
from .replay_store import ReplayStore
from .admin_channel import handle as admin_handle, admins

def history_consent_metadata(consent):
    """Untrusted heartbeat metadata; malformed consent never authorizes stored history."""
    if (not isinstance(consent,dict) or set(consent)!={'enabled','version','expires'}
        or not isinstance(consent.get('enabled'),bool)
        or not isinstance(consent.get('expires'),(int,float)) or isinstance(consent['expires'],bool)
        or not math.isfinite(consent['expires'])):return {}
    version=consent.get('version')
    if consent['enabled']:
        if not isinstance(version,str) or len(version)!=64 or any(c not in '0123456789abcdef' for c in version):return {}
    elif version is not None:return {}
    return {k:consent[k] for k in ('enabled','version','expires')}

def lease_holder_name(pid=None, token=None):
    """Name this Router writes into the dispatch lease: ``<host>-<pid>-<token>``.

    Kept as one function so the shape is a contract, not a literal: drills and tests
    parse the PID out of this string, and parsing the wrong segment used to kill nothing
    while looking like a missing lease takeover (see tests/test_drill_helpers.py).
    """
    return f'{socket.gethostname()}-{os.getpid() if pid is None else pid}-{token or uuid.uuid4().hex[:8]}'


class Router:
    def __init__(self, nc):
        self.nc = nc
        # Cross-host HA (opt-in): a shared JetStream-KV ledger and a KV dispatch
        # lease replace the per-host SQLite store and file lease.
        self.ha = os.environ.get('POC_HA') == '1'
        if self.ha:
            # A diagnostic is not a control: an HA deployment without one shared authorization
            # authority must not start and then read divergent local copies.
            from .control_plane import ha_problems
            problems = ha_problems(authority_base(RUNTIME))
            if problems:
                raise RuntimeError('HA authorization authority is misconfigured: ' + '; '.join(problems))
        # Unique per Router instance: hostname-pid alone can collide for two
        # Routers in one process (and the KV lease must tell holders apart).
        self.holder = lease_holder_name()
        durable = os.environ.get('POC_JETSTREAM') == '1' or self.ha
        self.js = nc.jetstream() if (durable and hasattr(nc, 'jetstream')) else None
        replicas = int(os.environ.get('POC_JETSTREAM_REPLICAS', '1'))
        if self.ha and self.js is not None:
            from .kv_task_store import KvTaskStore
            from .kv_lease import KvLease
            self.tasks = KvTaskStore(self.js, replicas=replicas)
            self.lease = KvLease(self.js, replicas=replicas)
        else:
            self.tasks = TaskStore(authority_base(RUNTIME)/'tasks.sqlite3')
            self.lease = LeaseStore(authority_base(RUNTIME)/'leases.sqlite3') if os.environ.get('POC_DISPATCH_LEASE') == '1' else None
        self.lease_token = None
        self.lease_epoch = 0
        self.replay = ReplayStore(authority_base(RUNTIME)/'replay.sqlite3') if os.environ.get('POC_SHARED_REPLAY') == '1' else None
        self.pending = {}
        self.seen = {}
        self.presence = {}
        self.runtime_caps = {}
        self.runtime_status = {}
        # starting/rejoining/ready/draining/offline, as reported in the heartbeat. A worker
        # that has not received its first task subscription yet must not be dispatched to,
        # and a missing field defaults to ready so older workers keep working.
        self.runtime_ready = {}
        # Opt-in controlled recovery for a hard interruption (SIGKILL/power loss): one new
        # attempt of the same READ-ONLY ask_peer task on the same target. Off by default.
        self.retry_interrupted = os.environ.get('POC_RETRY_INTERRUPTED') == '1'
        self.runtime_tasks = {}
        self.runtime_build = {}
        self.runtime_history_consent = {}
        self.validator = Draft202012Validator(json.loads((ROOT/'schemas/query.json').read_text()),
                                               format_checker=FormatChecker())

    def audit(self, source, code, correlation=None):
        with (RUNTIME/'audit.jsonl').open('a') as file:
            file.write(json.dumps(dict(time=time.time(), source=source, code=code,
                                       correlation_id=correlation))+'\n')

    async def respond(self, source, correlation, code, data=None):
        self.audit(source, code, correlation)
        await self.nc.publish(f'{PREFIX}.client.{source}.response', encode(
            dict(correlation_id=correlation, code=code, data=data)))

    async def ingress(self, msg):
        source = msg.subject.split('.')[-2]
        correlation = None
        try:
            body = decode(msg.data)
            if isinstance(body, dict):
                value = body.get('correlation_id')
                if isinstance(value, str) and len(value) <= 64:
                    correlation = value
            self.validator.validate(body)
            expiry = body['expires_at']
            if not math.isfinite(expiry) or not time.time() < expiry <= time.time()+30:
                await self.respond(source, correlation, 'EXPIRED')
                return
        except (ValueError, TypeError, ValidationError):
            # No malformed payload may terminate the subscription callback.
            await self.respond(source, correlation, 'INVALID_REQUEST')
            return
        try:
            registry = load_registry()
        except (OSError, ValueError, TypeError, KeyError):
            await self.respond(source, correlation, 'DEPENDENCY_UNAVAILABLE')
            return
        if source != body['sender_agent_id']:
            await self.respond(source, correlation, 'FORBIDDEN')
            return
        # Multi-instance: only the dispatch-lease holder answers, so a request gets one reply.
        if not await self._lease_holder():
            return
        # SEC-04: a replayed message_id must not be executed twice, for any action.
        limit = int(os.environ.get('POC_REPLAY_CACHE_MAX', '100000'))
        if self.replay is not None:
            outcome = self.replay.check_and_record(source, body['message_id'], body['expires_at'], limit)
            if outcome is False:
                await self.respond(source, correlation, 'REPLAY_REJECTED')
                return
            if outcome == 'rate_limited':
                await self.respond(source, correlation, 'RATE_LIMITED')
                return
        else:
            key = (source, body['message_id'])
            if key in self.seen:
                await self.respond(source, correlation, 'REPLAY_REJECTED')
                return
            if len(self.seen) >= limit:
                await self.respond(source, correlation, 'RATE_LIMITED')
                return
            self.seen[key] = body['expires_at']
        if body['action'].startswith('admin_'):
            # Admin actions are authorized by the local admin list, not the agent registry.
            if source not in admins():
                await self.respond(source, correlation, 'FORBIDDEN')
                return
            if body['action'] in ('admin_dlq_list', 'admin_dlq_requeue'):
                # DLQ administration goes through the Router's authoritative ledger
                # (local SQLite or the shared JetStream-KV ledger under POC_HA).
                try:
                    result = await self._admin_dlq(body['action'], body['payload'], source, correlation)
                except (ValueError, TypeError, KeyError) as error:
                    await self.respond(source, correlation, str(error) or 'INVALID_REQUEST')
                    return
                except Exception:
                    await self.respond(source, correlation, 'ADMIN_ACTION_FAILED')
                    return
                await self.respond(source, correlation, 'OK', result)
                return
            try:
                result = await asyncio.to_thread(admin_handle, body['action'], body['payload'], source, correlation)
            except (ValueError, TypeError, KeyError) as error:
                await self.respond(source, correlation, str(error) or 'INVALID_REQUEST')
                return
            except Exception:
                await self.respond(source, correlation, 'ADMIN_ACTION_FAILED')
                return
            await self.respond(source, correlation, 'OK', result)
            return
        if source not in registry or not registry[source]['enabled']:
            await self.respond(source, correlation, 'FORBIDDEN')
            return
        if body['action'] == 'list_access':
            await self.respond(source,correlation,'OK',{'requests':AccessStore().listing(source)})
            return
        if body['action'] == 'list_directory_requests':
            # Caller-scoped read-only view of its own directory requests, so a client can
            # keep a question alive and resubmit the moment an approval lands instead of
            # asking the user to watch the dashboard and ask again by hand.
            from .directory_requests import DirectoryRequests
            await self.respond(source,correlation,'OK',{'requests':DirectoryRequests().for_caller(source)})
            return
        if body['action'] == 'request_access':
            target=body['target_agent_id']
            if not visible(source,target,registry) or 'ask_peer' not in capabilities(registry[target]):
                await self.respond(source,correlation,'FORBIDDEN')
                return
            projects=set(registry[source]['projects']) & set(registry[target]['projects'])
            result=AccessStore().request(source,target,projects)
            await self.respond(source,correlation,'OK',result)
            return
        if body['action'] == 'submit_task':
            target = body['target_agent_id']
            operation = body['payload']['operation']
            if operation != 'ask_peer':
                await self.respond(source,correlation,'UNSUPPORTED_ACTION')
                return
            projects=sorted(set(registry[source]['projects']) & set(registry.get(target,{}).get('projects',[])))
            task_payload={'question':body['payload']['question'],'caller':source,'projects':projects}
            if not visible(source,target,registry) or operation not in capabilities(registry.get(target,{})):
                await self.respond(source,correlation,'FORBIDDEN')
                return
            if not allowed(source,target,operation,task_payload,registry):
                # Asking is enough: file the missing conversation grant here so the
                # administrator only has to approve it, no separate request step.
                try:
                    pending=AccessStore().request(source,target,projects)
                except (OSError,ValueError,TypeError):
                    await self.respond(source,correlation,'FORBIDDEN')
                    return
                await self.respond(source,correlation,'APPROVAL_REQUIRED',
                    {**pending,'hint':'Administrator approval required; ask the same question again once it is approved.'})
                return
            from .maintenance import requested as maintenance_requested
            if maintenance_requested(target,RUNTIME):
                await self.respond(source,correlation,'TARGET_DRAINING')
                return
            try:
                row = await maybe_await(self.tasks.submit(source,target,body['payload']['idempotency_key'],operation,task_payload,body['payload']['ttl_seconds']))
            except ValueError as error:
                await self.respond(source,correlation,str(error))
                return
            await self.respond(source,correlation,'ACCEPTED',self.tasks.public(row))
            return
        if body['action'] == 'cancel_task':
            row=await maybe_await(self.tasks.get(body['payload']['task_id']))
            if not row or row['owner']!=source or not allowed(source,row['target'],row['operation'],json.loads(row['payload']),registry):
                await self.respond(source,correlation,'FORBIDDEN')
                return
            code,row=await maybe_await(self.tasks.cancel(row['task_id'],source))
            if code=='CANCEL_REQUESTED' and row:
                # Cooperative cancel over the existing task subject (no new ACL).
                await self.nc.publish(f"{PREFIX}.agent.{row['target']}.task",
                                      encode({'type':'cancel','task_id':row['task_id'],'owner':source}))
            await self.respond(source,correlation,code,self.tasks.public(row) if row else None)
            return
        if body['action'] == 'list_tasks':
            options=body['payload']
            page=await maybe_await(self.tasks.list_owned(source,
                lambda row: allowed(source,row['target'],row['operation'],json.loads(row['payload']),registry),
                state=options.get('state'),limit=options.get('limit',20),before=options.get('cursor')))
            await self.respond(source,correlation,'OK',page)
            return
        if body['action'] == 'get_task':
            row = await maybe_await(self.tasks.get(body['payload']['task_id']))
            if not row or row['owner'] != source or not allowed(source,row['target'],row['operation'],json.loads(row['payload']),registry):
                await self.respond(source,correlation,'FORBIDDEN')
                return
            data=await self.task_public(row)
            from .directory_requests import DirectoryRequests
            detail=(data.get('result') or {}).get('data') or {}
            if isinstance(detail,dict) and not DirectoryRequests().permits_ids(source,row['target'],detail.get('directory_grant_ids',[])):
                await self.respond(source,correlation,'FORBIDDEN',
                    {'reason':'DIRECTORY_GRANT_INVALID','ids':detail.get('directory_grant_ids',[])})
                return
            if self._history_result_denied(source,row,data.get('result')):
                await self.respond(source,correlation,'FORBIDDEN',{'reason':self._history_denial_reason(data.get('result'))})
                return
            data['history']=await maybe_await(self.tasks.history(row['task_id']))
            data['history_limit']=100
            data['updated_at']=data['history'][-1]['timestamp'] if data['history'] else row['created_at']
            await self.respond(source,correlation,'OK',data)
            return
        if body['action'] == 'list_agents':
            requested_project=body['payload'].get('project_id')
            if requested_project and requested_project not in registry[source]['projects']:
                await self.respond(source,correlation,'FORBIDDEN')
                return
            rows = []
            for agent_id, row in sorted(registry.items()):
                if not visible(source, agent_id, registry):
                    continue
                if body['payload'].get('project_id') and body['payload']['project_id'] not in row['projects']:
                    continue
                wanted = body['payload'].get('capability')
                if wanted and wanted not in capabilities(row):
                    continue
                age = time.time() - self.presence.get(agent_id, 0)
                ready = self.runtime_ready.get(agent_id, 'ready') if age <= 45 else 'offline'
                metadata = describe(row)
                metadata['adapter_status'] = self.runtime_status.get(agent_id, metadata['adapter_status'])
                metadata['readiness'] = ready
                # A worker that is starting, rejoining or draining advertises no answerable
                # action, so a caller is never told a target is usable before it is.
                answerable = age <= 45 and ready == 'ready'
                metadata['available_capabilities'] = self.runtime_caps.get(agent_id, []) if answerable else []
                metadata['available_task_actions'] = [a for a in self.runtime_tasks.get(agent_id, []) if a=='ask_peer'] if answerable else []
                metadata['build'] = self.runtime_build.get(agent_id) if answerable else None
                metadata['project_ids'] = sorted(set(row['projects']) & set(registry[source]['projects']))
                metadata['available_capabilities'] = [a for a in metadata['available_capabilities'] if a!='generate_text']
                rows.append(dict(agent_id=agent_id, device_id=row['device_id'],
                                 presence='online' if age <= 45 else 'suspect' if age <= 90 else 'offline',
                                 **metadata))
            await self.respond(source, correlation, 'OK', {'agents': rows})
            return
        if 'capability' in body:
            if body['capability'] != body['action']:
                await self.respond(source, correlation, 'INVALID_REQUEST')
                return
            candidates = [agent_id for agent_id in sorted(registry)
                          if agent_id != source and time.time()-self.presence.get(agent_id, 0) <= 45
                          and self.runtime_ready.get(agent_id, 'ready') == 'ready'
                          and body['action'] in self.runtime_caps.get(agent_id, [])
                          and allowed(source, agent_id, body['action'], body['payload'], registry)]
            if not candidates:
                await self.respond(source, correlation, 'NO_ELIGIBLE_AGENT')
                return
            body['target_agent_id'] = candidates[0]
        target = body['target_agent_id']
        if not visible(source, target, registry):
            await self.respond(source, correlation, 'FORBIDDEN')
            return
        if not capabilities(registry[target]):
            await self.respond(source, correlation, 'ADAPTER_NOT_INTEGRATED')
            return
        if not allowed(source, target, body['action'], body['payload'], registry):
            await self.respond(source, correlation, 'FORBIDDEN')
            return
        if len(self.pending) >= 100:
            await self.respond(source, correlation, 'RATE_LIMITED')
            return
        dispatch_id = str(uuid.uuid4())
        self.pending[dispatch_id] = (source, correlation, body['target_agent_id'], body['expires_at'], body['action'], body['payload'])
        self.audit(source, 'DISPATCHED', correlation)
        await self.nc.publish(f"{PREFIX}.agent.{body['target_agent_id']}.query", encode(
            dict(dispatch_id=dispatch_id, request=body)))

    async def task_public(self,row):
        """The task row plus any retry linkage, so a client can follow a recovery attempt.

        The original carries `retry_task_id`; the replacement carries `retry_of/attempt`.
        Without the former, a client waiting on the original would return the interruption
        and never see the recovered answer.
        """
        data=self.tasks.public(row)
        info=await maybe_await(getattr(self.tasks,'retried_from',lambda _id:None)(row['task_id']))
        if info:
            data['retry_of']=info['root_task_id'];data['attempt']=info['attempt']
        retry=await maybe_await(getattr(self.tasks,'retry_for',lambda _id:None)(row['task_id']))
        if retry:
            data['retry_task_id']=retry['retry_task_id'];data['retry_attempt']=retry['attempt']
        return data

    @staticmethod
    def _history_denial_reason(result):
        data=(result or {}).get('data') or {}
        return 'HISTORY_ACCESS_NO_LONGER_ALLOWED' if isinstance(data,dict) and data.get('source')=='local_harness_history_index' else 'HISTORY_ACCESS_DENIED_BY_ADMIN'

    def _history_result_denied(self, actor, row, result):
        """True when a history-derived answer may no longer be shown.

        The dispatch-time history snapshot is not enough: an administrator can DENY or narrow the
        history scope after the answer was produced, and an old result must not stay readable
        (assessment 2026-10-05). Only explicitly history-sourced answers are affected.
        """
        data = (result or {}).get('data') if isinstance(result, dict) else None
        from .claude_history import HISTORY_RESULT_SOURCES
        if not isinstance(data, dict) or data.get('source') not in HISTORY_RESULT_SOURCES | {'local_harness_history_index'}:
            return False
        try:
            payload = json.loads(row['payload'])
            projects = payload.get('projects') or []
            if data.get('source')=='local_harness_history_index':
                consent=data.get('owner_consent') or {}
                live=self.runtime_history_consent.get(row['target']) or {}
                scopes=data.get('history_projects')
                if (not live.get('enabled') or live.get('version')!=consent.get('version')
                    or live.get('expires',0)<=time.time() or time.time()-self.presence.get(row['target'],0)>30
                    or not isinstance(scopes,list) or not scopes or any(p not in projects for p in scopes)):
                    return True
                from .authz import decide as authz_decide
                return any(not authz_decide(actor,row['target'],'history',project=p)['allowed'] for p in scopes)
            project = projects[0] if len(projects) == 1 else None
            from .authz import decide as authz_decide
            return not authz_decide(actor, row['target'], 'history', project=project)['allowed']
        except (OSError, ValueError, TypeError, KeyError):
            return True

    async def reply(self, msg):
        source = msg.subject.split('.')[-2]
        try:
            body = decode(msg.data)
            dispatch = body['dispatch_id']
            state = self.pending.get(dispatch)
            if not state or source != state[2] or time.time() >= state[3]:
                self.audit(source, 'UNEXPECTED_REPLY')
                return
            if body.get('code') not in {'OK', 'FORBIDDEN', 'EXPIRED', 'UNSUPPORTED_ACTION', 'TOOL_UNAVAILABLE', 'DATA_UNAVAILABLE', 'DATA_INVALID'}:
                self.audit(source, 'INVALID_REPLY')
                return
            try:
                permitted = allowed(state[0], source, state[4], state[5])
            except (OSError, ValueError, TypeError, KeyError):
                permitted = False
            if not permitted:
                self.pending.pop(dispatch)
                await self.respond(state[0], state[1], 'FORBIDDEN')
                return
            self.pending.pop(dispatch)
            await self.respond(state[0], state[1], body['code'], body.get('data'))
        except (ValueError, KeyError, TypeError):
            self.audit(source, 'INVALID_REPLY')

    async def heartbeat(self, msg):
        source = msg.subject.split('.')[-2]
        try:
            registry = load_registry()
            row = registry.get(source)
            data = decode(msg.data)
            if not row or not row['enabled'] or not isinstance(data, dict):
                raise ValueError('Unknown identity')
            for field in ['agent_id', 'device_id', 'tool']:
                if field in data and data[field] != row[field]:
                    raise ValueError('Registration binding mismatch')
            claimed = data.get('capabilities', capabilities(row))
            if not isinstance(claimed,list) or not all(isinstance(c,str) for c in claimed) or not set(claimed).issubset(capabilities(row)):
                raise ValueError('Unapproved capability claim')
            status = data.get('adapter_status',describe(row)['adapter_status'])
            if status not in {'ready','status_only','not_integrated','not_found','not_probed','probe_failed','probe_timeout','unrecognized_version'}:
                raise ValueError('Invalid adapter status')
            task_actions = data.get('task_actions',[])
            if not isinstance(task_actions,list) or not all(a in {'get_status','generate_text','ask_peer'} and a in claimed for a in task_actions):
                raise ValueError('Unapproved task operation')
            readiness = data.get('readiness', 'ready')
            if readiness not in {'starting','rejoining','ready','draining','offline'}:
                raise ValueError('Invalid readiness')
            self.runtime_tasks[source] = task_actions
            self.runtime_caps[source] = claimed
            self.runtime_status[source] = status
            self.runtime_ready[source] = readiness
            # What build the device reports running (source/release/running digest + contract).
            # Optional so older clients keep working; a malformed value is ignored, never drops
            # the heartbeat.
            build = data.get('build')
            if build is None or isinstance(build, dict):
                self.runtime_build[source] = build
            consent=data.get('history_consent')
            self.runtime_history_consent[source]=history_consent_metadata(consent)
            self.presence[source] = time.time()
        except (OSError, ValueError, TypeError, KeyError):
            self.audit(source, 'REGISTRATION_REJECTED')

    async def task_result(self,msg):
        source=msg.subject.split('.')[-2]
        try:
            body=decode(msg.data)
            if not isinstance(body,dict) or not isinstance(body.get('task_id'),str):return
            if body.get('code')=='STALE_DISPATCH':
                # A worker rejected our dispatch epoch as stale. Record the refusal and,
                # when we are the current lease holder, hand the task back for redelivery.
                self.audit(source,'STALE_DISPATCH')
                row=await maybe_await(self.tasks.get(body['task_id']))
                if row and source==row['target'] and (self.lease is None or await self._lease_holder()):
                    await maybe_await(self.tasks.requeue_stale(body['task_id']))
                return
            if self.lease is not None and not await self._lease_holder():
                self.audit(source,'STALE_RESULT'); return
            if body.get('type')=='task_started':
                row=await maybe_await(self.tasks.get(body['task_id']))
                if row and source==row['target']:
                    await maybe_await(self.tasks.mark_running(body['task_id']))
                return
            row=await maybe_await(self.tasks.get(body['task_id']))
            if not row or source!=row['target']:
                self.audit(source,'UNEXPECTED_TASK_RESULT')
                return
            if body.get('code') not in {'OK','TOOL_UNAVAILABLE','EXPIRED','UNSUPPORTED_ACTION','EXECUTION_FAILED','WORKER_INTERRUPTED','CLOUD_TEST_DISABLED','EXECUTION_TIMEOUT','OUTPUT_TOO_LARGE','INVALID_REQUEST','CANCELLED','CANNOT_CANCEL','BUDGET_EXCEEDED','OUTPUT_BLOCKED','FORBIDDEN','LOCAL_SUMMARY_UNAVAILABLE','HISTORY_NOT_CONFIGURED','HISTORY_SCOPE_REQUIRED','HISTORY_UNAVAILABLE'}:
                return
            try:permitted=allowed(row['owner'],source,row['operation'],json.loads(row['payload']))
            except (OSError,ValueError,TypeError,KeyError):permitted=False
            result={'code':body['code'],'data':body.get('data')} if permitted else {'code':'FORBIDDEN','data':None}
            if permitted and row['operation'] in {'ask_peer','generate_text'} and isinstance(result.get('data'),dict):
                # Cross-boundary answers are untrusted. One policy at the server
                # boundary covers the one-shot harness and the packaged device
                # worker even if a device's own guard is bypassed or outdated.
                from .output_guard import guard_result
                result['code'],result['data']=guard_result(result['code'],result['data'])
            if permitted and row['operation']=='ask_peer' and row['state'] in {'accepted','queued','dispatched','running'} and isinstance(result.get('data'),dict):
                from .directory_requests import DirectoryRequests,reason_text,reject_code,RootRejected
                proposals=result['data'].pop('directory_requests',[])
                if isinstance(proposals,list):
                    requests=[];rejections=[]
                    if len(proposals)>6:
                        rejections.append({'root':None,'code':'TOO_MANY','reason':reason_text('TOO_MANY')})
                        proposals=proposals[:6]
                    for proposal in proposals:
                        try:
                            if not isinstance(proposal,dict) or set(proposal)!={'root'}:raise RootRejected('BAD_TYPE',None)
                            item=DirectoryRequests().request(row['owner'],source,proposal['root'])
                            requests.append({k:item[k] for k in ['id','root','profile','state','expires']}
                                            |{k:item[k] for k in ('requested_root','canonicalized','also_approved_for') if k in item})
                        except RootRejected as error:
                            # A refused root used to vanish silently, which reads as "the
                            # agent could not answer". Report the code, the reason and the
                            # real candidates so the agent on the device can search and
                            # resubmit the exact path itself.
                            rejection={'root':proposal.get('root') if isinstance(proposal,dict) else None,
                                       'code':reject_code(error),'reason':str(error)}
                            if error.candidates:rejection['candidates']=error.candidates
                            rejections.append(rejection)
                        except (ValueError,TypeError) as error:
                            rejections.append({'root':proposal.get('root') if isinstance(proposal,dict) else None,
                                               'code':'REFUSED','reason':str(error)[:200]})
                    lines=[]
                    if requests:
                        result['data']['directory_requests']=requests
                        for r in requests:
                            line=r['root']+' · '+r['state']+' · '+r['id']
                            if r.get('requested_root'):line+='（原请求 '+r['requested_root']+'，已按设备上实际名称规范化）'
                            if r.get('also_approved_for'):line+='（同一目录已对 '+', '.join(r['also_approved_for'])+' 授权；本调用方仍需单独批准）'
                            lines.append(line)
                        lines.append('申请已交管理员处理；批准前不因申请而获得目录读取权限。')
                    if rejections:
                        result['data']['directory_request_rejections']=rejections
                        lines.append('资料访问申请被拒绝：'+'；'.join(
                            (r['root'] or '(无路径)')+' · '+r['reason']+
                            ('（候选：'+', '.join(r['candidates'])+'）' if r.get('candidates') else '')
                            for r in rejections))
                    if lines:
                        result['data']['text']=result['data'].get('text','')+'\n\n'+'\n'.join(lines)
            if permitted and isinstance(result.get('data'),dict) and row['operation']=='ask_peer':
                # Reconciliation: a harness running on this host can reach the runtime
                # directory and file a request out of band. The platform is still the
                # authority, so adopt any request that appeared during this task into the
                # answer -- otherwise the caller can neither see nor wait for it.
                # Best effort only: it must never break result finalisation.
                try:
                    from .directory_requests import DirectoryRequests
                    listing=getattr(DirectoryRequests(),'listing',None)
                    if listing:
                        known={r.get('id') for r in (result['data'].get('directory_requests') or []) if isinstance(r,dict)}
                        extra=[r for r in listing(row['owner'],source)
                               if r['state']=='pending' and r['id'] not in known and r['created']>=row['created_at']-1]
                        if extra:
                            result['data']['directory_requests']=(result['data'].get('directory_requests') or [])+ \
                                [{k:r[k] for k in ['id','root','profile','state','expires']}|{'registered_by':'platform_reconciliation'} for r in extra]
                            result['data']['text']=result['data'].get('text','')+'\n\n平台登记的资料访问申请（本机侧直接登记，已并入本次回答）：'+ \
                                '；'.join(r['root']+' · '+r['state']+' · '+r['id'] for r in extra)
                except (AttributeError,TypeError,ValueError,KeyError,OSError):
                    pass
            if isinstance(result.get('data'),dict):
                from .directory_requests import DirectoryRequests
                ids=result['data'].get('directory_grant_ids',[])
                if not DirectoryRequests().permits_ids(row['owner'],source,ids):
                    result={'code':'FORBIDDEN','data':{'reason':'DIRECTORY_GRANT_INVALID',
                                                       'hint':'结果携带的 directory_grant_ids 不是当前有效授权（可能已被撤销/过期）',
                                                       'ids':ids}}
            if permitted and self._history_result_denied(row['owner'],row,result):
                # Refuse to store a history answer under a now-denied/narrowed admin scope, so the
                # result cannot later be read back from the ledger (assessment 2026-10-05).
                result={'code':'FORBIDDEN','data':{'reason':self._history_denial_reason(result)}}
            if (self.retry_interrupted and body.get('code')=='WORKER_INTERRUPTED'
                    and row['operation']=='ask_peer' and permitted):
                # Read-only, same target, at most once; authorization is re-checked when the
                # new task is dispatched. The original stays a recorded failure.
                if not hasattr(self.tasks,'record_retry'):
                    # A shared-KV TaskStore has no retry linkage; never claim recovery there.
                    self.audit(row['target'],'RETRY_UNSUPPORTED',row['task_id'])
                elif (not await maybe_await(self.tasks.is_retry(row['task_id']))
                        and await maybe_await(self.tasks.retry_for(row['task_id'])) is None):
                    await self._schedule_retry(row)
            finisher=getattr(self.tasks,'finish_status',None)
            if finisher is not None:
                status=await maybe_await(finisher(row['task_id'],result))
            else:
                await maybe_await(self.tasks.finish(row['task_id'],result));status='committed'
            if status=='retry':
                # The authoritative ledger did not persist the result. Do NOT acknowledge: the
                # Worker keeps resending its outbox until the write is durable (assessment P1).
                self.audit(source,'RESULT_WRITE_RETRY')
                return
            await self.nc.publish(f'{PREFIX}.client.{source}.response',encode({'type':'task_result_ack','task_id':row['task_id']}))
        except (ValueError,TypeError,KeyError):
            self.audit(source,'INVALID_TASK_RESULT')

    async def _schedule_retry(self,row):
        """Queue one replacement task for an interrupted read-only task; never loops."""
        now=time.time()
        remaining=int(min(900,max(0,row['deadline']-now)))
        if remaining<5:
            self.audit(row['target'],'RETRY_SKIPPED_NO_TIME',row['task_id'])
            return None
        try:
            payload=json.loads(row['payload'])
            if hasattr(self.tasks,'submit_retry'):
                # New task and linkage commit together, so the client can always follow it.
                new=await maybe_await(self.tasks.submit_retry(row['task_id'],row['owner'],row['target'],
                                            row['operation'],payload,remaining))
            else:
                new=await maybe_await(self.tasks.submit(row['owner'],row['target'],'retry-'+row['task_id'],
                                                        row['operation'],payload,remaining))
                self.tasks.record_retry(row['task_id'],new['task_id'],2,'WORKER_INTERRUPTED')
        except (ValueError,TypeError,KeyError):
            self.audit(row['target'],'RETRY_FAILED',row['task_id'])
            return None
        self.audit(row['target'],'RETRY_SCHEDULED',row['task_id'])
        return new['task_id']

    async def _admin_dlq(self, action, payload, source, correlation):
        """List or requeue dead letters on the Router's authoritative ledger."""
        from . import admin_channel
        payload = payload or {}
        if action == 'admin_dlq_list':
            limit = int(payload.get('limit', 100))
            if not 1 <= limit <= 1000:
                raise ValueError('INVALID_REQUEST')
            rows = await maybe_await(self.tasks.dead_letters(limit))
            keys = ['task_id', 'owner', 'target', 'operation', 'attempts', 'created_at', 'deadline']
            result = {'dead_letters': [{k: r[k] for k in keys} for r in rows], 'summary': {'count': len(rows)}}
        else:
            task_id = payload.get('task_id')
            if not isinstance(task_id, str):
                raise ValueError('INVALID_REQUEST')
            row = await maybe_await(self.tasks.requeue(task_id, actor='admin-channel'))
            result = {'task_id': task_id, 'state': row['state'], 'summary': {'task_id': task_id}}
        admin_channel._audit(RUNTIME, source, action, correlation, result.get('summary', {}))
        return result

    async def _lease_holder(self):
        """True when this Router holds the shared dispatch lease (always true without one).

        Works with the synchronous SQLite LeaseStore and the async JetStream-KV lease.
        """
        if self.lease is None:
            return True
        now = time.time()
        ttl = float(os.environ.get('POC_LEASE_TTL', '15'))
        lease = await maybe_await(self.lease.renew('dispatch', self.holder, self.lease_token, ttl=ttl, now=now)) if self.lease_token else None
        if lease is None:
            lease = await maybe_await(self.lease.acquire('dispatch', self.holder, ttl=ttl, now=now))
        if not lease:
            return False
        self.lease_token = lease['token']
        self.lease_epoch = lease['epoch']
        return True

    async def dispatch_tasks(self):
        now=time.time()
        if not await self._lease_holder():return
        def permitted(row):
            try:return row['operation']=='ask_peer' and allowed(row['owner'],row['target'],row['operation'],json.loads(row['payload']))
            except (OSError,ValueError,TypeError,KeyError):return False
        def eligible(target,operation):
            from .maintenance import requested as maintenance_requested
            return (now-self.presence.get(target,0)<=45
                    and self.runtime_ready.get(target,'ready')=='ready'
                    and not maintenance_requested(target,RUNTIME)
                    and operation in self.runtime_tasks.get(target,[]))
        for row in await maybe_await(self.tasks.plan(now,permitted,eligible,reserve=self.js is not None)):
            body={key:row[key] for key in ['task_id','target','operation','deadline']}
            body['payload']=json.loads(row['payload'])
            from .directory_requests import DirectoryRequests
            from .history_access import HistoryAccess
            body['notes_grants']=DirectoryRequests().dispatch(row['owner'],row['target'],row['deadline'])
            body['history_grants']=HistoryAccess().grants(row['owner'],row['target'],row['deadline'])
            if self.lease is not None:
                body['lease_epoch']=self.lease_epoch  # fencing: downstream rejects stale holders
            if self.js is not None:
                from . import jetstream_bus
                try:
                    await asyncio.wait_for(jetstream_bus.publish(self.js, PREFIX, body),
                                           timeout=float(os.environ.get('POC_JETSTREAM_PUBLISH_TIMEOUT', '5')))
                except Exception:
                    # PubAck never arrived (or timed out): keep the task queued.
                    await maybe_await(self.tasks.record_send_failure(row['task_id'], now))
                    continue
                await maybe_await(self.tasks.sent(row['task_id']))
                # Durable dispatch plus the Core copy. The stream is consumed only by
                # worker identities (their ACLs carry $JS.API./_INBOX.); a client-only
                # identity or a custom worker that implements just the Core task
                # subject still receives the task this way. The worker ledger claims by
                # task_id, so a JetStream-capable worker never executes it twice.
                await self.nc.publish(f"{PREFIX}.agent.{row['target']}.task",encode(body))
            else:
                await self.nc.publish(f"{PREFIX}.agent.{row['target']}.task",encode(body))

    async def sweep(self):
        while True:
            now = time.time()
            self.seen = {key: expiry for key, expiry in self.seen.items() if expiry > now}
            for key, state in list(self.pending.items()):
                if state[3] <= now:
                    self.pending.pop(key, None)
                    await self.respond(state[0], state[1], 'TIMEOUT')
            await self.dispatch_tasks()
            await asyncio.sleep(.5)

    async def run(self):
        if self.js is not None:
            from . import jetstream_bus
            try:
                await jetstream_bus.ensure(self.js, 'TASKS', PREFIX,
                                           replicas=int(os.environ.get('POC_JETSTREAM_REPLICAS', '1')))
            except Exception:
                # JetStream requested but unavailable (server started without it):
                # fall back to Core NATS instead of crashing the Router.
                self.js = None
                self.audit('router', 'JETSTREAM_UNAVAILABLE')
                print('jetstream unavailable; using Core NATS', flush=True)
        await self.nc.subscribe(f'{PREFIX}.ingress.*.request', cb=self.ingress)
        await self.nc.subscribe(f'{PREFIX}.agent.*.reply', cb=self.reply)
        await self.nc.subscribe(f'{PREFIX}.agent.*.heartbeat', cb=self.heartbeat)
        await self.nc.subscribe(f'{PREFIX}.agent.*.result', cb=self.task_result)
        await self.nc.flush()
        print('router ready', flush=True)
        await self.sweep()

async def main():
    nc = await connect('router')
    try:
        await Router(nc).run()
    finally:
        await nc.close()

if __name__ == '__main__':
    asyncio.run(main())
