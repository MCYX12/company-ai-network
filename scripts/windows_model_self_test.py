"""Owner-run, fixed-material Harness test; preserves every bounded synthetic answer.

This invokes the installed tool-free adapter, never an original desktop session.
It tests execution and exact output adherence, not business answer quality.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

TOKEN = 'BATCH_OK'
PROMPT = ('这是部署自检。下面是本次唯一的获批测试资料：\n'
          '自检标记：BATCH_OK\n'
          '请原样输出资料中的自检标记，不加解释、引号、Markdown 或标点。'
          '不需要工具、个人资料或聊天历史。')


def trial(run, config, timeout):
    start = time.monotonic()
    try:
        code, data = run(config, PROMPT, time.time() + timeout)
    except Exception as error:
        # Provider exceptions/stderr may carry credentials; retain only the class.
        return {'code': 'PROBE_FAILED', 'error_type': type(error).__name__,
                'answer_matches': False, 'elapsed_seconds': round(time.monotonic()-start, 3)}
    text = data.get('text') if isinstance(data, dict) else None
    valid = isinstance(text, str)
    return {'code': code, 'answer_matches': code == 'OK' and valid and text.strip() == TOKEN,
            'answer_text': text[:512] if code == 'OK' and valid else None,
            'answer_text_truncated': valid and len(text) > 512,
            'answer_sha256': hashlib.sha256(text.encode()).hexdigest() if valid else None,
            'elapsed_seconds': round(time.monotonic()-start, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--timeout', type=int, default=45)
    args = parser.parse_args()
    if not 1 <= args.trials <= 10 or not 5 <= args.timeout <= 75 or args.trials*args.timeout > 300:
        parser.error('Use 1..10 fixed trials, 5..75 seconds each, at most 300 seconds total')
    root = args.install_dir.resolve()
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location('installed_probe_worker', root/'worker.py')
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    config = worker.settings(root)
    command = config.get('command')
    if not isinstance(command, list) or len(command) < 2 or Path(command[1]).resolve() != root/'dsh_desktop_adapter.py':
        parser.error('Only the installed separate-profile Harness adapter is supported')
    report = {'schema_version': 1, 'prompt_version': 'approved-fixed-material-v1',
              'started_at': datetime.now(timezone.utc).isoformat(),
              'fixed_trial_count': args.trials, 'retry_until_success': False,
              'original_history_read': False, 'private_material_supplied': False,
              'status': 'running', 'ok': False, 'trials': []}
    # Exclusive creation before any model call; never erase a previous failure.
    with args.output.open('x', encoding='utf-8') as output:
        def save():
            output.seek(0); output.write(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
            output.truncate(); output.flush()
        save()
        for index in range(args.trials):
            row = trial(worker.run_command, config, args.timeout)
            row['trial'] = index+1
            report['trials'].append(row)
            save()
        report.update(status='complete', ok=all(row['answer_matches'] for row in report['trials']),
                      completed_at=datetime.now(timezone.utc).isoformat())
        save()
    print(json.dumps({'ok': report['ok'], 'trials': len(report['trials']),
                      'strict_match_count': sum(row['answer_matches'] for row in report['trials'])}))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
