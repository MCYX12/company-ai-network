"""Administrator-only device revocation, with backups and fail-closed write order."""
import argparse
from datetime import datetime,timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.registry import validate_registry,ID


def candidate(registry,config,device):
    if not isinstance(device,str) or not ID.fullmatch(device):raise ValueError('Invalid device ID')
    registry=json.loads(json.dumps(registry));config=json.loads(json.dumps(config))
    rows=validate_registry(registry)
    identities=sorted(k for k,v in rows.items() if v['device_id']==device)
    if not identities:raise ValueError('Unknown device; no changes made')
    users=config['accounts']['POC']['users']
    if any(u.get('user') in {'router','server'} for u in users if u.get('user') in identities):raise ValueError('Infrastructure identities cannot be revoked here')
    for row in registry['agents']:
        if row['agent_id'] in identities:row['enabled']=False
    config['accounts']['POC']['users']=[u for u in users if u.get('user') not in identities]
    validate_registry(registry)
    return registry,config,identities


def atomic(path,data):
    fd,name=tempfile.mkstemp(prefix='.revoke-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if Path(name).exists():Path(name).unlink()


def revoke(runtime,device,reason,apply=False,binary=None):
    runtime=Path(runtime).resolve()
    if not reason.strip() or len(reason)>500:raise ValueError('Reason required (1..500 characters)')
    paths=[runtime/'registry.json',runtime/'nats.conf']
    if any(p.is_symlink() or not p.is_file() for p in paths):raise ValueError('Explicit regular registry and NATS config required')
    # Cooperating administrators must use this lock; do not edit files concurrently.
    with (runtime/'.admin.lock').open('a') as lock:
        os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX)
        originals=[p.read_bytes() for p in paths]
        registry,config,identities=candidate(*[json.loads(b) for b in originals],device)
        report={'device_id':device,'identities':identities,'reason':reason,'applied':False,'transport_activation_required':True,'running_execution_stopped':False}
        if not apply:return report
        staged=[json.dumps(x,ensure_ascii=False,indent=2).encode() for x in [registry,config]]
        fd,name=tempfile.mkstemp(prefix='.nats-revocation-',dir=runtime)
        try:
            with os.fdopen(fd,'wb') as f:f.write(staged[1])
            subprocess.run([str(binary or ROOT/'.runtime/bin/nats-server'),'-t','-c',name],check=True,capture_output=True,timeout=10)
        finally:Path(name).unlink(missing_ok=True)
        if any(p.read_bytes()!=raw for p,raw in zip(paths,originals)):raise ValueError('Configuration changed during validation')
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup=runtime/'admin-history'/stamp;backup.mkdir(parents=True,mode=0o700)
        os.chmod(backup.parent,0o700)
        for p,raw in zip(paths,originals):atomic(backup/p.name,raw)
        atomic(backup/'report.json',json.dumps(report,indent=2).encode())
        # Registry first: new Router queries fail closed even if config activation is interrupted.
        for p,data in zip(paths,staged):atomic(p,data)
        report.update(applied=True,backup=str(backup),applied_at=datetime.now(timezone.utc).isoformat())
        atomic(backup/'report.json',json.dumps(report,indent=2).encode())
        return report


def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--device',required=True);p.add_argument('--reason',required=True);p.add_argument('--apply',action='store_true');a=p.parse_args()
    print(json.dumps(revoke(a.runtime,a.device,a.reason,a.apply),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
