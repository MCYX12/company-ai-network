"""Deny by default; project grants come only from the administrator registry."""
from .conversation_access import approved
from .registry import load_registry, visible, capabilities

def allowed(source, target, action, payload, registry=None):
    directory = load_registry() if registry is None else registry
    if not visible(source, target, directory):
        return False
    if action not in capabilities(directory[target]):
        return False
    if action in {'ask_peer','generate_text'}:
        common=set(directory[source]['projects']) & set(directory[target]['projects'])
        # approved() now routes through the unified authorization entry (assessment item 4).
        if not approved(source,target,payload.get('projects',list(common))):
            return False
    if action=='ask_peer':
        projects=payload.get('projects')
        return isinstance(projects,list) and bool(projects) and set(projects).issubset(set(directory[source]['projects']) & set(directory[target]['projects']))
    if action in {'get_status', 'generate_text'}:
        return True
    project = payload.get('project_id')
    return (action == 'get_project_progress' and project is not None
            and project in directory[source]['projects'] and project in directory[target]['projects'])
