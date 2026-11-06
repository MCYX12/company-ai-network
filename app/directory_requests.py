"""Router-owned requests for one fixed read-only project-notes profile."""
import json,os,sqlite3,time,uuid,posixpath,re
from pathlib import PurePosixPath,PureWindowsPath
from .common import authority_base, RUNTIME, approval_expiry
PROFILE='project_notes_v1'
# Every refusal carries a code and a human sentence, so a caller never has to guess
# why an automatic directory request produced no record.
REJECTIONS={
 'BAD_TYPE':'root 必须是字符串路径',
 'NOT_ABSOLUTE':'必须是绝对路径（Unix /... 或 Windows C:\\...）',
 'TOO_LONG':'路径过长（上限 1024 字符）',
 'BAD_CHARS':'路径含反斜杠或控制字符',
 'NOT_NORMALIZED':'路径未规范化（含 // 或 . 段或结尾 /），请给出规范化的绝对路径',
 'PARENT_TRAVERSAL':'路径含 .. 段',
 'TOO_SHORT':'路径层级过浅（少于两级）',
 'TOO_BROAD':'根目录过于宽泛（如 /home/用户名、/Users/用户名 或其上层），请给出具体项目目录',
 'HIDDEN_COMPONENT':'路径包含隐藏目录（以 . 开头的段）',
 'TOO_MANY':'一次最多提交 6 个目录申请',
 'CASE_MISMATCH':'该路径与设备上实际名称只有大小写差异：请由持有设备上的 agent 自行搜索确认后，按候选中的真实路径重新提交',
 'WINDOWS_UNSAFE':'Windows 路径不得使用 UNC/设备前缀、设备名、备用数据流、尾随空格或句点',
 # Reserved, and deliberately *not* emitted: only the device that owns the directory can know a
 # path does not exist (the platform must not invent or probe Mac paths). A wrong path is caught
 # as CASE_MISMATCH here, or refused by the device agent before it is proposed.
 'NOT_FOUND':'该路径在持有设备上不存在：请由 agent 搜索候选后提交真实存在的项目目录',
}
class RootRejected(ValueError):
 def __init__(self,code,root=None,candidates=None):
  self.code=code;self.root=root;self.candidates=list(candidates or [])
  super().__init__(REJECTIONS.get(code,code))
def reason_text(code):
 return REJECTIONS.get(code,code)
def reject_code(error):
 return getattr(error,'code','INVALID_ROOT')
def validate_root(root):
 if not isinstance(root,str):raise RootRejected('BAD_TYPE',root)
 if len(root)>1024:raise RootRejected('TOO_LONG',root)
 if root.startswith(('\\\\','//')):raise RootRejected('WINDOWS_UNSAFE',root)
 if re.match(r'^[A-Za-z]:',root):
  if any(ord(c)<32 for c in root):raise RootRejected('BAD_CHARS',root)
  p=PureWindowsPath(root)
  if not p.is_absolute():raise RootRejected('NOT_ABSOLUTE',root)
  if '..' in p.parts:raise RootRejected('PARENT_TRAVERSAL',root)
  if str(p)!=root:raise RootRejected('NOT_NORMALIZED',root)
  parts=p.parts[1:]
  if any(':' in part or any(c in part for c in '<>"|?*') or part.endswith((' ','.'))
         or re.fullmatch(r'(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?',part,re.I)
         for part in parts):raise RootRejected('WINDOWS_UNSAFE',root)
  if len(parts)<2:raise RootRejected('TOO_SHORT',root)
  if parts[0].casefold() in {'windows','program files','program files (x86)'} or (len(parts)==2 and parts[0].casefold()=='users'):
   raise RootRejected('TOO_BROAD',root)
  if any(part.startswith('.') for part in parts):raise RootRejected('HIDDEN_COMPONENT',root)
  return root
 if not root.startswith('/'):raise RootRejected('NOT_ABSOLUTE',root)
 if '\\' in root or any(ord(c)<32 for c in root):raise RootRejected('BAD_CHARS',root)
 p=PurePosixPath(root)
 if str(p)!=root:raise RootRejected('NOT_NORMALIZED',root)
 if '..' in p.parts:raise RootRejected('PARENT_TRAVERSAL',root)
 if len(p.parts)<3:raise RootRejected('TOO_SHORT',root)
 if len(p.parts)==3 and p.parts[1] in {'Users','home'}:raise RootRejected('TOO_BROAD',root)
 if any(x.startswith('.') for x in p.parts[1:]):raise RootRejected('HIDDEN_COMPONENT',root)
 return root
def canonical_root(root):
 """Match each path component against this host's filesystem (best effort).

 Only a hint: a path this host cannot resolve -- for example a Mac path reported to
 the Linux peer -- is returned unchanged instead of being invented. On a
 case-insensitive filesystem this also returns the directory's real spelling, which
 is what keeps "robotac" and "ROBOTAC" from becoming two different grants.
 """
 # A Linux Router must never interpret a Windows path as a local relative name.
 if isinstance(root,str) and re.match(r'^[A-Za-z]:',root):return root,False
 try:parts=PurePosixPath(root).parts
 except (TypeError,ValueError):return root,False
 current='/';changed=False
 for part in parts[1:]:
  try:names=os.listdir(current)
  except OSError:return root,False
  if part in names:current=os.path.join(current,part);continue
  folded=part.casefold();matches=[n for n in names if n.casefold()==folded]
  if len(matches)!=1:return root,False
  current=os.path.join(current,matches[0]);changed=True
 return current,changed
def case_candidates(root):
 """Real sibling names that differ from `root` only by case, from this host's view.

 Discovery belongs to the agent that lives on the device: the platform only reports
 what it can see so the agent (or the user) can resubmit the exact path. An empty list
 means "nothing local to point at" -- typically a path on another device -- so the
 request is accepted as submitted instead of being rewritten here.
 """
 if isinstance(root,str) and re.match(r'^[A-Za-z]:',root):return []
 try:parts=PurePosixPath(root).parts
 except (TypeError,ValueError):return []
 current='/'
 for part in parts[1:]:
  try:names=os.listdir(current)
  except OSError:return []
  if part in names:current=os.path.join(current,part);continue
  folded=part.casefold();matches=sorted(n for n in names if n.casefold()==folded)
  if not matches:return []
  return [os.path.join(current,match) for match in matches]
 return []
class DirectoryRequests:
 def __init__(self,path=None):
  self.path=path or authority_base(RUNTIME)/'directory-requests.sqlite3'
  with self.db() as db:
   db.execute('CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,caller TEXT,target TEXT,root TEXT,profile TEXT,state TEXT,created REAL,expires REAL,actor TEXT,reason TEXT)')
   db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,request_id TEXT,event TEXT,time REAL,actor TEXT,reason TEXT)')
 def db(self):return sqlite3.connect(self.path,timeout=5)
 def _others(self,db,target,root,caller):
  rows=db.execute('SELECT DISTINCT caller FROM requests WHERE target=? AND root=? AND state=? AND expires>? AND caller!=?',
                  (target,root,'approved',time.time(),caller)).fetchall()
  return sorted({r[0] for r in rows})
 def request(self,caller,target,root,allow_canonical=None):
  validate_root(root)
  # Default: the agent that owns the directory searches and submits the real path; the
  # platform refuses a case-only variant with candidates instead of silently rewriting
  # it. DIRECTORY_AUTOCANON=1 restores the previous server-side renaming.
  if allow_canonical is None:allow_canonical=os.environ.get('DIRECTORY_AUTOCANON')=='1'
  if allow_canonical:
   canonical,renamed=canonical_root(root)
   if renamed:validate_root(canonical)
  else:
   canonical,renamed=root,False
   candidates=case_candidates(root)
   if candidates:raise RootRejected('CASE_MISMATCH',root,candidates)
  requested=root if renamed else None
  with self.db() as db:
   db.row_factory=sqlite3.Row;db.execute('BEGIN IMMEDIATE')
   others=self._others(db,target,canonical,caller)
   old=db.execute('SELECT * FROM requests WHERE caller=? AND target=? AND root=? ORDER BY created DESC LIMIT 1',(caller,target,canonical)).fetchone()
   if old and (old['state'] in {'pending','denied','revoked'} or old['expires']>time.time()):
    return dict(old)|{'canonicalized':renamed,'requested_root':requested,'also_approved_for':others}
   # Bound automatic requests from a faulty or compromised peer.
   if db.execute("SELECT COUNT(*) FROM requests WHERE caller=? AND target=? AND state='pending'",(caller,target)).fetchone()[0]>=30:raise ValueError('Pending request limit')
   ident=str(uuid.uuid4());now=time.time()
   note='发现待授权项目目录'+(f'（原请求 {requested}，已按实际名称规范化为 {canonical}）' if renamed else '')
   db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',(ident,caller,target,canonical,PROFILE,'pending',now,0,'',note))
   db.execute('INSERT INTO events(request_id,event,time,actor,reason) VALUES(?,?,?,?,?)',(ident,'requested',now,target,'authenticated_peer_discovery'))
   return dict(db.execute('SELECT * FROM requests WHERE id=?',(ident,)).fetchone())|{'canonicalized':renamed,'requested_root':requested,'also_approved_for':others}
 def listing(self,caller=None,target=None):
  with self.db() as db:
   db.row_factory=sqlite3.Row
   rows=db.execute('SELECT * FROM requests'+(' WHERE caller=? AND target=?' if caller is not None else '')+' ORDER BY created DESC LIMIT 100',(caller,target) if caller is not None else ()).fetchall()
  return [dict(r,state='expired' if r['state']=='approved' and r['expires']<=time.time() else r['state']) for r in rows]
 def for_caller(self,caller):
  """One caller's own rows, across targets: safe for the caller to poll itself."""
  with self.db() as db:
   db.row_factory=sqlite3.Row
   rows=db.execute('SELECT * FROM requests WHERE caller=? ORDER BY created DESC LIMIT 100',(caller,)).fetchall()
  return [{k:r[k] for k in ('id','target','root','profile','state','expires','created')} | {'state':'expired' if r['state']=='approved' and r['expires']<=time.time() else r['state']} for r in rows]
 def decide(self,ident,decision,seconds,reason,actor):
  if decision not in {'approved','denied','revoked','pending'} or type(seconds)!=int or not (seconds==0 or 1<=seconds<=86400) or not isinstance(reason,str) or not 1<=len(reason.strip())<=500:raise ValueError('Invalid decision')
  with self.db() as db:
   db.execute('BEGIN IMMEDIATE');old=db.execute('SELECT state,expires FROM requests WHERE id=?',(ident,)).fetchone()
   if not old or (decision in {'approved','denied'} and old[0]!='pending') or (decision=='revoked' and old[0]!='approved'):raise ValueError('State changed')
   if decision=='pending' and not (old[0] in {'denied','revoked'} or (old[0]=='approved' and old[1]<=time.time())):raise ValueError('Cannot reopen active request')
   now=time.time();db.execute('UPDATE requests SET state=?,expires=?,actor=?,reason=? WHERE id=?',(decision,approval_expiry(seconds,now) if decision=='approved' else 0,actor,reason.strip(),ident))
   db.execute('INSERT INTO events(request_id,event,time,actor,reason) VALUES(?,?,?,?,?)',(ident,decision,now,actor,reason.strip()))
 def permits_ids(self,caller,target,ids):
  if not isinstance(ids,list) or len(ids)>100 or not all(isinstance(x,str) for x in ids):return False
  approved={r['id'] for r in self.listing(caller,target) if r['state']=='approved' and r['expires']>time.time()}
  return set(ids).issubset(approved)
 def dispatch(self,caller,target,deadline):
  return [{k:r[k] for k in ['id','caller','target','root','profile','state']}|{'expires':min(r['expires'],deadline)} for r in self.listing(caller,target) if r['state'] in {'pending','approved','denied','revoked','expired'}]

def handle(actor,body):
 store=DirectoryRequests()
 if not body:
  from .registry import load_registry
  registry=load_registry();rows=store.listing()
  for r in rows:
   r['device']=registry.get(r['target'],{}).get('device_id','未登记')
   r['employee']=registry.get(r['caller'],{}).get('employee_id',r['caller'])
  return {'requests':rows,'profile':PROFILE}
 if set(body)!={'id','decision','seconds','reason'}:raise ValueError('Invalid fields')
 if body['decision']=='approved':
  from .registry import load_registry,visible
  row=next((r for r in store.listing() if r['id']==body['id']),None)
  if not row or not visible(row['caller'],row['target'],load_registry()):raise ValueError('Registration changed')
 store.decide(body['id'],body['decision'],body['seconds'],body['reason'],actor)
 return {'updated':body['id']}
