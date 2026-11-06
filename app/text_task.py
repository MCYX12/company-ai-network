"""Explicitly enabled, bounded CLI text tests using existing local model configuration."""
import os
import re
import subprocess
from .worker_kernel import process_options, terminate_process_tree
import tempfile
import time
from pathlib import Path
from .inventory import inspect_tool
from .registry import capabilities
from .model_network import model_environment
from . import model_stub

# Only these lines of a failed CLI's stderr reach the device log (never prompt text).
ERROR_SIGNAL = re.compile(r'(?i)(error|failed|denied|unauthor|usage limit|quota|rate limit|not logged|expired|429)')


def enabled():
    return os.environ.get('ALLOW_CLOUD_MODEL_TESTS') == '1'


def stub_enabled():
    return os.environ.get('POC_LOCAL_MODEL_STUB') == '1'


def execute_text(row, payload, deadline):
    if stub_enabled():
        # Deterministic offline stand-in: no network, no CLI, explicitly labelled as simulated.
        prompt = payload.get('prompt')
        if set(payload) != {'prompt'} or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
            return 'INVALID_REQUEST', None
        if 'generate_text' not in capabilities(row):
            return 'TOOL_UNAVAILABLE', None
        return 'OK', {'synthetic': True, 'agent_id': row['agent_id'], 'tool': row['tool'],
                      'source': model_stub.MARKER, 'text': model_stub.answer(prompt)}
    if not enabled():
        return 'CLOUD_TEST_DISABLED', None
    prompt = payload.get('prompt')
    if set(payload) != {'prompt'} or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
        return 'INVALID_REQUEST', None
    status = inspect_tool(row['tool'])
    if row['tool'] not in {'codex', 'claude-code'} or status['adapter_status'] != 'status_only':
        return 'TOOL_UNAVAILABLE', None
    timeout = min(90, deadline - time.time() - 2)
    if timeout <= 0:
        return 'EXPIRED', None
    try:child_env=model_environment(row['tool'])
    except (OSError,ValueError,TypeError):
        return 'EXECUTION_FAILED', {'hint':'MODEL_NETWORK_CONFIG_INVALID'}
    with tempfile.TemporaryDirectory(prefix='agent-text-test-') as folder:
        output = Path(folder) / 'answer.txt'
        if row['tool'] == 'codex':
            argv = [status['executable'], 'exec', '--skip-git-repo-check', '--ephemeral',
                    '--sandbox', 'read-only', '-c', 'approval_policy="never"',
                    '-c', 'features.shell_tool=false', '--color', 'never',
                    '--output-last-message', str(output), '-']
        else:
            argv = [status['executable'], '--print', '--tools', '', '--strict-mcp-config',
                    '--disable-slash-commands', '--no-session-persistence',
                    '--settings', '{"disableAllHooks":true}', '--output-format', 'text']
        # No arbitrary argv, shell interpolation, project directory, or resumed session.
        # Configured provider/auth remain CLI-owned. Diagnostic output is not returned to
        # the caller; only lines that look like an error are echoed to this device's own
        # service log. A quota, login or proxy failure is otherwise indistinguishable
        # from any other non-zero exit, and the operator can only guess from the hint
        # below. Prompt text echoed by the CLI is deliberately never logged.
        task = 'Answer using only the supplied evidence. Do not use tools or access files. Distinguish documented claims from verified observations:\n' + prompt
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen(argv, cwd=folder, stdin=subprocess.PIPE, stdout=stdout,
                                    stderr=stderr, env=child_env, **process_options())
            try:
                proc.communicate(task.encode(), timeout=timeout)
            except subprocess.TimeoutExpired:
                terminate_process_tree(proc)
                proc.communicate()
                return 'EXECUTION_TIMEOUT', None
            if proc.returncode != 0:
                stderr.seek(0)
                lines = [line.strip()[:200] for line in
                         stderr.read(65536).decode('utf-8', errors='replace').splitlines() if line.strip()]
                signals = [line for line in lines if re.search(ERROR_SIGNAL, line)]
                # Some CLIs report the API failure on stdout, not stderr (observed on the Mac's
                # claude-code CLI: an HTML '"Service unavailable"' page). Reading stdout is opt-in
                # because it can carry answer text; only lines that look like errors are logged.
                if not signals and os.environ.get('POC_CLI_STDOUT_DIAGNOSTICS') == '1':
                    stdout.seek(0)
                    out_lines = [line.strip()[:200] for line in
                                 stdout.read(65536).decode('utf-8', errors='replace').splitlines() if line.strip()]
                    signals = [line for line in out_lines if re.search(ERROR_SIGNAL, line)]
                if signals:
                    print('%s CLI exited %d: %s' % (row['tool'], proc.returncode,
                                                    ' | '.join(signals[-3:])[-400:]), flush=True)
                else:
                    # The exit code is the only diagnostic when the CLI prints nothing loggable;
                    # without this line a non-zero exit with empty stderr used to leave no trace.
                    print('%s CLI exited %d (no error signal on stderr or stdout)' % (row['tool'], proc.returncode),
                          flush=True)
                failure = {'tool':row['tool'], 'exit_code':proc.returncode,
                           'hint':'Check this CLI model/login configuration on the executing device.'}
                if signals:
                    failure['diagnostic'] = ' | '.join(signals[-3:])[-400:]
                return 'EXECUTION_FAILED', failure
            if row['tool'] == 'codex':
                if not output.exists():
                    return 'EXECUTION_FAILED', None
                with output.open('rb') as stream:
                    raw = stream.read(16385)
            else:
                stdout.seek(0)
                raw = stdout.read(16385)
            if len(raw) > 16384:
                return 'OUTPUT_TOO_LARGE', None
            answer = raw.decode('utf-8', errors='replace').strip()
            if not answer:
                return 'EXECUTION_FAILED', None
            return 'OK', {'synthetic':False, 'agent_id':row['agent_id'], 'tool':row['tool'],
                          'source':'configured_cli_model', 'text':answer}
