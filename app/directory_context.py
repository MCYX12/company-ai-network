"""Request-scoped grants supplied only by the authenticated Router dispatch."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
from .directory_requests import PROFILE,validate_root
CURRENT=ContextVar('directory_grants',default=None)
HISTORY=ContextVar('history_grants',default=None)
def authorized(caller,target,root):
 grants=CURRENT.get()
 if grants is None:return None
 found=[g for g in grants if g['caller']==caller and g['target']==target and g['root']==root]
 if not found:return None
 return any(g['state']=='approved' and g['expires']>time.time() for g in found)
def validate_grants(row,payload,grants,history_grants=None):
 """Same validation for every answer path; the grants come only from the dispatch."""
 if not isinstance(grants,list) or len(grants)>100:raise ValueError('Invalid grants')
 for g in grants:
  if set(g)!={'id','caller','target','root','profile','state','expires'} or g['profile']!=PROFILE or g['caller']!=payload.get('caller') or g['target']!=row['agent_id'] or g['state'] not in {'pending','approved','denied','revoked','expired'} or not isinstance(g['expires'],(int,float)):raise ValueError('Invalid grant')
  validate_root(g['root'])
 history=history_grants if history_grants is not None else []
 if not isinstance(history,list) or len(history)>100:raise ValueError('Invalid history grants')
 for g in history:
  if set(g)!={'id','caller','target','state','expires','scope'} or g['caller']!=payload.get('caller') or g['target']!=row['agent_id'] or g['state'] not in {'pending','approved','denied','revoked','expired'} or not isinstance(g['expires'],(int,float)) or not isinstance(g.get('scope','*'),str):raise ValueError('Invalid history grant')
 return grants,history
@contextmanager
def scope(row,payload,grants,history_grants=None):
 """Make one dispatch's grants visible to whatever answers this task.

 The reader path (directory_context.execute) and the one-shot harness path
 (harness_adapter via the task worker) must both see exactly the Router-supplied
 grants; otherwise a harness answer silently has no authorized directories at all.
 """
 grants,history=validate_grants(row,payload,grants,history_grants)
 token=CURRENT.set(grants);history_token=HISTORY.set(history)
 try:yield grants,history
 finally:CURRENT.reset(token);HISTORY.reset(history_token)
def execute(row,payload,deadline,grants,history_grants=None):
 from .peer_question import execute_question
 with scope(row,payload,grants,history_grants):
  code,data=execute_question(row,payload,deadline)
  if isinstance(data,dict):
   used={p['project_path'] for p in data.get('projects',[]) if p.get('progress_report')}
   ids=[g['id'] for g in grants if g['state']=='approved' and g['root'] in used]
   if ids:data['directory_grant_ids']=ids
  return code,data
