"""Trusted account lifecycle bridge to existing certificate enrollment."""
import base64,json,subprocess,tempfile,fcntl
from pathlib import Path
from .accounts import Accounts
from .common import RUNTIME

def handle(actor,body):
    with (RUNTIME/"member-lifecycle.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _handle(actor,body)

def _handle(actor,body):
    store=Accounts();op=body.get('op')
    if not body:return store.listing()
    if op=='invite' and set(body)=={'op','role','projects'}:return store.invite(actor,body['role'],body['projects'])
    if op=='import' and set(body)=={'op','registration'}:return {'id':store.import_member(actor,body['registration'])}
    if op=='admin_status' and set(body)=={'op','name','enabled'}:
        store.admin_status(actor,body['name'],body['enabled']);return {'ok':True}
    fields={'op','id'}|({'askable'} if op=='activate' else set())
    if not {'op','id'}<=set(body)<=fields:raise ValueError('Invalid fields')
    member=next((r for r in store.listing()['members'] if r['id']==body['id']),None)
    if not member:raise ValueError('Unknown member')
    ident=member['id']
    if op in {'disable','reject'}:
        store.status(actor,ident,'disabled' if op=='disable' else 'rejected');return {'ok':True}
    if op=='activate':
        if member['state']=='active':return {'ok':True,'already_active':True}
        from scripts.enroll_agent import enroll
        from .registry import load_registry
        store.status(actor,ident,'provisioning')
        if ident not in load_registry():
            row=dict(agent_id=ident,employee_id=ident,device_id=ident+'-device',tool='fixture',projects=member['projects'],enabled=True)
            if body.get('askable',True):
                # Default is dual-role: worker ACL plus the dialogue capability, so the
                # device can answer as well as ask. Pass {"askable":false} for caller-only.
                row['capabilities']=['get_status','ask_peer']
                enroll(RUNTIME,row,client_only=False)
            else:
                enroll(RUNTIME,row,client_only=True)
        subprocess.run(['systemctl','--user','restart','company-agent-stack.service'],check=True,timeout=20)
        store.status(actor,ident,'active');return {'ok':True}
    if op=='package' and member['state']=='active':
        from scripts.export_client_enrollment import export
        with tempfile.TemporaryDirectory(dir=RUNTIME) as tmp:
            path=Path(tmp)/'enrollment.zip';export(RUNTIME,ident,path)
            return {'filename':ident+'-enrollment.zip','base64':base64.b64encode(path.read_bytes()).decode()}
    raise ValueError('状态或操作无效')
