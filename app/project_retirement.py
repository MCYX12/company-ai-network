"""Local project tombstones prevent an obsolete publisher from restoring retired data."""
import json
from pathlib import Path


def retired(runtime, project):
    path = Path(runtime) / 'project-retirements.json'
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink():
        raise ValueError('Linked retirement state')
    data = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(data, dict) or type(data.get('format')) is not int or
            data['format'] != 1 or not isinstance(data.get('retired'), list) or
            not all(isinstance(p, str) and p for p in data['retired'])):
        raise ValueError('Invalid retirement state')
    return project in data['retired']
