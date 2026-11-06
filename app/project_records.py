"""Explicitly published structured records only; no arbitrary paths or model calls."""
import json
import re
import stat
from .client_material import read_bytes
from datetime import datetime, timezone
from .common import RUNTIME
from . import project_state

FIELDS={'schema_version','project_id','source_id','updated_at','summary','completed','blockers','evidence'}

def validate(record, project=None):
    """One implementation of the published-record contract, shared by the reader and the publisher."""
    if not isinstance(record,dict) or set(record)!=FIELDS or record['schema_version']!=1:
        raise ValueError('schema')
    if project is not None and record['project_id']!=project:
        raise ValueError('schema')
    for name in ['source_id','updated_at','summary']:
        if not isinstance(record[name],str) or not 1<=len(record[name])<=2000:raise ValueError('text')
    for name in ['completed','blockers','evidence']:
        if not isinstance(record[name],list) or len(record[name])>30 or not all(isinstance(x,str) and 1<=len(x)<=1000 for x in record[name]):raise ValueError('list')
    updated=datetime.fromisoformat(record['updated_at'])
    if updated.tzinfo is None:raise ValueError('timezone')
    if (datetime.now(timezone.utc)-updated).total_seconds() < -300:raise ValueError('future timestamp')
    if len(json.dumps(record,ensure_ascii=False).encode())>16384:raise ValueError('oversize')
    return record


def read_progress(row, payload, runtime=None):
    project=payload.get('project_id')
    if not isinstance(project,str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}',project) or project not in row['projects']:
        return 'FORBIDDEN',None
    base=runtime or RUNTIME
    from .project_retirement import retired
    try:
        if retired(base,project):
            return 'DATA_UNAVAILABLE',{'project_id':project,'summary':'该项目已退役。'}
    except (OSError,ValueError,TypeError):
        return 'DATA_INVALID',None
    folder=base/'project-records'
    path=folder/(project+'.json')
    if folder.is_symlink() or path.is_symlink():
        return 'DATA_INVALID',None
    try:
        # Windows junctions/reparse points and hard-linked records must not turn
        # an explicitly published record into a read of another local file.
        for part in [folder, *folder.parents]:
            info=part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                raise ValueError('linked parent')
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('linked record')
        with path.open('rb') as stream:raw=stream.read(16385)
        if len(raw)>16384:raise ValueError('oversize')
        record=json.loads(raw)
        validate(record,project)
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(record['updated_at'])).total_seconds()
    except FileNotFoundError:
        return 'DATA_UNAVAILABLE',{'project_id':project,'summary':'未知：尚未发布项目资料。'}
    except (OSError,ValueError,TypeError,KeyError,UnicodeError):
        return 'DATA_INVALID',None
    now=datetime.now(timezone.utc).timestamp()
    shot=project_state.snapshot(record,now=now)
    try:
        raw,_=read_bytes(folder,project+'.meta.json',8192);meta=json.loads(raw)
        if not isinstance(meta,dict):raise ValueError('sidecar schema')
        for field in ('observed_at','confirmed_at','published_at'):
            if meta.get(field):
                clock=datetime.fromisoformat(meta[field])
                if clock.tzinfo is None or clock.timestamp()>now+300:raise ValueError('sidecar clock')
        metadata_state='available'
    except FileNotFoundError:meta={};metadata_state='missing'
    except (OSError,ValueError,TypeError,KeyError):meta={};metadata_state='invalid'
    narrative_age,narrative_clock=project_state.confirmation_clock(record,meta,now)
    narrative=project_state.snapshot({'updated_at':meta.get('confirmed_at') or record['updated_at']},now=now)
    return 'OK',{'synthetic':False,'agent_id':row['agent_id'],'source':'published_project_record',
        **record,'progress':None,'freshness':shot['freshness'],'snapshot_at':shot['snapshot_at'],
        'age_seconds':shot['age_seconds'],'observed_at':meta.get('observed_at'),'confirmed_at':meta.get('confirmed_at'),
        'published_at':meta.get('published_at'),'metadata_state':metadata_state,'narrative_clock':narrative_clock,
        'narrative_age_seconds':narrative_age,'narrative_freshness':narrative['freshness'],'freshness_note':shot['note'],'state':shot['state'],
        'stale_after_seconds':shot['stale_after_seconds'],'queried_at':datetime.now(timezone.utc).isoformat()}
