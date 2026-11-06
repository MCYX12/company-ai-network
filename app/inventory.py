"""Discover CLI tools and match read-only version/status adapters. No shell or sessions."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

COMMANDS={'openclaw':'openclaw','hermes':'hermes','codex':'codex','claude-code':'claude'}
PATTERNS={
    'codex':re.compile(r'^codex-cli (\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)$'),
    'claude-code':re.compile(r'^(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?) \(Claude Code\)$'),
}

def locate(command):
    found=shutil.which(command)
    if found and Path(found).is_absolute():
        return str(Path(found).absolute())
    for folder in [Path.home()/'.local/bin',Path.home()/'.npm-global/bin',Path('/opt/homebrew/bin'),Path('/usr/local/bin')]:
        file=folder/command
        if file.is_file() and os.access(file,os.X_OK):
            return str(file)
    return None

def inspect_tool(tool, probe=True):
    # A self-developed agent runs its own worker; there is no fixed local command to
    # probe. Arbitrary commands are still never accepted.
    result={'tool':tool,'executable':None,'version':None,'adapter_status':'not_found',
            'capabilities':[],'checked_at':datetime.now(timezone.utc).isoformat(),
            'session_status':'not_inspected','task_execution_supported':False}
    if tool not in COMMANDS:
        result['adapter_status']='not_integrated'
        return result
    executable=locate(COMMANDS[tool])
    result['executable']=executable
    if not executable:
        return result
    if tool not in PATTERNS:
        result['adapter_status']='not_integrated'
        return result
    if not probe:
        result['adapter_status']='not_probed'
        return result
    # Fixed argv, no shell, no user-supplied action/flags, no project cwd.
    # Discard stderr instead of forwarding potentially sensitive diagnostic text.
    try:
        with tempfile.TemporaryDirectory(prefix='agent-version-') as cwd:
            completed=subprocess.run([executable,'--version'],cwd=cwd,stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=2,text=True)
        if completed.returncode != 0:
            result['adapter_status']='probe_failed'
            return result
        output=completed.stdout.strip()
        match=PATTERNS[tool].fullmatch(output) if len(output)<=256 else None
        if not match:
            result['adapter_status']='unrecognized_version'
            return result
        result.update(version=match.group(1),adapter_status='status_only',capabilities=['get_status'])
    except subprocess.TimeoutExpired:
        result['adapter_status']='probe_timeout'
    except (OSError,UnicodeError):
        result['adapter_status']='probe_failed'
    return result

def discover(probe=True):
    return {'os':platform.system(),'architecture':platform.machine(),
            'tools':[inspect_tool(tool,probe) for tool in COMMANDS],
            'scope':'installed CLI/version only; not task, conversation or authentication status'}

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--paths-only',action='store_true',help='Do not execute even --version')
    args=parser.parse_args()
    print(json.dumps(discover(not args.paths_only),ensure_ascii=False,indent=2))
