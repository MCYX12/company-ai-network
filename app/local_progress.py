"""Per-project, per-principal local summaries; source access is checked independently."""
import json,time,hashlib,os,fcntl,re,subprocess
from pathlib import Path
from .common import RUNTIME
from . import doc_index

FIELDS=['project','generated_at','local_model','raw_uploaded','facts','git','evidence','limitations']
# The versioned project_notes_v1 document list. Only these documents are read, and the
# same list is used by the local summarizer (scripts/summarize_local_project.py), so the
# fast prompt path and the summary path can never disagree about what "authorized" means.
# Never scripts, code, model files or hidden files.
NOTES_DOCS=('README.md','README.txt','TODO.md','TODO.txt','PROGRESS.md','STATUS.md','CHANGELOG.md')
PROFILE_NAME='project_notes_v1'
# A directory approval must not silently widen the read set. A device owner may therefore
# extend the profile with an explicit, device-local document list (POC_NOTES_EXTRA_DOCS,
# comma/semicolon/newline separated). It is opt-in and bounded: at most 40 entries, three
# path levels, document-like suffixes only; no absolute paths, no "..", no hidden segments.
EXTRA_DOCS_ENV='POC_NOTES_EXTRA_DOCS'
EXTRA_DOCS_MAX=40
DOC_SUFFIXES=('.md','.markdown','.txt','.rst','.text')
def _clean_doc(name):
 if not isinstance(name,str):return None
 name=name.strip()
 if not name or name.startswith('/') or '\\' in name or len(name)>200:return None
 if not name.lower().endswith(DOC_SUFFIXES):return None
 parts=name.split('/')
 if len(parts)>3 or any(p in ('','.','..') or p.startswith('.') for p in parts):return None
 return name
def extra_docs():
 raw=os.environ.get(EXTRA_DOCS_ENV,'')
 names=[]
 for token in re.split(r'[,;\n]+',raw):
  clean=_clean_doc(token)
  if clean and clean not in names:names.append(clean)
 return tuple(names[:EXTRA_DOCS_MAX])
def notes_docs():
 """The effective document allowlist: the versioned profile plus owner-declared extras."""
 return tuple(NOTES_DOCS)+tuple(d for d in extra_docs() if d not in NOTES_DOCS)
def profile_digest():
 return hashlib.sha256(('|'.join(notes_docs())).encode()).hexdigest()[:12]
# safe (default): raw note text never reaches a networked model -- only facts a local
# summarizer produced, plus a metadata inventory and read-only git counts.
# raw: the device owner opts in to embedding the approved documents themselves.
NOTES_MODE=os.environ.get('POC_NOTES_MODE','safe')
def safe_json(p):
 if p.is_symlink() or p.stat().st_mode & 0o022 or p.stat().st_size>65536:raise ValueError('Unsafe report or grant')
 return json.loads(p.read_text())
def notes_allowed(caller,target,project):
 from .directory_context import authorized
 central=authorized(caller,target,project)
 if central is not None:return central
 p=RUNTIME/'project-notes-access.json'
 if not p.exists():return False
 return any(g.get('caller')==caller and g.get('target')==target and g.get('root')==project and g.get('expires',0)>time.time() and g.get('enabled') is True for g in safe_json(p).get('grants',[]))
def cache_path(caller,target,project):
 # The profile digest is part of the key: extending or narrowing the document allowlist
 # must not serve a summary produced under a different read set.
 key=hashlib.sha256(json.dumps([PROFILE_NAME,profile_digest(),caller,target,project]).encode()).hexdigest()
 return RUNTIME/'progress-reports'/(key+'.json')
def version(row):
 return hashlib.sha256(json.dumps(['v23-semantic-2',row['project_path'],row.get('last_activity'),
                                   row.get('text_events'),row.get('tool_requests'),profile_digest()]).encode()).hexdigest()
def report_for(caller,target,project,source_version=None):
 p=cache_path(caller,target,project)
 if not p.exists():
  p=RUNTIME/'local-progress-report.json' # bounded migration of the previous owner-prepared report
  if not p.exists():return None
 r=safe_json(p)
 if r.get('caller')!=caller or r.get('target')!=target or r.get('project_path')!=project:return None
 if r['expires']<=time.time():return None
 if source_version is not None and r.get('source_version')!=source_version:return None
 if r.get('notes_allowed',True) and not notes_allowed(caller,target,project):return None
 return {k:r[k] for k in FIELDS}
def ensure_report(caller,target,row,deadline):
 from .claude_history import settings,HistoryAccessError
 try:config=settings(caller,target)
 except HistoryAccessError:config=None
 if (not config and not notes_allowed(caller,target,row['project_path'])) or row.get('kind')!='project_activity':return None
 project=row['project_path'];source_version=version(row)+('-notes' if notes_allowed(caller,target,project) else '-history')
 found=report_for(caller,target,project,source_version)
 if found:return found
 path=cache_path(caller,target,project);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
 if path.parent.is_symlink():raise ValueError('Unsafe cache directory')
 # Only one local model run for concurrent questions about the same project.
 fd=os.open(path.with_suffix('.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'w') as lock:
  while True:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
   except BlockingIOError:
    if time.time()>deadline-5:raise TimeoutError('Local summary busy')
    time.sleep(.1)
  found=report_for(caller,target,project,source_version)
  if found:return found
  if config and not settings(caller,target):raise ValueError('Permission removed')
  from scripts.summarize_local_project import generate
  report=generate(caller,target,project,deadline,source_version)
  if config and not settings(caller,target):raise ValueError('Permission removed')
  if report['notes_allowed'] and not notes_allowed(caller,target,project):raise ValueError('Notes permission revoked')
  temporary=path.with_suffix('.tmp')
  fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'w') as f:json.dump(report,f,ensure_ascii=False)
  temporary.replace(path)
  return {k:report[k] for k in FIELDS}

def _doc_inventory(root,names=None):
 items=[]
 for name in (notes_docs() if names is None else names):
  p=Path(root)/name
  try:
   if p.is_symlink() or not p.is_file():continue
   info=p.stat()
  except OSError:continue
  items.append({'name':name,'bytes':info.st_size,'mtime':int(info.st_mtime)})
 return items

def _git_state(root):
 """Read-only git status counts; never run tests/hooks and never write."""
 try:
  p=subprocess.run(['/usr/bin/git','-c','core.fsmonitor=false','-c','core.untrackedCache=false',
                    '-C',str(root),'status','--porcelain=v1','-z','--untracked-files=normal'],
                   env={**os.environ,'GIT_OPTIONAL_LOCKS':'0'},capture_output=True,timeout=15)
 except (OSError,subprocess.SubprocessError):return None
 if p.returncode!=0:return None
 records=p.stdout.split(b'\0')
 return {'available':True,'changed_entries':sum(bool(x) for x in records),
         'untracked_entries':sum(x.startswith(b'??') for x in records)}

# Bounded coverage metadata: aggregated counts stay exact, the per-item lists are samples
# so a granted root with hundreds of chunks cannot bloat the answer payload.
COVERAGE_SAMPLE=40
def _fill_coverage(coverage,root,allowed,docs,mode,digested=None,omitted=None,skipped=None,truncated=False):
 if coverage is None:return
 digested=digested or [];omitted=omitted or [];skipped=skipped or []
 coverage.update({'root':root,'profile':PROFILE_NAME,'mode':mode,
                  'allowed':len(allowed),'present':[d['name'] for d in docs[:COVERAGE_SAMPLE]],
                  'present_total':len(docs),
                  'digested':digested[:COVERAGE_SAMPLE],'digested_total':len(digested),
                  'omitted':omitted[:COVERAGE_SAMPLE],'omitted_total':len(omitted),
                  'skipped':skipped[:COVERAGE_SAMPLE],'skipped_total':len(skipped),
                  'truncated':bool(truncated)})

def _document_chunks(root,names):
 """Chunk the present allowed documents; never read outside the profile allowlist."""
 chunks=[];skipped=[]
 try:
  from .claude_history import opened
 except ImportError:
  opened=None
 for name in names:
  p=Path(root)/name
  try:
   if p.is_symlink() or not p.is_file():
    skipped.append({'name':name,'reason':'MISSING'});continue
   # Defense in depth: even with a validated name, refuse a resolved path that escaped
   # the approved root (for example through a symlinked parent directory).
   root_resolved=Path(root).resolve()
   if p.resolve()!=root_resolved and not str(p.resolve()).startswith(str(root_resolved)+os.sep):
    skipped.append({'name':name,'reason':'OUTSIDE_ROOT'});continue
   size=p.stat().st_size
   if size>doc_index.MAX_DOC_BYTES:
    skipped.append({'name':name,'reason':'EXCEEDS_POLICY','bytes':size});continue
  except OSError:
   skipped.append({'name':name,'reason':'UNREADABLE'});continue
  try:
   if opened is not None:
    with opened(Path(root),p)[0] as stream:
     text=stream.read(doc_index.MAX_DOC_BYTES+1).decode('utf-8','replace')
   else:
    text=p.read_text(encoding='utf-8',errors='replace')[:doc_index.MAX_DOC_BYTES+1]
  except (OSError,UnicodeError):
   skipped.append({'name':name,'reason':'UNREADABLE'});continue
  doc_chunks,dropped=doc_index.split_chunks_with_drops(text)
  if dropped:
   # A capped document must not look complete: the unprocessed chunks are reported.
   skipped.append({'name':name,'reason':'CHUNK_LIMIT','dropped':dropped})
  for ch in doc_chunks:
   chunks.append({'file':name,'heading':ch['heading'],'start_line':ch['start_line'],
                  'end_line':ch['end_line'],'chars':len(ch['text']),
                  'sha256':doc_index.chunk_sha(ch['text']),'text':ch['text']})
 return chunks,skipped

def notes_digest(caller,target,root,budget=900,question=None,coverage=None):
 """Bounded prompt block for one granted directory, or None when not allowed.

 `safe` (default) puts only local-summarizer facts and a metadata inventory plus read-only
 git counts into the prompt: the approved 说明/待办原文 never reaches a networked model.
 `raw` is the device owner's explicit opt-in (POC_NOTES_MODE=raw); it embeds question-ranked
 chunks with file/section provenance instead of the first few lines of each file. When
 `coverage` is supplied it is filled with what was digested, omitted and why.
 """
 if NOTES_MODE=='off' or not notes_allowed(caller,target,root):return None
 budget=max(0,int(budget))
 if budget<120:return None
 lines=[f'已授权目录（{PROFILE_NAME}）：{root}']
 used=len(lines[0])
 allowed=notes_docs()
 docs=_doc_inventory(root,allowed)
 report=None
 collected_git=None
 try:report=report_for(caller,target,root)
 except (OSError,ValueError,KeyError):report=None
 if report:
  line=f"- 本地摘要（{report.get('local_model')}，生成于 {time.strftime('%Y-%m-%d %H:%M',time.localtime(report.get('generated_at') or 0))}）："
  lines.append(line);used+=len(line)
  for fact in (report.get('facts') or [])[:6]:
   text=' '.join(str(fact.get('summary','')).split())
   if not text:continue
   line=f'  · [{fact.get("basis")}] {text}'
   if used+len(line)>budget:lines.append('  · （事实已截断）');used+=len(lines[-1]);break
   lines.append(line);used+=len(line)
  # Show which material the summary actually came from; a cached report must not read as
  # "the whole approved directory was read".
  doc_evidence=[e for e in (report.get('evidence') or []) if isinstance(e,dict) and e.get('kind')=='current_project_document']
  line=f'- 摘要资料覆盖：{PROFILE_NAME} 允许 {len(allowed)} 个，本机摘要引用当前项目文档 {len(doc_evidence)} 个'
  lines.append(line);used+=len(line)
  _fill_coverage(coverage,root,allowed,docs,'report',digested=doc_evidence,
                 truncated=bool(report.get('truncated') or report.get('omitted_chunks')))
  git=report.get('git') or {}
  if git.get('available'):
   line=f"- 只读 git 状态：changed={git.get('changed_entries')} untracked={git.get('untracked_entries')}（工作区干净不代表项目完成）"
   lines.append(line);used+=len(line)
  for limitation in (report.get('limitations') or [])[:3]:
   line=f'- 限制：{limitation}'
   if used+len(line)>budget:break
   lines.append(line);used+=len(line)
 else:
  extension='（已由设备所有者扩展）' if extra_docs() else ''
  # Read git once either way; in raw mode it is only printed when the content fits.
  git=_git_state(root)
  collected_git=git
  if NOTES_MODE=='raw':
   # A compact header only: the per-chunk lines below need the digest budget, and the
   # coverage metadata returned to the caller carries the full present/omitted detail.
   line=(f'- 说明/待办资料（{PROFILE_NAME}{extension}：允许 {len(allowed)} 个，'
         f'本目录命中 {len(docs)} 个，按问题检索正文）：')
   lines.append(line);used+=len(line)
  else:
   if docs:
    listed=', '.join(f'{d["name"]}({d["bytes"]}B, {time.strftime("%m-%d %H:%M",time.localtime(d["mtime"]))})' for d in docs[:6])
    line=(f'- 说明/待办文档清单（{PROFILE_NAME}{extension}：允许 {len(allowed)} 个，本目录命中 {len(docs)} 个）'
          + '（原文未发送给模型）：' + listed)
   else:
    line=(f'- 说明/待办文档清单：本目录未命中 {PROFILE_NAME}{extension} 允许的 {len(allowed)} 个文档；'
          '缺少正文时不据此编造进度')
   lines.append(line);used+=len(line)
   if git:
    line=f"- 只读 git 状态：changed={git['changed_entries']} untracked={git['untracked_entries']}（工作区干净不代表项目完成）"
    lines.append(line);used+=len(line)
   line='- 内容级结论需本机本地摘要器（127.0.0.1:11434）产生的 facts；本机未生成时只能给出上述清单与 git 状态，不要据此编造进度。'
   lines.append(line);used+=len(line)
  _fill_coverage(coverage,root,allowed,docs,NOTES_MODE,truncated=len(docs)>6)
 if NOTES_MODE=='raw':
  # Question-ranked chunks instead of the first lines of each file: a fact in the middle or
  # end of a large document can be retrieved, and the caller sees which sections were used,
  # which were dropped for budget, and why files were skipped.
  chunks,skipped=_document_chunks(root,[d['name'] for d in docs])
  ranked=doc_index.rank_chunks(chunks,question)
  digested=[];omitted=[]
  for idx,ch in enumerate(ranked):
   text=' '.join(ch['text'].split())
   prefix=f"- [{ch['file']}:{ch['start_line']}-{ch['end_line']} {ch['heading']}] "
   room=max(0,budget-used-len(prefix)-8)
   if room<60:
    for later in ranked[idx:]:
     omitted.append({'file':later['file'],'start_line':later['start_line'],'end_line':later['end_line'],
                     'heading':later['heading'],'sha256':later['sha256'],'reason':'BUDGET'})
    break
   snippet=text[:room]
   line=prefix+snippet+('…' if len(text)>room else '')
   lines.append(line);used+=len(line)
   digested.append({'file':ch['file'],'start_line':ch['start_line'],'end_line':ch['end_line'],
                    'heading':ch['heading'],'sha256':ch['sha256'],'chars':len(text),'embedded_chars':len(snippet)})
  if not ranked:
   line='- 说明/待办 原文：未读取到可嵌入内容（文件不存在、超过大小上限或不可读）'
   lines.append(line);used+=len(line)
  content_truncated=any(d['chars']>d['embedded_chars'] for d in digested)
  _fill_coverage(coverage,root,allowed,docs,'raw',digested,omitted,skipped,
                 bool(omitted) or content_truncated or bool(skipped))
 text='\n'.join(lines)
 if len(text)>budget:  # hard backstop: the caller's budget is a bound, not a hint
  text=text[:max(0,budget-8)]+'…（已截断）'
 return text

def known_projects(caller,target):
 """Metadata only from earlier device-owned reports; never return their expired facts."""
 paths=list((RUNTIME/'progress-reports').glob('*.json'))[:100]
 legacy=RUNTIME/'local-progress-report.json'
 if legacy.exists():paths.append(legacy)
 from .directory_context import CURRENT
 roots={g['root'] for g in (CURRENT.get() or []) if g['caller']==caller and g['target']==target}
 for p in paths:
  try:
   r=safe_json(p)
   if r.get('caller')==caller and r.get('target')==target:roots.add(r['project_path'])
  except (OSError,ValueError,KeyError):continue
 return [dict(name=Path(root).name,project_path=root,kind='project_activity',sessions=0,last_activity='',text_events=0,read_requests=0,edit_requests=0,tool_requests=0,topics=[],progress_signals=[],score=0,catalog_only=True) for root in sorted(roots)]
