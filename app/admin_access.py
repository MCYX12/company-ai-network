"""Local administrator UI operations; never exposed through agent ingress."""
import uuid
from .conversation_access import AccessStore
from .registry import load_registry,visible

def handle(body):
    store=AccessStore()
    if not body:
        rows=store.listing();registry=load_registry()
        for r in rows:
            r['employee']=registry.get(r['caller'],{}).get('employee_id','未登记')
            r['device']=registry.get(r['caller'],{}).get('device_id','未登记')
        return {'requests':rows,'limit':100}
    if set(body)!={'id','decision','seconds','reason'}:raise ValueError('Invalid fields')
    uuid.UUID(body['id'])
    if body['decision'] not in {'approved','denied','revoked'}:raise ValueError('Invalid decision')
    if type(body['seconds']) is not int or not (body['seconds']==0 or 1<=body['seconds']<=86400):raise ValueError('Invalid duration')
    if not isinstance(body['reason'],str) or not 1<=len(body['reason'].strip())<=500:raise ValueError('Reason required')
    if body['decision']=='approved':
        registry=load_registry()
        with store.db() as db:
            row=db.execute('SELECT caller,target,projects FROM approvals WHERE id=?',(body['id'],)).fetchone()
        import json
        if not row or not visible(row[0],row[1],registry) or not set(json.loads(row[2])).issubset(set(registry[row[0]]['projects']) & set(registry[row[1]]['projects'])):raise ValueError('Registration or project scope changed')
    store.decide(body['id'],body['decision'],body['seconds'],body['reason'].strip())
    return {'updated':body['id']}
