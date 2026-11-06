"""Add exact task subjects to existing identities; configuration-only, no restart."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def upgrade(runtime):
    path=runtime/'nats.conf'
    config=json.loads(path.read_text())
    for account in config['accounts'].values():
        for user in account.get('users',[]):
            identity=user['user'];permissions=user['permissions']
            pub=permissions['publish']['allow'];sub=permissions['subscribe']['allow']
            if identity=='router':
                if 'corp.dev.v1.agent.*.task' not in pub:pub.append('corp.dev.v1.agent.*.task')
                if 'corp.dev.v1.agent.*.result' not in sub:sub.append('corp.dev.v1.agent.*.result')
            else:
                result=f'corp.dev.v1.agent.{identity}.result';task=f'corp.dev.v1.agent.{identity}.task'
                if result not in pub:pub.append(result)
                if task not in sub:sub.append(task)
    candidate=runtime/'nats.tasks.conf'
    candidate.write_text(json.dumps(config,ensure_ascii=False,indent=2))
    binary=os.environ.get('NATS_SERVER',str(ROOT/'.runtime/bin/nats-server'))
    subprocess.run([binary,'-t','-c',str(candidate)],check=True)
    backup=runtime/('nats.before-tasks-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.conf')
    shutil.copy2(path,backup);candidate.replace(path)
    print('Task ACL staged; restart the corresponding NATS to activate.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--runtime',type=Path,required=True)
    upgrade(parser.parse_args().runtime)
