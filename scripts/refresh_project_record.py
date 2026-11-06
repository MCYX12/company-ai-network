"""Re-derive the published project record from real sources and publish only when it changed.

Why this exists: updated_at is the human owner's claim "this is the state as of T", and the
platform downgrades answers (stale / expired) rather than presenting an old snapshot as
current. A timer that merely re-stamped the timestamp would fake that claim, so this script
re-derives the *machine-checkable* parts from real sources (git HEAD/tag, the live directory
count from the health report, the newest drill evidence) and publishes with a new timestamp
**only when those derived facts differ from what is already published**. Nothing changed -> it
leaves the record alone, exactly so it can go stale and the health check can call for a human
refresh of the narrative.

Three clocks are kept apart (assessment 2026-10-05, "事实新鲜度"):

* observed_at  - when the machine facts were last (re)read; recorded in a sidecar every run;
* confirmed_at - when a human last re-asserted the narrative (--confirm); only a person moves it;
* updated_at   - when the published record itself last changed.

Change detection compares a *stable* projection that drops every clock and normalises the
timestamp embedded in the [自动快照] line, so re-running in a later minute with unchanged facts
compares equal and never re-stamps the record. A human confirmation is persisted in the sidecar
<project>.meta.json (and in the readable completed list) so a later automatic refresh can neither
lose it nor pass it off as fresh machine facts.

Usage:
    python -m scripts.refresh_project_record --source release/project-records/company-ai-network.json \
        --publish .runtime-lan [--publish .runtime] [--dry-run]
    python -m scripts.refresh_project_record --source ... --publish .runtime-lan --confirm "状态未变"
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.project_records import validate

AUTO_PREFIX = '[自动快照] '
CONFIRM_PREFIX = '[人工确认] '
CONFIRM_KEEP = 3
META_SUFFIX = '.meta.json'
META_SCHEMA = 1

# The auto snapshot line carries an observation clock for humans. Change detection must ignore
# that clock, otherwise every refresh in a later minute looks like a change and re-stamps
# updated_at, silently keeping unverified narrative looking fresh ("伪更新").
_AUTO_STAMP = re.compile(r'^(\[自动快照\] )\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z')
_META_KEYS = ('observed_at', 'facts_digest', 'confirmed_at', 'confirmed_note',
              'confirmed_facts_digest', 'published_at', 'schema_version')


def _git(*args):
    try:
        result = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def _counts(payload, checks):
    """(passed, total) that never counts a SKIP/BLOCKED entry as a pass.

    A results.json written by the current drills carries explicit status on every check plus
    passed/skipped/blocked tallies; an older file may only have pass booleans and a detail that
    says SKIP. Either way a skipped assertion is not a pass.
    """
    passed = 0
    total = 0
    for check in checks or []:
        if not isinstance(check, dict):
            continue
        total += 1
        status = str(check.get('status') or '').upper()
        if status:
            passed += 1 if status == 'PASS' else 0
            continue
        detail = check.get('detail')
        skipped = (isinstance(detail, str) and detail.upper().startswith('SKIP')) or (
            isinstance(detail, dict) and 'skipped' in detail)
        passed += 1 if (check.get('pass') and not skipped) else 0
    if total:
        return passed, total
    return int(payload.get('passed') or 0), int(payload.get('total') or 0)


def collect_facts(evidence_root=None, health_dir=None):
    """Everything the snapshot may assert: only values read from real files or git."""
    evidence_root = Path(evidence_root or ROOT / 'evidence')
    health = Path(health_dir or ROOT / '.runtime-lan') / 'health/latest.json'
    facts = {'head': _git('rev-parse', '--short', 'HEAD'),
             'tag': _git('describe', '--tags', '--abbrev=0'),
             'dirty': bool(_git('status', '--porcelain')),
             'online': None, 'visible': None, 'evidence': []}
    try:
        for check in json.loads(health.read_text()).get('checks', []):
            if check.get('check') == 'authorized_directory' and isinstance(check.get('detail'), dict):
                facts['online'] = check['detail'].get('online')
                facts['visible'] = check['detail'].get('visible')
    except (OSError, ValueError, TypeError):
        pass
    found = []
    for results in evidence_root.glob('*/results.json'):
        try:
            payload = json.loads(results.read_text())
        except (OSError, ValueError):
            continue
        # drill evidence comes in two shapes: a report dict, or a bare list of checks
        if isinstance(payload, dict):
            checks = payload.get('checks') or []
            passed, total = _counts(payload, checks)
        elif isinstance(payload, list):
            passed, total = _counts({}, payload)
        else:
            continue
        found.append((results.stat().st_mtime, results.parent.name, passed, total))
    for _mtime, name, passed, total in sorted(found, reverse=True)[:3]:
        facts['evidence'].append({'name': name, 'passed': passed, 'total': total})
    return facts


def facts_digest(facts):
    """Stable identity of the machine facts: re-derived values only, never a clock."""
    payload = {key: facts.get(key) for key in ('head', 'tag', 'dirty', 'online', 'visible')}
    payload['evidence'] = [{'name': e.get('name'), 'passed': e.get('passed'), 'total': e.get('total')}
                           for e in facts.get('evidence', [])]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _normalise_line(line):
    """Drop the observation clock from an auto line so a later run with equal facts matches."""
    return _AUTO_STAMP.sub(r'\1<observed>', line) if isinstance(line, str) else line


def stable(record):
    """Canonical projection used for change detection: every clock removed.

    updated_at is the publish clock and the auto snapshot line embeds the observation clock;
    both are excluded so that "did the facts change" is independent of "when did we look".
    """
    copy = json.loads(json.dumps(record))
    copy.pop('updated_at', None)
    if isinstance(copy.get('completed'), list):
        copy['completed'] = [_normalise_line(item) for item in copy['completed']]
    return json.dumps(copy, ensure_ascii=False, sort_keys=True)


def comparable(record):
    """The published record minus the clocks: what change detection compares."""
    return stable(record)


def auto_line(facts, now):
    parts = []
    if facts.get('head'):
        parts.append(f'HEAD={facts["head"]}' + ('（工作树有未提交改动）' if facts.get('dirty') else ''))
    if facts.get('tag'):
        parts.append(f'最新 tag {facts["tag"]}')
    if facts.get('online') is not None:
        parts.append(f'在线 {facts["online"]}/{facts["visible"]}（health/latest.json）')
    scored = [e['name'] + (f'（{e["passed"]}/{e["total"]}）' if e.get('total') else '') for e in facts.get('evidence', [])]
    if scored:
        parts.append('最新证据 ' + '、'.join(scored))
    return AUTO_PREFIX + now.strftime('%Y-%m-%dT%H:%MZ') + ('：' + '、'.join(parts) if parts else '')


def _split_completed(items):
    """(human entries, confirmation lines); both automatic and confirmation clocks are dropped."""
    humans, confirms = [], []
    for item in items or []:
        if not isinstance(item, str):
            continue
        if item.startswith(CONFIRM_PREFIX):
            confirms.append(item)
        elif item.startswith(AUTO_PREFIX):
            continue
        else:
            humans.append(item)
    return humans, confirms


def _dedupe(lines):
    seen, out = set(), []
    for line in lines or []:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def derive(source, facts, now=None, confirmations=None):
    """Human-authored fields are preserved; the auto snapshot line and evidence tail are rewritten.

    confirmations lets a caller carry forward confirmation lines recorded in the previously
    published record (or its sidecar), so an automatic refresh never drops a human statement that
    exists only in the runtime record and not in the publish source.
    """
    now = now or datetime.now(timezone.utc)
    record = json.loads(json.dumps(source))  # deep copy
    humans, source_confirms = _split_completed(record.get('completed'))
    confirms = _dedupe(source_confirms + list(confirmations or []))[-CONFIRM_KEEP:]
    record['completed'] = (humans + confirms + [auto_line(facts, now)])[-30:]
    # evidence keeps human-cited documents plus clean, existing paths for the newest drills
    evidence = [item for item in record['evidence'] if not item.startswith('evidence/')]
    for entry in facts.get('evidence', []):
        for candidate in (f'evidence/{entry["name"]}', f'evidence/{entry["name"]}/results.json'):
            if (ROOT / candidate).exists() and candidate not in evidence:
                evidence.append(candidate)
    record['evidence'] = list(dict.fromkeys(evidence))[-30:]
    return record


def confirm(record, note, now=None):
    """A human re-asserting that the narrative is still true, in one command.

    Only a person may do this: the auto-refresh deliberately refuses to re-stamp an unchanged
    record. The note is recorded verbatim so the published file shows it was a human statement.
    The caller persists the matching confirmed_at in the sidecar; this function only edits the
    readable record.
    """
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 200:
        raise ValueError('a confirmation note of 1..200 characters is required')
    now = now or datetime.now(timezone.utc)
    record = json.loads(json.dumps(record))
    humans, confirms = _split_completed(record.get('completed'))
    auto = [item for item in record.get('completed', [])
            if isinstance(item, str) and item.startswith(AUTO_PREFIX)]
    confirms.append(f'{CONFIRM_PREFIX}{now.strftime("%Y-%m-%dT%H:%MZ")}：状态未变（{note.strip()}）')
    record['completed'] = (humans + confirms[-CONFIRM_KEEP:] + auto[-1:])[-30:]
    return record


def meta_path(folder, project):
    return Path(folder) / (project + META_SUFFIX)


def load_meta(folder, project):
    """Read the sidecar holding the observation/confirmation clocks, or an empty record."""
    try:
        meta = json.loads(meta_path(folder, project).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return {key: meta.get(key) for key in _META_KEYS} | {'schema_version': META_SCHEMA}


def save_meta(folder, project, meta):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({key: meta.get(key) for key in _META_KEYS} | {'schema_version': META_SCHEMA},
                         ensure_ascii=False, indent=2) + '\n'
    os.umask(0o077)
    handle, name = tempfile.mkstemp(prefix='.meta-', dir=folder)
    with os.fdopen(handle, 'w') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(name, 0o600)
    os.replace(name, meta_path(folder, project))
    return meta_path(folder, project)


def publish(record, runtime):
    runtime = Path(runtime)
    from app.project_retirement import retired
    if retired(runtime, record['project_id']):
        raise ValueError('Retired project cannot be republished')
    folder = runtime / 'project-records'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (record['project_id'] + '.json')
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        shutil.copy2(target, target.with_name(target.name + '.bak-' + stamp))
    payload = json.dumps(record, ensure_ascii=False, indent=2) + '\n'
    os.umask(0o077)
    handle, name = tempfile.mkstemp(prefix='.record-', dir=folder)
    with os.fdopen(handle, 'w') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(name, 0o600)
    os.replace(name, target)
    return target, len(payload.encode())


def _relative(path):
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def _previous_record(folder, project):
    try:
        return json.loads((Path(folder) / (project + '.json')).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=ROOT / 'release/project-records/company-ai-network.json')
    parser.add_argument('--publish', type=Path, action='append', default=None,
                        help='runtime directory to publish into (repeatable); nothing is written without it')
    parser.add_argument('--health-runtime', type=Path, default=ROOT / '.runtime-lan',
                        help='runtime whose health/latest.json supplies the live directory count')
    parser.add_argument('--confirm', metavar='NOTE',
                        help='human re-assertion: record it even when the machine facts also changed')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    source = json.loads(args.source.read_text())
    if args.source.name != source['project_id'] + '.json':
        raise ValueError('Source filename must match the project id')
    source['updated_at'] = source.get('updated_at') or datetime.now(timezone.utc).isoformat()
    facts = collect_facts(health_dir=args.health_runtime)
    digest = facts_digest(facts)
    now = datetime.now(timezone.utc)
    if args.confirm is not None and not 1 <= len(args.confirm.strip()) <= 200:
        raise SystemExit('--confirm 需要 1..200 字符的一句话')
    report = {'source': _relative(args.source), 'facts': facts, 'facts_digest': digest,
              'publish': [str(t) for t in (args.publish or [])], 'dry_run': args.dry_run}
    if args.dry_run:
        folder = (Path(args.publish[0]) / 'project-records') if args.publish else (ROOT / '.runtime-lan/project-records')
        previous = _previous_record(folder, source['project_id'])
        candidate = derive(source, facts, now,
                           confirmations=_split_completed((previous or {}).get('completed'))[1])
        report['auto_line'] = candidate['completed'][-1]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    published = 0
    for runtime in (args.publish or []):
        from app.project_retirement import retired
        if retired(runtime, source['project_id']):
            raise ValueError('Retired project cannot be refreshed')
        folder = Path(runtime) / 'project-records'
        previous = _previous_record(folder, source['project_id'])
        meta = load_meta(folder, source['project_id'])
        carried = _split_completed((previous or {}).get('completed'))[1]
        candidate = derive(source, facts, now, confirmations=carried)
        if args.confirm is not None:
            candidate = confirm(candidate, args.confirm, now)
        changed = previous is None or stable(previous) != stable(candidate)
        if not changed and args.confirm is None:
            meta.update(observed_at=now.isoformat(), facts_digest=digest,
                        published_at=(previous or {}).get('updated_at'))
            save_meta(folder, source['project_id'], meta)
            print(f'unchanged: {meta_path(folder, source["project_id"])}'
                  f'（记录内容未变化，保持原 updated_at={previous["updated_at"]}；'
                  f'观测时间已单独记入 sidecar，未重新发布记录）')
            continue
        candidate['updated_at'] = now.isoformat()
        validate(candidate, candidate['project_id'])
        path, size = publish(candidate, runtime)
        meta['observed_at'] = now.isoformat()
        meta['facts_digest'] = digest
        meta['published_at'] = candidate['updated_at']
        if args.confirm is not None:
            meta['confirmed_at'] = now.isoformat()
            meta['confirmed_note'] = args.confirm.strip()
            meta['confirmed_facts_digest'] = digest
        save_meta(folder, source['project_id'], meta)
        published += 1
        label = 'confirmed' if args.confirm is not None else 'published'
        print(f'{label}: {path}（{size} 字节，updated_at={candidate["updated_at"]}）')
    if not published and args.publish:
        print('no target needed an update')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
