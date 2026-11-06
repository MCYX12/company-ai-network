"""Build a per-project fact file from real artifacts (assessment 3.1).

A level only rises when an artifact actually proves it for *this* item:

* test evidence must be a machine-readable record whose result is OK, whose
  covered test ids include every selector the item declares, and which is not
  older than the item's source files;
* acceptance evidence must record an explicit accepted conclusion for this
  item together with the artifact version it signed off.

Bare file existence now proves nothing beyond code_present/planned. A test run
that passed for unrelated code can no longer upgrade an item, and a record that
says FAILED can never produce tests_pass.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import facts

TEST_OK = {'ok', 'pass', 'passed', 'success', 'succeeded'}
ACCEPT_OK = {'accepted', 'approved', 'pass', 'passed', 'ok'}


def current_revision(root=ROOT):
    try:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def load_evidence(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def newest_evidence(root=ROOT, folder='facts/evidence'):
    files = [path for path in (Path(root) / folder).glob('*.json') if path.is_file()]
    return max(files, key=lambda path: path.stat().st_mtime) if files else None


def _as_time(value):
    """Parse a numeric epoch or an ISO-8601 string into a POSIX timestamp."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def source_mtime(paths, root):
    latest = 0.0
    for rel in paths or []:
        path = Path(root) / rel
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    return latest


def source_unchanged(root, revision, paths, executed_at):
    """True when the item's source has not changed since the evidence revision.

    Uses git diff against the recorded revision when possible (works across the
    later evidence/fact commits), and falls back to file mtimes outside a repo.
    """
    paths = [str(p) for p in (paths or [])]
    if revision:
        try:
            command = ['git', '-C', str(root), 'diff', '--quiet', str(revision)]
            if paths:
                command += ['--', *paths]
            result = subprocess.run(command, capture_output=True, timeout=10)
            if result.returncode == 0:
                return True
            # rc=1 with no stderr is a real difference; a bad revision also exits 1
            # but prints "Could not access", so only trust a silent difference.
            if result.returncode == 1 and not result.stderr.strip():
                return False
        except (OSError, subprocess.SubprocessError):
            pass
    if executed_at is None:
        return False
    return executed_at >= source_mtime(paths, root)


def ran_anything(data):
    """False when the record says nothing ran (an all-skipped run proves nothing)."""
    ran = data.get('ran')
    if isinstance(ran, bool) or not isinstance(ran, (int, float)):
        return True
    return ran > 0


def selectors_skipped(data, selectors):
    """True when the record says the declared selectors were skipped rather than executed.

    refresh_facts records the run-level skipped count; a record may also list the skipped ids.
    An all-skipped run (skipped >= ran) can never prove tests_pass.
    """
    skipped = data.get('skipped_selectors') or data.get('skipped_tests')
    if not isinstance(skipped, list):
        skipped = data.get('skipped')
    if isinstance(skipped, list):
        return any(any(str(selector) in str(entry) for entry in skipped) for selector in selectors)
    count, ran = data.get('skipped'), data.get('ran')
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return False
    if isinstance(ran, bool) or not isinstance(ran, (int, float)):
        return count > 0 and not (data.get('covered') or [])
    return ran - count <= 0


def paths_digest(paths, root):
    """Content digest of an item's source paths: the code state a fact was computed against."""
    digest = hashlib.sha256()
    for rel in sorted(str(path) for path in (paths or [])):
        digest.update(rel.encode())
        try:
            digest.update((Path(root) / rel).read_bytes())
        except OSError:
            digest.update(b'<missing>')
    return digest.hexdigest()


def evidence_digest(entries):
    """Digest of the evidence records a level was granted from; changes when they change."""
    digest = hashlib.sha256()
    for entry in entries:
        if entry:
            digest.update(json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str).encode())
    return digest.hexdigest()


def test_evidence(item, root, revision, now):
    """Return (path, executed_at) when a test record proves this item, else None."""
    selectors = [str(s) for s in (item.get('tests') or [])]
    explicit = item.get('tests_evidence')
    candidates = []
    if explicit:
        candidates.append(Path(root) / explicit)
    elif item.get('auto_tests_evidence') and selectors:
        folder = Path(root) / 'facts/evidence'
        if folder.is_dir():
            candidates.extend(sorted(folder.glob('*.json'),
                                     key=lambda path: path.stat().st_mtime, reverse=True))
    for path in candidates:
        data = load_evidence(path)
        if not data or str(data.get('kind') or 'test_run') != 'test_run':
            continue
        if str(data.get('result', '')).strip().lower() not in TEST_OK:
            continue
        covered = data.get('covered')
        if not selectors or not isinstance(covered, list):
            continue
        covered = [str(entry) for entry in covered]
        if not all(any(selector in entry for entry in covered) for selector in selectors):
            continue
        executed_at = _as_time(data.get('executed_at')) or _as_time(data.get('date'))
        if executed_at is None:
            continue
        # 证据必须绑定明确的源码版本：缺版本不能沿用"测试通过"。
        evidence_revision = str(data.get('revision') or '').strip()
        if not evidence_revision:
            continue
        # 只有跳过项的运行不能证明通过：整套跳过、或声明的选择器被跳过，都不算。
        if not ran_anything(data) or selectors_skipped(data, selectors):
            continue
        # Compare against the revision the evidence was produced at, so a later
        # source commit (or dirty edit) since that run is detected as stale.
        if not source_unchanged(root, evidence_revision, item.get('code_paths'), executed_at):
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        return rel, executed_at
    return None


def acceptance_evidence(item, root, revision):
    """Return (path, accepted_at) when an acceptance record proves this item."""
    spec = item.get('acceptance')
    if not spec:
        return None
    path = Path(root) / spec
    data = load_evidence(path)
    if not data or str(data.get('kind') or 'acceptance') != 'acceptance':
        return None
    if str(data.get('conclusion', '')).strip().lower() not in ACCEPT_OK:
        return None
    scope = data.get('scope') or data.get('item')
    name = item.get('item')
    # 缺范围（无法与事项对上）不能升级为"业务验收通过"。
    if not scope or not name or not (str(scope) in name or name in str(scope)):
        return None
    accepted_at = (_as_time(data.get('accepted_at')) or _as_time(data.get('date'))
                   or _as_time(data.get('recorded_at')))
    if accepted_at is None:
        return None
    # 缺版本同样不能升级：验收必须绑定它所签署的发布件版本。
    version = str(data.get('artifact_version') or data.get('revision') or '').strip()
    if not version:
        return None
    if not source_unchanged(root, version, item.get('code_paths'), accepted_at):
        return None
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)
    return rel, accepted_at


def compute_item(item, root=ROOT, revision=None, now=None):
    now = time.time() if now is None else now
    root = Path(root)
    if revision is None:
        revision = current_revision(root)
    code = list(item.get('code_paths') or [])
    code_present = bool(code) and all((root / path).exists() for path in code)
    planned_in = item.get('planned_in')
    planned = bool(planned_in) and (root / planned_in).exists()

    test_hit = test_evidence(item, root, revision, now)
    accept_hit = acceptance_evidence(item, root, revision)
    tests_pass = test_hit is not None
    accepted = accept_hit is not None

    levels = {'planned': planned, 'claimed_done': bool(item.get('docs_claim')),
              'code_present': code_present, 'tests_pass': tests_pass, 'accepted': accepted}
    evidence = code[:]
    executed_at = None
    if test_hit:
        evidence.append(test_hit[0])
        executed_at = test_hit[1]
    if accept_hit:
        evidence.append(accept_hit[0])
        if accept_hit[1] is not None:
            executed_at = accept_hit[1] if executed_at is None else max(executed_at, accept_hit[1])
    evidence += list(item.get('evidence') or [])
    record = facts.fact(item['item'], levels, source=item.get('source', ''), evidence=evidence,
                        verified_at=executed_at, limits=item.get('limits', ''),
                        evidence_executed_at=executed_at, evidence_read_at=now, generated_at=now)
    # 绑定源码与证据摘要：源码或证据一变，缓存即可被判为失效，而不是继续沿用旧结论。
    record['code_digest'] = paths_digest(code, root)
    record['evidence_digest'] = evidence_digest([test_hit, accept_hit])
    return record


def build(spec, root=ROOT, revision=None):
    now = time.time()
    if revision is None:
        revision = current_revision(root)
    return {'project_id': spec['project_id'], 'generated_at': now, 'revision': revision,
            'items': [compute_item(item, root, revision, now) for item in spec.get('items', [])]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--revision', default=None)
    parser.add_argument('--explain', action='store_true')
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text(encoding='utf-8'))
    data = build(spec, args.root, args.revision)
    if args.output:
        args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.explain:
        for record in data['items']:
            print(facts.explain(record))
            print()
    if not args.explain:
        print(json.dumps({'project_id': data['project_id'], 'items': len(data['items']),
                          'output': str(args.output) if args.output else None,
                          'revision': data.get('revision')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
