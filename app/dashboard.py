"""Local single-user control panel. All business operations pass through Router ACLs."""
import argparse
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit
import uuid
from .common import ROOT, connect, query, request

WEB=ROOT/'web'

def plan_status():
    rows=[]
    for line in (ROOT/'docs/PLAN_STATUS.md').read_text().splitlines():
        if line.startswith('| P'):
            cols=[c.strip() for c in line.strip('|').split('|')]
            if len(cols)==4:rows.append(dict(phase=cols[0],status=cols[1],done=cols[2],remaining=cols[3]))
    return rows

class Bridge:
    def __init__(self,identity):
        self.identity=identity;self.nc=None
        self.loop=asyncio.new_event_loop()
        self.thread=threading.Thread(target=self.loop.run_forever,daemon=True);self.thread.start()
        self.lock=None
    async def _call(self,action,payload,target):
        if self.lock is None:self.lock=asyncio.Lock()
        async with self.lock:
            if self.nc is None or self.nc.is_closed:
                self.nc=await connect(self.identity,max_reconnect_attempts=0)
            if not self.nc.is_connected:raise ConnectionError('not connected')
            body=request(self.identity,target or 'ag-b');body['action']=action;body['payload']=payload
            if target is None:body.pop('target_agent_id')
            return await query(self.nc,self.identity,body)
    def call(self,action,payload=None,target=None):
        future=asyncio.run_coroutine_threadsafe(self._call(action,payload or {},target),self.loop)
        try:return future.result(timeout=9)
        except BaseException:
            future.cancel();raise
    def close(self):
        if self.nc:
            try:asyncio.run_coroutine_threadsafe(self.nc.close(),self.loop).result(3)
            except Exception:pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(3)

class DashboardServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,bridge,admin=False,accounts=None):
        self.accounts=accounts
        self.admin=admin
        self.bridge=bridge;self.token=secrets.token_urlsafe(32)
        super().__init__(address,Handler)
        self.hosts={f'127.0.0.1:{self.server_port}',f'localhost:{self.server_port}'}

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup();self.connection.settimeout(12)
    def log_message(self,*_):pass
    def reply(self,status,value,kind='application/json; charset=utf-8'):
        raw=json.dumps(value,ensure_ascii=False).encode() if kind.startswith('application/json') else value
        self.send_response(status)
        self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)))
        if getattr(self,'set_cookie',None):self.send_header('Set-Cookie',self.set_cookie)
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers();self.wfile.write(raw)
    def trusted(self,token=False):
        host=self.headers.get('Host','')
        if host not in self.server.hosts:return False
        origin=self.headers.get('Origin')
        if origin and origin!='http://'+host:return False
        if self.headers.get('Sec-Fetch-Site')=='cross-site':return False
        return not token or secrets.compare_digest(self.headers.get('X-Dashboard-Token','').encode(),self.server.token.encode())
    def session_token(self):
        from http.cookies import SimpleCookie
        c=SimpleCookie()
        try:c.load(self.headers.get('Cookie',''))
        except Exception:return ''
        return c['company_admin'].value if 'company_admin' in c else ''
    def administrator(self):
        return self.server.accounts.who(self.session_token()) if self.server.accounts else None
    def do_GET(self):
        if not self.trusted():return self.reply(403,{'error':'LOCAL_ACCESS_ONLY'})
        path=urlsplit(self.path).path
        files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
        files['/login.js']=('login.js','text/javascript; charset=utf-8')
        if path in files:
            name,kind=files[path];
            if name=='index.html' and self.server.accounts and not self.administrator():name='login.html'
            raw=(WEB/name).read_bytes()
            if name in {'index.html','login.html'}:raw=raw.replace(b'__TOKEN__',self.server.token.encode())
            return self.reply(200,raw,kind)
        if path=='/api/project-status':
            if not self.trusted(token=True):return self.reply(403,{'error':'INVALID_SESSION'})
            if self.server.accounts and not self.administrator():return self.reply(401,{'error':'LOGIN_REQUIRED'})
            from .project_state import statuses
            return self.reply(200,{'projects':statuses(),'scope':'本机项目摘要快照与准备状态'})
        if path=='/api/health':
            if not self.trusted(token=True):return self.reply(403,{'error':'INVALID_SESSION'})
            if self.server.accounts and not self.administrator():return self.reply(401,{'error':'LOGIN_REQUIRED'})
            # Delivery health for the console: the same snapshot scripts/alerts.py evaluates.
            # Kept read-only and tolerant: a missing file is normal before the first timer run.
            from .common import RUNTIME
            from .registry import admission_snapshot
            admission=admission_snapshot()
            try:
                oidc_sync=json.loads((RUNTIME/'health/oidc-sync.json').read_text())
                from .oidc_status import valid as valid_status
                if not valid_status(oidc_sync):raise ValueError('Invalid OIDC sync snapshot')
            except FileNotFoundError:
                oidc_sync=None
            except (OSError,ValueError):
                from .oidc_status import invalid
                oidc_sync=invalid()
            snapshot=RUNTIME/'health/latest.json'
            try:
                data=json.loads(snapshot.read_text())
                if (not isinstance(data,dict) or data.get('status') not in ('ok','warn','fail')
                    or not isinstance(data.get('checks'),list)
                    or any(not isinstance(c,dict) or not isinstance(c.get('check'),str) or not c['check']
                           or c.get('status') not in ('ok','warn','fail') for c in data['checks'])):
                    raise ValueError('Invalid health snapshot')
                expected='fail' if any(c['status']=='fail' for c in data['checks']) else 'warn' if any(c['status']=='warn' for c in data['checks']) else 'ok'
                if data['status']!=expected:raise ValueError('Inconsistent health snapshot')
            except FileNotFoundError:
                return self.reply(200,{'available':False,'status':'unknown','oidc_sync':oidc_sync,
                    'admission_problems':admission.get('problems') or [],
                    'admission_snapshot_error':admission.get('snapshot_error'),
                    'detail':'尚无健康检查记录（company-agent-healthcheck.timer 每 5 分钟写一次）'})
            except (OSError,ValueError):
                return self.reply(200,{'available':False,'status':'unknown','oidc_sync':oidc_sync,
                    'snapshot_error':'HEALTH_SNAPSHOT_INVALID',
                    'admission_problems':admission.get('problems') or [],
                    'admission_snapshot_error':admission.get('snapshot_error'),
                    'detail':'健康检查记录损坏或不可读，当前状态未知；请检查健康任务与磁盘权限。'})
            if admission.get('snapshot_error'):
                return self.reply(200,{'available':False,'status':'unknown','oidc_sync':oidc_sync,
                    'snapshot_error':admission['snapshot_error'],
                    'admission_snapshot_error':admission['snapshot_error'],
                    'admission_problems':[],
                    'detail':'身份准入诊断记录损坏或不可读；请检查 Router 日志与磁盘权限，不能据此判断准入正常。'})
            checks=data.get('checks') or []
            non_ok=[c for c in checks if c.get('status') not in ('ok',)]
            ledger=next((c for c in checks if c.get('check')=='task_ledger'),None)
            return self.reply(200,{'available':True,'status':data.get('status'),
                'oidc_sync':oidc_sync,
                'checked_at':data.get('checked_at') or data.get('generated_at') or data.get('started_at'),
                'generated_at':data.get('checked_at') or data.get('generated_at') or data.get('started_at'),
                'checks':non_ok,
                'problems':[c.get('check') for c in non_ok],
                'admission_problems':admission.get('problems') or [],
                'admission_observed_at':admission.get('observed_at'),
                'task_ledger':(ledger or {}).get('detail'),
                'scope':'本机运行时健康快照；非正常检查含原因与处理建议；死信区分历史累计与近 24h，并按目标智能体分组'})
        if path!='/api/bootstrap':return self.reply(404,{'error':'NOT_FOUND'})
        if not self.trusted(token=True):return self.reply(403,{'error':'INVALID_SESSION'})
        if self.server.accounts and not self.administrator():return self.reply(401,{'error':'LOGIN_REQUIRED'})
        from .site import defaults
        return self.reply(200,{'account':self.administrator(),'admin':self.server.admin,'identity':self.server.bridge.identity,'defaults':defaults(),'plan':plan_status(),'scope':'本人授权范围 · 本机控制台'})
    def do_POST(self):
        if not self.trusted(token=True):return self.reply(403,{'error':'INVALID_SESSION'})
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.reply(415,{'error':'JSON_REQUIRED'})
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=16384:raise ValueError()
            body=json.loads(self.rfile.read(size))
            if not isinstance(body,dict):raise ValueError()
            path=urlsplit(self.path).path
            target=None
            if path.startswith('/api/auth/') and self.server.accounts:
                accounts=self.server.accounts
                if path=='/api/auth/state' and not body:return self.reply(200,{'initialized':accounts.initialized(),'account':self.administrator()})
                try:
                    if path=='/api/auth/register' and set(body)=={'code','name','password'}:
                        accounts.register_admin(body['code'],body['name'],body['password']);return self.reply(200,{'ok':True})
                    if path=='/api/auth/login' and set(body)=={'name','password'}:
                        token=accounts.login(body['name'],body['password']);self.set_cookie='company_admin='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600';return self.reply(200,{'ok':True})
                    if path=='/api/auth/password' and set(body)=={'old_password','new_password'} and self.administrator():
                        accounts.change_password(self.administrator(),body['old_password'],body['new_password']);return self.reply(200,{'ok':True})
                    if path=='/api/auth/logout' and not body:
                        accounts.logout(self.session_token());self.set_cookie='company_admin=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0';return self.reply(200,{'ok':True})
                except Exception:return self.reply(400,{'error':'账户操作未完成，请检查账户、密码、邀请码或稍后再试。'})
                return self.reply(400,{'error':'INVALID_INPUT'})
            if self.server.accounts and not self.administrator():return self.reply(401,{'error':'LOGIN_REQUIRED'})
            if path=='/api/admin/accounts':
                if not self.server.admin or not self.administrator():return self.reply(403,{'error':'ADMIN_REQUIRED'})
                from .member_admin import handle
                try:return self.reply(200,handle(self.administrator(),body))
                except Exception:return self.reply(409,{'error':'操作未完成。请刷新确认状态；签发失败时可重试启用，勿重复邀请。'})
            if path=='/api/admin/directories':
                if not self.server.admin:return self.reply(403,{'error':'ADMIN_REQUIRED'})
                from .directory_requests import handle
                try:return self.reply(200,handle(self.administrator() or 'local-admin',body))
                except (ValueError,TypeError):return self.reply(409,{'error':'申请状态或范围已变化，请刷新后重试。'})
                except Exception:return self.reply(503,{'error':'资料申请服务暂不可用，请稍后检查。'})
            if path=='/api/admin/files':
                if not self.server.admin:return self.reply(403,{'error':'ADMIN_REQUIRED'})
                from .file_admin import handle
                try:
                    result=handle(body)
                    if body and self.server.accounts:
                        with self.server.accounts.db() as db:self.server.accounts.event(db,self.administrator(),'admin_change',path)
                    return self.reply(200,result)
                except (ValueError,OSError,UnicodeError):return self.reply(400,{'error':'文件范围未保存：请检查路径、文件类型、大小、敏感内容及权限。'})
                except Exception:return self.reply(503,{'error':'文件授权存储暂不可用，请刷新检查操作结果。'})
            if path=='/api/admin/history':
                if not self.server.admin:return self.reply(403,{'error':'ADMIN_REQUIRED'})
                from .history_access import handle
                try:
                    result=handle(self.administrator() or 'local-admin',body)
                    if body and self.server.accounts:
                        with self.server.accounts.db() as db:self.server.accounts.event(db,self.administrator(),'admin_change',path)
                    return self.reply(200,result)
                except (ValueError,TypeError,AttributeError):return self.reply(409,{'error':'历史范围未保存：请检查身份、决定与有效期。'})
                except Exception:return self.reply(503,{'error':'历史范围账本暂不可用，请刷新检查操作结果。'})
            if path=='/api/admin/access':
                if not self.server.admin:return self.reply(403,{'error':'ADMIN_REQUIRED'})
                from .admin_access import handle
                try:
                    result=handle(body)
                    if body and self.server.accounts:
                        with self.server.accounts.db() as db:self.server.accounts.event(db,self.administrator(),'admin_change',path)
                    return self.reply(200,result)
                except (ValueError,TypeError,AttributeError):return self.reply(409,{'error':'审批未生效：申请状态或项目范围已改变，或输入无效。请刷新后检查。'})
                except Exception:return self.reply(503,{'error':'审批账本暂不可用，操作结果不明时请先刷新。'})
            if path=='/api/work' and set(body)=={'target'}:
                from .progress_query import remote_work
                try:return self.reply(200,remote_work(self.server.bridge,body['target']))
                except ValueError:return self.reply(400,{'error':'INVALID_INPUT'})
                except Exception:return self.reply(503,{'error':'PLATFORM_UNAVAILABLE'})
            if path=='/api/progress' and set(body)=={'target','project_id'}:
                from .progress_query import remote_progress
                try:return self.reply(200,remote_progress(self.server.bridge,body['target'],body['project_id']))
                except ValueError:return self.reply(400,{'error':'INVALID_INPUT'})
                except Exception:return self.reply(503,{'error':'PLATFORM_UNAVAILABLE'})
            if path=='/api/agents' and not body:
                action,payload='list_agents',{}
            elif path=='/api/tasks' and set(body)<= {'state','cursor'}:
                action,payload='list_tasks',{'limit':50,**body}
            elif path in {'/api/task','/api/cancel'} and set(body)=={'task_id'}:
                uuid.UUID(body['task_id']);action='get_task' if path=='/api/task' else 'cancel_task';payload=body
            elif path=='/api/project' and set(body)=={'target','project_id'}:
                action='get_project_progress';target=body['target'];payload={'project_id':body['project_id']}
            elif path=='/api/chat/access' and set(body)=={'target'}:
                action='request_access';target=body['target'];payload={}
            elif path=='/api/chat/access-status' and not body:
                action,payload='list_access',{}
            elif path=='/api/chat/send' and set(body)=={'target','question','idempotency_key'}:
                uuid.UUID(body['idempotency_key'])
                if not isinstance(body['question'],str) or not 1<=len(body['question'].strip())<=1200:raise ValueError()
                action='submit_task';target=body['target']
                payload={'operation':'ask_peer','question':body['question'],'idempotency_key':body['idempotency_key'],'ttl_seconds':240}
            else:return self.reply(404,{'error':'UNKNOWN_OPERATION'})
            if target is not None:
                import re
                if not isinstance(target,str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}',target):raise ValueError()
        except (ValueError,TypeError,AttributeError):return self.reply(400,{'error':'INVALID_INPUT'})
        try:
            result=self.server.bridge.call(action,payload,target)
            return self.reply(200,result)
        except (Exception,FutureTimeout):
            return self.reply(503,{'error':'PLATFORM_UNAVAILABLE','message':'平台暂不可达，请检查消息服务。任务提交结果不明时请先刷新任务列表。'})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--identity',default='ag-a');parser.add_argument('--port',type=int,default=8787)
    parser.add_argument('--admin',action='store_true',help='Enable trusted local administrator controls on Router host')
    args=parser.parse_args()
    if not args.admin:raise SystemExit('前端仅提供管理员后台；普通成员请使用 Skill。')
    from .accounts import Accounts
    accounts=Accounts();accounts.setup_code()
    bridge=Bridge(args.identity);server=DashboardServer(('127.0.0.1',args.port),bridge,admin=True,accounts=accounts)
    print(f'Dashboard ready: http://127.0.0.1:{args.port} (identity={args.identity})',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();bridge.close()
if __name__=='__main__':main()
