"""Re-run the suite and refresh project facts from real evidence (assessment 3.1).

The recorded evidence now carries the clocks and provenance a fact needs:
kind, executed_at, recorded_at, source revision, command, result, environment
and the ids of the tests that actually ran (covered). A rebuilt fact file can
then never invent a fresh execution time, and an item only upgrades when its own
declared test selectors are among the covered ids.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import build_facts


def parse_result(output):
    ran = re.search(r'Ran (\d+) test', output)
    skipped = re.search(r'skipped=(\d+)', output)
    return {'ran': int(ran.group(1)) if ran else None,
            'skipped': int(skipped.group(1)) if skipped else 0,
            'result': 'OK' if 'OK' in output.splitlines()[-1] else 'FAILED'}


def collect_covered(root):
    """Ids of the tests discoverable from tests/ (module.Class.method)."""
    import unittest
    start = Path(root) / 'tests'
    if not start.is_dir():
        return []
    try:
        suite = unittest.TestLoader().discover(str(start), pattern='test_*.py')
    except (ImportError, OSError, ValueError):
        return []
    ids = []

    def walk(item):
        for test in item:
            if isinstance(test, unittest.TestSuite):
                walk(test)
            else:
                ids.append(test.id())

    walk(suite)
    return sorted(set(ids))


def environment():
    return {'python': platform.python_version(), 'platform': platform.platform()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--skip-tests', action='store_true')
    args = parser.parse_args()
    root = args.root
    evidence = root / 'facts/evidence'
    evidence.mkdir(parents=True, exist_ok=True)
    if not args.skip_tests:
        started = datetime.now(timezone.utc)
        run = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'],
                             cwd=root, capture_output=True, text=True, timeout=600)
        summary = parse_result(run.stdout + run.stderr)
        stamp = started.strftime('%Y%m%dT%H%M%SZ')
        payload = {'kind': 'test_run', 'scope': 'full test suite', 'date': stamp,
                   'executed_at': started.isoformat(),
                   'recorded_at': datetime.now(timezone.utc).isoformat(),
                   'revision': build_facts.current_revision(root),
                   'command': 'python -m unittest discover -s tests -p test_*.py',
                   'environment': environment(), 'covered': collect_covered(root), **summary}
        target = evidence / ('test-run-%s.json' % stamp)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'evidence': str(target), **summary}, ensure_ascii=False))
    for spec_path in sorted((root / 'facts').glob('*.spec.json')):
        spec = json.loads(spec_path.read_text(encoding='utf-8'))
        data = build_facts.build(spec, root)
        output = root / 'facts' / (spec['project_id'] + '.json')
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'rebuilt': str(output), 'items': len(data['items']), 'revision': data.get('revision')},
                         ensure_ascii=False))


if __name__ == '__main__':
    main()
