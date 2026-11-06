"""Answer ask_peer by running a configured one-shot local agent command.

A self-developed agent can reuse the built-in worker without new protocol code:
point a tool id at a local command that reads the task on stdin and prints the
answer on stdout. Configuration lives in <runtime>/harness.json:

    {"tools": {"dsh-harness": {"command": ["dsh", "--profile", "headless", "-"],
                               "timeout_seconds": 120}}}

The command is fixed by the administrator; nothing a caller sends becomes argv,
and the platform still never runs arbitrary commands.
"""
import json
import os
import re
import shutil
import subprocess
from .worker_kernel import process_options, terminate_process_tree
import tempfile
import time

from .common import RUNTIME
from .worker_kernel import sandbox_prefix

DEFAULT = {'dsh-harness': {'command': ['dsh', '--profile', 'headless', '-'], 'timeout_seconds': 120}}
MAX_OUTPUT = 16384
MAX_STDERR = 2048
# Only these variables reach the configured command. Everything else (API keys,
# tokens, proxy credentials) must be requested explicitly via "env". A one-shot
# harness gets a minimal, predictable environment instead of the operator's
# whole shell, following the OpenAgents dsh adapter's isolation model.
ENV_ALLOW = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TERM', 'TMPDIR', 'TZ', 'SHELL', 'USER',
             'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'COMSPEC', 'PATHEXT')


def _config():
    path = RUNTIME / 'harness.json'
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        raw = path.read_bytes()
        if len(raw) > 16384:
            return {}
        value = json.loads(raw)
    except (OSError, ValueError):
        return {}
    tools = value.get('tools') if isinstance(value, dict) else None
    return tools if isinstance(tools, dict) else {}


def tools():
    return set(DEFAULT) | set(_config())


def is_harness(row):
    return row.get('tool') in tools()


def settings(tool):
    entry = dict(DEFAULT.get(tool, {}))
    extra = _config().get(tool)
    if isinstance(extra, dict):
        entry.update(extra)
    return entry


def _base_env():
    return {key: value for key, value in os.environ.items() if key in ENV_ALLOW}


def _program(entry):
    command = entry.get('command')
    # A non-empty program plus string arguments; an explicit empty argument is allowed
    # because real CLIs need one (claude's `--tools ''` disables every tool). An empty
    # program or a non-list (for example a shell string) is still refused.
    if (not isinstance(command, list) or not command
            or not all(isinstance(x, str) for x in command) or not command[0]):
        return None, 'HARNESS_COMMAND_INVALID'
    program = command[0]
    if os.path.sep not in program:
        resolved = shutil.which(program)
    else:
        resolved = program if os.path.isfile(program) and os.access(program, os.X_OK) else None
    if not resolved:
        return None, 'HARNESS_COMMAND_NOT_FOUND'
    if not os.access(resolved, os.X_OK):
        return None, 'HARNESS_COMMAND_NOT_EXECUTABLE'
    return resolved, None


def preflight(tool):
    """Resolve and validate a configured command before any task arrives.

    Returns (adapter_status, hint) in the vocabulary the Router accepts, so a
    misconfigured harness shows up in the directory instead of failing a user's
    question. Mirrors the OpenAgents adapter preflight (resolve the entry point,
    reject an unusable configuration up front).
    """
    entry = settings(tool)
    _resolved, hint = _program(entry)
    if hint:
        return ('not_found' if hint == 'HARNESS_COMMAND_NOT_FOUND' else 'probe_failed'), hint
    try:
        sandbox_prefix(entry)
    except ValueError:
        return 'probe_failed', 'HARNESS_SANDBOX_INVALID'
    limit = entry.get('timeout_seconds', 120)
    if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
        return 'probe_failed', 'HARNESS_TIMEOUT_INVALID'
    return 'ready', None


# The platform owns the answering contract: a device's own prompt_prefix carries its
# local rules, but every harness must know how to apply for a directory and what it may
# rely on. Kept in sync with packaging/client/worker.py (same text, standalone artifact).
PLATFORM_RULES = (
    '平台提问协议（必须遵守）：\n'
    '1) 需要新目录授权时，在回答最后单独一行输出：DIRECTORY_REQUESTS: ["绝对路径"]'
    '（一行合法 JSON 数组，最多 6 个；Unix 使用 / 路径，Windows 使用盘符路径并正确转义 JSON 反斜杠；路径必须是你在本设备上搜索确认存在的真实绝对路径，'
    '多候选或找不到就只列候选并结束，绝不提交猜测路径）。\n'
    '2) 不要读取、修改或直接调用平台运行目录与账本（如 .runtime-lan/ 下的文件），也不要用命令行去“直接登记”或“直查账本”。\n'
    '3) 只依据本次随附的授权资料（含已授权目录的清单/摘要）作答；未授权内容说明不足，不编造进度或百分比。\n'
    '4) 默认精简作答：先给结论（1-3 句），再列已完成/进行中或卡点/来源与时间；只保留影响结论的证据，并说明资料限制。'
    '用户明确要求详细分析时才展开，否则不要复述整份资料。')


def _prompt(row, payload, entry=None, coverage=None):
    """Return (prompt, granted_roots) for one harness task; fills `coverage` when given."""
    lead = (entry or {}).get('prompt_prefix')
    lead = lead.strip() if isinstance(lead, str) and lead.strip() else None
    notes_roots=[]
    try:
        from .peer_question import build_prompt
        prompt, sources = build_prompt(row, payload, coverage=coverage)
        # build_prompt has already embedded exactly these roots. Calling notes_context a
        # second time would re-digest and could disagree about which roots were used.
        notes_roots=[root for source in sources if isinstance(source, dict)
                     and source.get('kind')=='project_notes'
                     for root in (source.get('roots') or [])]
    except (ValueError, TypeError, KeyError, OSError):
        prompt = ('你是本机上的智能体，正在回答另一台电脑的用户问题。只能依据本次给出的问题作答，'
                  '不确定时明确说明。\n用户问题：\n' + str(payload.get('question', '')))
    # Platform contract first, then the device owner's local rules, then the platform
    # prompt: the question's own 1..1200 length check already happened on the caller text.
    parts=[PLATFORM_RULES]+([lead] if lead else [])+[prompt]
    return '\n\n'.join(parts), list(notes_roots or [])


PROPOSAL_LINE = re.compile(r'(?im)^[ \t>*`_]*directory_requests[ \t]*[*_]{0,2}[ \t]*[:：][ \t]*'
                           r'(?P<body>\[.*\]|\{.*\})[ \t]*[*`_]*[ \t]*$')


def _proposals(text):
    """Lift a directory proposal out of a one-shot harness answer.

    The harness returns text only, so the platform defines one line convention and also
    accepts the {"path": ...} shape an LLM tends to emit. Anything unparsable is left in
    the text untouched instead of being half-read.
    """
    found = []
    def take(match):
        try:
            body = json.loads(match.group('body'))
        except ValueError:
            return match.group(0)
        items = body if isinstance(body, list) else [body]
        roots = []
        for item in items:
            if isinstance(item, str):
                roots.append(item)
            elif isinstance(item, dict):
                root = item.get('root') or item.get('path')
                if isinstance(root, str):
                    roots.append(root)
        if not roots or len(roots) > 6:
            return match.group(0)
        found.extend(roots)
        return ''
    remaining = PROPOSAL_LINE.sub(take, text)
    if not found:
        return text, []
    return remaining.strip(), [{'root': root} for root in found]


def execute(row, payload, deadline):
    question = payload.get('question')
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1200:
        return 'INVALID_REQUEST', None
    entry = settings(row.get('tool'))
    command = entry.get('command')
    _resolved, hint = _program(entry)
    if hint:
        return 'TOOL_UNAVAILABLE', {'hint': hint}
    limit = entry.get('timeout_seconds', 120)
    if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
        return 'TOOL_UNAVAILABLE', {'hint': 'HARNESS_TIMEOUT_INVALID'}
    timeout = min(float(limit), max(0.0, deadline - time.time() - 2))
    if timeout <= 0:
        return 'EXPIRED', None
    prompt_started=time.time()
    coverage={}
    prompt, granted_roots = _prompt(row, payload, entry, coverage=coverage)
    prompt_seconds=time.time()-prompt_started
    env = _base_env()
    extra_env = entry.get('env')
    if isinstance(extra_env, dict):
        for key, value in extra_env.items():
            if isinstance(key, str) and isinstance(value, str):
                env[key] = value
    # An optional OS sandbox wrapper (bwrap/systemd-run/...) is the only mechanism that gives
    # the answering process a real boundary; the platform cannot create one portably itself.
    try:
        argv = sandbox_prefix(entry) + list(command)
    except ValueError:
        return 'TOOL_UNAVAILABLE', {'hint': 'HARNESS_SANDBOX_INVALID'}
    try:
        # The answering command must not start inside the platform's own directory: a
        # harness that inherits the server cwd can read and even write .runtime-lan
        # ledgers directly, which bypasses the platform API (the request then never
        # reaches the caller's answer). A configured cwd keeps it out of the way.
        cwd = entry.get('cwd')
        cwd = cwd if isinstance(cwd, str) and os.path.isdir(cwd) else None
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout,
                                    stderr=stderr, env=env, cwd=cwd, **process_options())
            harness_started=time.time()
            try:
                proc.communicate(prompt.encode(), timeout=timeout)
                harness_seconds=time.time()-harness_started
            except subprocess.TimeoutExpired:
                terminate_process_tree(proc)
                proc.communicate()
                return 'EXECUTION_TIMEOUT', None
            if proc.returncode != 0:
                stderr.seek(0)
                tail = stderr.read(MAX_STDERR).decode('utf-8', errors='replace').strip()
                return 'EXECUTION_FAILED', {'tool': row.get('tool'), 'exit_code': proc.returncode,
                                            'stderr_tail': tail,
                                            'hint': 'Check the configured harness command on the executing device.'}
            stdout.seek(0)
            raw = stdout.read(MAX_OUTPUT + 1)
    except FileNotFoundError:
        return 'TOOL_UNAVAILABLE', {'hint': 'HARNESS_COMMAND_NOT_FOUND'}
    except (OSError, ValueError):
        return 'EXECUTION_FAILED', {'tool': row.get('tool'), 'hint': 'HARNESS_COMMAND_FAILED'}
    if len(raw) > MAX_OUTPUT:
        return 'OUTPUT_TOO_LARGE', None
    text = raw.decode('utf-8', errors='replace').strip()
    if not text:
        return 'EXECUTION_FAILED', None
    # A one-shot harness can only answer with text, so a directory proposal has to be
    # lifted out of it; otherwise the platform never sees the request and the caller is
    # told "I applied" while nothing is recorded.
    text, proposals = _proposals(text)
    data = {'synthetic': False, 'agent_id': row.get('agent_id'), 'tool': row.get('tool'),
            'source': 'local_harness', 'text': text,
            'timings': {'prompt_build_seconds': round(prompt_seconds,3),
                        'harness_seconds': round(harness_seconds,3)}}
    if proposals:
        data['directory_requests'] = proposals
    if coverage:
        # The harness path must report the same digest coverage as the built-in path.
        data['context_sources'] = [{'kind': 'project_notes', 'roots': list(granted_roots or []),
                                    'truncated': bool(coverage.get('truncated')),
                                    'coverage': coverage}]
    # Report which approved grants this answer actually summarised, so the platform's
    # validity check and the caller see the same picture.
    try:
        from .directory_context import CURRENT
        ids=[g.get('id') for g in (CURRENT.get() or [])
             if g.get('state')=='approved' and g.get('root') in granted_roots and g.get('id')]
    except (AttributeError,TypeError,KeyError):ids=[]
    if ids:data['directory_grant_ids']=ids
    return 'OK', data
