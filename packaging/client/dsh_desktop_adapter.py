"""Windows DeepSeek Harness bridge; fresh, tool-free headless sessions only."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app.worker_kernel import process_options, terminate_process_tree

MAX_OUTPUT = 1024 * 1024
TEXT_ONLY_RULES = ('本次是纯文本回答模式，没有文件、Shell、Web 或其他工具。'
                   '只依据下面随附的获批资料回答，不输出工具调用、工具标记或假装执行命令。'
                   '设备所有者已确认存在的目录可按平台约定申请；未确认的路径应说明未知。\n\n')


def capture(argv, text, env, cwd, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Harness deadline')
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout,
                                stderr=stderr, env=env, cwd=cwd, **process_options())
        try:
            proc.communicate(text.encode('utf-8'), timeout=remaining)
        except subprocess.TimeoutExpired:
            terminate_process_tree(proc)
            proc.communicate()
            raise TimeoutError('Harness deadline') from None
        if proc.returncode:
            # Provider errors can contain account data; do not forward stderr.
            raise ValueError('Harness process failed')
        stdout.seek(0)
        output = stdout.read(MAX_OUTPUT + 1)
    if len(output) > MAX_OUTPUT:
        raise ValueError('Harness output too large')
    return output.decode('utf-8')


def final_text(output):
    events = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    if any(e.get('type') in {'tool_call', 'tool_result', 'error'} for e in events):
        raise ValueError('Unexpected Harness event')
    finals = [e for e in events if e.get('type') == 'final']
    if len(finals) != 1:
        raise ValueError('Missing complete Harness answer')
    text = finals[0].get('text')
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 16384:
        raise ValueError('Invalid Harness answer')
    if any(marker in text for marker in ('<｜｜DSML｜｜', '<||DSML||', '<tool_call', '<function_calls', '<|tool_call|>')):
        raise ValueError('Unexecuted tool markup is not an answer')
    return text.strip()


def runtime(app, home):
    if not home.is_absolute():
        raise ValueError('An absolute Harness home is required')
    app, home = app.resolve(), home.resolve()
    desktop_home = (Path.home() / '.dsh').resolve()
    if not home.is_absolute() or home == desktop_home or home.is_relative_to(desktop_home):
        raise ValueError('A separate Harness home is required')
    if home == app or home.is_relative_to(app):
        raise ValueError('Harness home must be outside the application')
    exe = app / 'DeepSeek Harness.exe'
    cli = app / 'resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'
    if not exe.is_file():
        raise ValueError('Desktop runtime missing')
    env = dict(os.environ, ELECTRON_RUN_AS_NODE='1', DSH_HOME=str(home))
    helper = Path(__file__).with_name('dsh_desktop_profile.cjs')
    return [str(exe), '--expose-internals'], str(cli), str(helper), env, home


def prepare(app, home, deadline):
    base, cli, helper, env, home = runtime(app, home)
    if home.exists():
        raise ValueError('Existing Harness home is preserved')
    result = capture(base + [helper, 'prepare', str(home)], '', env, home.parent, deadline)
    return json.loads(result)


def check(app, home, deadline):
    base, cli, helper, env, home = runtime(app, home)
    version = capture(base + [cli, '--version'], '', env, home, deadline).strip()
    if version != '0.2.0-rc.2':
        raise ValueError('Unverified Harness version')
    if not (home / 'company-model.patch.yml').is_file():
        raise ValueError('Prepared Harness profile missing')
    common = base + [cli, '--profile', 'headless', '--patch',
                     str(home / 'company-model.patch.yml')]
    composed = capture(common + ['--dump-config'], '', env, home, deadline)
    result = capture(base + [helper, 'verify'], composed, env, home, deadline)
    summary = json.loads(result)
    if summary.get('active_tool_rows') != [] or not summary.get('selection'):
        raise ValueError('Harness profile verification failed')
    return common, env, home, summary


def answer(app, home, prompt, timeout=140):
    if not prompt.strip() or len(prompt.encode('utf-8')) > 131072:
        raise ValueError('Invalid prompt')
    deadline = time.monotonic() + timeout
    # Check the composed configuration BEFORE any model invocation. Checking
    # tool events afterwards alone would not prevent a tool from reading files.
    common, env, cwd, _ = check(app, home, deadline)
    output = capture(common + ['--json', '-'], TEXT_ONLY_RULES + prompt, env, cwd, deadline)
    return final_text(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--app', type=Path, default=Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/DeepSeek Harness')
    parser.add_argument('--timeout', type=float, default=140)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'win32' or not 1 <= args.timeout <= 300:
        parser.error('Windows and a timeout between 1 and 300 seconds are required')
    try:
        deadline = time.monotonic() + args.timeout
        if args.prepare:
            prepare(args.app, args.home, deadline)
            print(json.dumps(check(args.app, args.home, deadline)[3], ensure_ascii=False))
        elif args.check:
            print(json.dumps(check(args.app, args.home, deadline)[3], ensure_ascii=False))
        else:
            print(answer(args.app, args.home, sys.stdin.read(), args.timeout))
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as error:
        print('HARNESS_ADAPTER_FAILED:' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
