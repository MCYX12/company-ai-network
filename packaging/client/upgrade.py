"""Verify or upgrade an installed client, preserving identity/configuration/runtime.

Default is read-only. --apply requires --worker-stopped; stop the worker task first.
Every replaced file has a timestamped backup; failures restore replaced contents.
"""
import argparse,hashlib,json,shutil,sqlite3,stat,time,zipfile,io
from contextlib import closing
from pathlib import Path
from app.client_material import independent,safe_name,read_bytes
import package_trust

TOP={'worker.py','ollama_adapter.py','dsh_desktop_adapter.py','dsh_desktop_profile.cjs','dsh_desktop_status.cjs','history_export.py','upgrade.py','worker_doctor.py','THIRD_PARTY_LICENSE.txt','selfcheck.ps1','install_windows.ps1','install.py','README.txt','SKILL.md','package_trust.py','configure_windows_worker.ps1','run-worker.py','WorkerService.cs','configure_windows_service.ps1','install_windows_msi.ps1','windows_recovery_check.ps1'}
def managed(name):return name in TOP or name.startswith(('app/','nats/')) and name.endswith('.py')
def upgrade(package,destination,apply=False,worker_stopped=False,signature=None,trusted_key=None):
    package=Path(package).absolute();destination=Path(destination).absolute()
    independent(destination,True)
    # Read once: verification and ZIP extraction must see exactly the same bytes.
    package_data,_=read_bytes(package.parent,package.name,8*1024*1024)
    publisher={'publisher_verified':False}
    if signature is not None and trusted_key is not None:
        independent(Path(trusted_key).absolute());independent(Path(signature).absolute())
        publisher=package_trust.verify(package,signature,trusted_key,package_data)
    elif signature is not None or trusted_key is not None or apply:
        raise ValueError('Applying an upgrade requires a signature and independently provisioned publisher key')
    with zipfile.ZipFile(io.BytesIO(package_data)) as z:
        infos=z.infolist()
        if len(infos)>200 or sum(i.file_size for i in infos)>8*1024*1024:raise ValueError('Package limit')
        names=[i.filename for i in infos]
        if len(set(names))!=len(names) or any(not n.startswith('company-agent-client/') for n in names):raise ValueError('Package layout')
        manifest=json.loads(z.read('company-agent-client/manifest.json'))
        if not isinstance(manifest,dict) or not manifest:raise ValueError('Missing manifest')
        contents={}
        for name,digest in manifest.items():
            safe_name(name)
            key='company-agent-client/'+name
            info=z.getinfo(key)
            if stat.S_ISLNK(info.external_attr>>16) or info.file_size>1024*1024:raise ValueError('Unsafe package member')
            raw=z.read(key)
            if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Manifest mismatch')
            if managed(name):
                path=destination/name
                if path.exists():independent(path)
                else:
                    for parent in path.parents:
                        if parent.exists():independent(parent,True)
                contents[name]=raw
        if set(names)!={'company-agent-client/manifest.json'}|{'company-agent-client/'+n for n in manifest}:raise ValueError('Unmanifested package member')
    # A transaction includes its verification inventory, while local settings are preserved.
    inventory=destination/'installed-manifest.json'
    if inventory.exists():independent(inventory)
    contents['installed-manifest.json']=json.dumps({'schema_version':1,'package_sha256':hashlib.sha256(package_data).hexdigest(),
        'files':{n:manifest[n] for n in contents}},sort_keys=True,indent=2).encode()
    changed=[n for n,raw in contents.items() if not (destination/n).is_file() or (destination/n).read_bytes()!=raw]
    result={'verified':True,**publisher,'changed':changed,'applied':False,'credentials_preserved':True}
    if not apply:return result
    if not worker_stopped:raise ValueError('Stop worker and pass --worker-stopped')
    for ledger in (destination/'.runtime').glob('worker-*.sqlite3'):
        independent(ledger)
        with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True)) as db:
            # A stopped worker with unfinished work must recover before upgrading.
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'work' in tables and db.execute("SELECT count(*) FROM work WHERE state='running' OR (state='done' AND acked=0)").fetchone()[0]:raise ValueError('Unfinished worker tasks or unacknowledged results')
    backup=destination.with_name(destination.name+'-before-upgrade-'+str(time.time_ns()));backup.mkdir()
    replaced=[]
    try:
        for name in changed:
            target=destination/name;saved=backup/name;saved.parent.mkdir(parents=True,exist_ok=True)
            if target.exists():shutil.copy2(target,saved)
            target.parent.mkdir(parents=True,exist_ok=True);tmp=target.with_name(target.name+'.upgrade-'+str(time.time_ns()))
            with tmp.open('xb') as stream:stream.write(contents[name])
            tmp.replace(target);replaced.append(name)
        (backup/'upgrade.json').write_text(json.dumps({'files':changed,'package_sha256':hashlib.sha256(package_data).hexdigest()}))
    except BaseException:
        for name in reversed(replaced):
            saved=backup/name
            if saved.exists():shutil.copy2(saved,destination/name)
            else:
                retained=backup/'failed-additions'/name;retained.parent.mkdir(parents=True,exist_ok=True)
                (destination/name).replace(retained)
        raise
    return {**result,'applied':True,'backup':str(backup)}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--package',type=Path,required=True);p.add_argument('--destination',type=Path,default=Path(__file__).resolve().parent);p.add_argument('--apply',action='store_true');p.add_argument('--worker-stopped',action='store_true')
    p.add_argument('--signature',type=Path);p.add_argument('--trusted-key',type=Path)
    a=p.parse_args();print(json.dumps(upgrade(a.package,a.destination,a.apply,a.worker_stopped,a.signature,a.trusted_key),ensure_ascii=False))
