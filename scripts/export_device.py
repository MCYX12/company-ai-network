"""Export only one device's enrolled identities, never CA/server/router keys."""
import argparse
import json
import os
from pathlib import Path
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.registry import validate_registry

def export(runtime,device,output):
    source=runtime/'registry.json'
    registry=validate_registry(json.loads((source if source.exists() else ROOT/'config/registry.json').read_text()))
    rows=[r for r in registry.values() if r['device_id']==device and r['enabled']]
    if not rows:
        raise ValueError('Device has no enabled identities')
    if output.exists():
        raise ValueError('Output already exists; choose a new filename')
    os.umask(0o077)
    output.parent.mkdir(parents=True,exist_ok=True)
    prefix='company-agent-device/'
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as out:
        for path in sorted((ROOT/'app').glob('*.py')):
            out.write(path,prefix+'app/'+path.name)
        out.writestr(prefix+'scripts/__init__.py','')
        out.write(ROOT/'scripts/summarize_local_project.py',prefix+'scripts/summarize_local_project.py')
        out.write(ROOT/'requirements.lock',prefix+'requirements.lock')
        out.write(runtime/'certs/ca.crt',prefix+'.runtime/certs/ca.crt')
        for row in rows:
            for ext in ['crt','key']:
                name=row['agent_id']+'.'+ext
                out.write(runtime/'certs'/name,prefix+'.runtime/certs/'+name)
        out.writestr(prefix+'.runtime/registry.json',json.dumps({'schema_version':1,'agents':rows},indent=2))
        out.writestr(prefix+'device.json',json.dumps({'device_id':device,'agents':[r['agent_id'] for r in rows]},indent=2))
        endpoint=(runtime/'endpoint.txt').read_text() if (runtime/'endpoint.txt').exists() else 'tls://localhost:14222\n'
        out.writestr(prefix+'endpoint.txt',endpoint)
        out.writestr(prefix+'start-device.sh','''#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
chmod 700 .runtime .runtime/certs
chmod 600 .runtime/certs/*.key
export POC_RUNTIME="$PWD/.runtime"
export NATS_URL="$(cat endpoint.txt)"
exec .venv/bin/python -m app.device --config device.json
''')
        out.writestr(prefix+'start-cloud-test.sh','''#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export ALLOW_CLOUD_MODEL_TESTS=1
exec bash start-device.sh
''')
        out.writestr(prefix+'start-device.ps1','''$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:POC_RUNTIME = Join-Path $PSScriptRoot ".runtime"
$env:NATS_URL = (Get-Content endpoint.txt -Raw).Trim()
& .\.venv\Scripts\python.exe -m app.device --config device.json
exit $LASTEXITCODE
''')
    output.chmod(0o600)
    print(f'Exported {len(rows)} independent identities for {device}: {output}')

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--device',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    export(args.runtime,args.device,args.output)
