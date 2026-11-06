"""One device supervisor, independent TLS identity and connection per instance."""
import argparse
import asyncio
import json
from pathlib import Path
from .agent import main as run_agent
from .registry import load_registry

async def main(path):
    config = json.loads(Path(path).read_text())
    if set(config) != {'device_id', 'agents'} or not isinstance(config['agents'], list):
        raise ValueError('Expected device_id and agents list')
    identities = config['agents']
    if not identities or not all(isinstance(a, str) for a in identities) or len(set(identities)) != len(identities):
        raise ValueError('Device requires unique agent identities')
    registry = load_registry()
    for identity in identities:
        if identity not in registry or registry[identity]['device_id'] != config['device_id'] or not registry[identity]['enabled']:
            raise ValueError('Device/identity binding mismatch: '+identity)
    async with asyncio.TaskGroup() as group:
        for identity in identities:
            group.create_task(run_agent(identity), name=identity)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    asyncio.run(main(parser.parse_args().config))
