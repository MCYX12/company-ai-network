"""Read-only owner diagnostics; no model generation or transcript reads."""
import hashlib,json,sys
from pathlib import Path
import worker
from app.client_history import consent_status,native_status
from app.client_material import read_bytes,safe_name
from upgrade import managed

def integrity(root):
    try:
        raw,_=read_bytes(root,'installed-manifest.json',65536);inventory=json.loads(raw)
        if (not isinstance(inventory,dict) or set(inventory)!={'schema_version','package_sha256','files'}
            or type(inventory['schema_version']) is not int or inventory['schema_version']!=1
            or not isinstance(inventory['files'],dict) or not 1<=len(inventory['files'])<=200):raise ValueError('Invalid installation inventory')
        changed=[]
        for name,digest in inventory['files'].items():
            safe_name(name)
            if not managed(name) or not isinstance(digest,str) or len(digest)!=64:raise ValueError('Invalid inventory entry')
            try:content,_=read_bytes(root,name,1024*1024)
            except (OSError,ValueError):changed.append(name);continue
            if hashlib.sha256(content).hexdigest()!=digest:changed.append(name)
        return {'stage':'installed_files','status':'error' if changed else 'ok','message':'Installed files differ from verified package' if changed else 'Verified managed files match package','changed':changed,'package_sha256':inventory['package_sha256']}
    except FileNotFoundError:return {'stage':'installed_files','status':'warning','message':'Installation inventory missing; run verified upgrade'}
    except (OSError,ValueError,TypeError,KeyError):return {'stage':'installed_files','status':'error','message':'Installation inventory unreadable or invalid'}

def diagnose(root):
    config=worker.settings(root);status,hint=worker.preflight(config)
    checks=[integrity(root),{'stage':'fixed_command','status':'ok' if status=='ready' else 'error','message':hint or 'Fixed command resolves'}]
    mode=config.get('notes_mode','safe')
    checks.append({'stage':'material_mode','status':'ok' if mode in {'raw','local','safe','off'} else 'error','message':mode})
    if mode=='local':
        try:
            from ollama_adapter import request_json
            model=config.get('local_summary_model');rows=request_json('/api/tags',timeout=5).get('models',[])
            if not model or not any(model in (r.get('name'),r.get('model')) for r in rows):raise ValueError('Model missing')
            details=request_json('/api/show',{'model':model},timeout=5)
            if details.get('remote_host') or details.get('remote_model') or 'completion' not in details.get('capabilities',[]):raise ValueError('Remote or unsupported model')
            checks.append({'stage':'local_summary_model','status':'ok','message':'Installed local completion model; generation not tested'})
        except (OSError,ValueError,TypeError):checks.append({'stage':'local_summary_model','status':'error','message':'Loopback completion model unavailable; no raw fallback'})
    consent=consent_status(root/'.runtime')
    opted_in=config.get('history_exports_enabled') is True
    checks.append({'stage':'history_exports','status':'error' if opted_in and not consent['enabled'] else 'ok',
        'message':('Enabled with owner consent' if consent['enabled'] else 'Enabled but owner policy missing, expired or invalid; requests are denied') if opted_in else 'Disabled; desktop chats are not scanned'})
    from app.native_work import desktop_status
    native=desktop_status(config,root)
    opted_in=isinstance(config.get('native_work'),dict) and config['native_work'].get('enabled') is True
    partial=[v for name in ('background_jobs','scheduled_tasks') if isinstance((v:=native.get(name)),dict) and v.get('status')=='unknown']
    checks.append({'stage':'native_work','status':'warning' if opted_in and (native['status']=='unknown' or partial) else 'ok',
        'message':native['status']+('; partial interfaces unavailable' if partial else ''),
        'hint':native.get('hint') or (partial[0].get('hint') if partial else None)})
    return {'code':'DIAGNOSTIC','checks':checks,'install_digest':worker.INSTALL_DIGEST}
if __name__=='__main__':
    r=diagnose(Path(__file__).resolve().parent);print(json.dumps(r,ensure_ascii=False));sys.exit(1 if any(c['status']=='error' for c in r['checks']) else 2 if any(c['status']=='warning' for c in r['checks']) else 0)
