"""Trusted administrator on the data-owning computer; reads are local only."""
from .file_access import FileAccess

def handle(body):
    store=FileAccess()
    if not body:return {'scopes':store.listing(),'location':'本控制台所在电脑'}
    if set(body)=={'revoke'}:
        store.revoke(body['revoke']);return {'revoked':body['revoke']}
    fields={'caller','target','project','path','kind','depth','seconds'}
    if set(body)!=fields:raise ValueError('Invalid fields')
    from .registry import load_registry,visible
    registry=load_registry()
    if not visible(body['caller'],body['target'],registry) or body['project'] not in set(registry[body['caller']]['projects']) & set(registry[body['target']]['projects']):raise ValueError('Project membership required')
    # UI cannot approve cloud transfer; separate adapter/approval work is required.
    return {'id':store.create(**body,cloud=False)}
