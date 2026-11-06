"""Consume the verdicts a drill records, and fail the process on a real failure.

Several drills write evidence/<name>-<stamp>/results.json with entries such as
{'check': ..., 'pass': bool}, {'ok': bool} or {'result': 'FAIL ...'}, but the scripts used to
end after printing the JSON, so **nothing consumed those verdicts and the exit code was always
0** (found by auditing the A-to-A release candidate, where 12 of 26 scripts had the same hole).

Verdict vocabulary (assessment 2026-10-05, "验收口径"):

* PASS    - the assertion held;
* FAIL    - the assertion was judged and did not hold; only FAIL fails the process;
* SKIP    - the assertion was not exercised (target offline, optional path absent);
* BLOCKED - the assertion could not be exercised for an environmental reason that must be fixed.

A SKIP or BLOCKED entry deliberately carries **no** pass field, so any consumer that sums pass
booleans cannot silently count it as a pass; tally() reports them separately and finish() prints
all four counts. An assertion that merely records a value (a state, a duration, a counter) stays
informational and is never treated as a failure.

Usage at the end of a drill::

    from tests import drill_gate
    counts = drill_gate.tally(checks)
    raise SystemExit(drill_gate.finish('jetstream_ha_drill', checks, evidence))

checks may be a list of dicts or a dict of {label: value-or-dict}.
"""
import json
import sys

PASS, FAIL, SKIP, BLOCKED, INFO = 'PASS', 'FAIL', 'SKIP', 'BLOCKED', 'INFO'
STATUSES = (PASS, FAIL, SKIP, BLOCKED)

FAILURE_PREFIXES = ('fail', 'failed', 'false', 'not ok', 'unexpected')
FAILURE_SUBSTRINGS = ('unexpected success',)


def _entries(checks):
    """Yield (index, label, mapping) for list- or dict-shaped check collections."""
    if isinstance(checks, dict):
        for key, value in checks.items():
            if isinstance(value, dict):
                yield key, str(key), value
            else:
                # A bare bool in a dict-shaped record *is* the verdict (dr_full_restore_drill
                # uses {'label': True/False}); a bare non-bool is only informational.
                yield key, str(key), {'__bare__': True, 'value': value}
        return
    for index, value in enumerate(checks or []):
        if not isinstance(value, dict):
            yield index, '#%d' % index, {'__bare__': True, 'value': value}
            continue
        label = value.get('check') or value.get('case') or value.get('test') or '#%d' % index
        yield index, str(label), value


def _reason(entry):
    """Return a short reason when the entry carries an explicit failure verdict, else None."""
    if entry.get('__bare__'):
        return 'value=False' if entry.get('value') is False else None
    for key in ('pass', 'ok'):
        if entry.get(key) is False:
            return '%s=False' % key
    for key in ('result', 'status', 'verdict'):
        value = entry.get(key)
        if not isinstance(value, str):
            continue
        text = value.strip().lower()
        if text.startswith(FAILURE_PREFIXES) or any(s in text for s in FAILURE_SUBSTRINGS):
            return '%s=%s' % (key, value.strip()[:60])
    return None


def status_of(entry):
    """The explicit verdict of one entry, or INFO when it only carries an informational value."""
    if entry.get('__bare__'):
        value = entry.get('value')
        return PASS if value is True else FAIL if value is False else INFO
    raw = str(entry.get('status') or '').strip().upper()
    if raw in STATUSES:
        return raw
    detail = entry.get('detail')
    if (isinstance(detail, str) and detail.strip().upper().startswith('SKIP')) or (
            isinstance(detail, dict) and 'skipped' in detail):
        return SKIP
    if isinstance(detail, dict) and 'blocked' in detail:
        return BLOCKED
    for key in ('pass', 'ok'):
        if entry.get(key) is False:
            return FAIL
    for key in ('pass', 'ok'):
        if entry.get(key) is True:
            return PASS
    if _reason(entry) is not None:
        return FAIL
    return INFO


def record(name, status=PASS, detail=None, **extra):
    """Build one check entry with an explicit verdict.

    Only PASS and FAIL set the legacy pass boolean; SKIP and BLOCKED do not, so a tally that
    counts pass cannot mistake them for a pass.
    """
    status = str(status).upper()
    if status not in STATUSES:
        raise ValueError('status must be one of %s' % (STATUSES,))
    entry = {'check': name, 'status': status}
    if status == PASS:
        entry['pass'] = True
    elif status == FAIL:
        entry['pass'] = False
    if detail is not None:
        entry['detail'] = detail
    entry.update(extra)
    return entry


def skipped(name, reason, **detail):
    """Assertion not exercised (target offline, optional path absent); never counted as a pass."""
    return record(name, SKIP, {'skipped': reason, **detail})


def blocked(name, reason, **detail):
    """Assertion blocked by an environment that must be fixed before it can be judged."""
    return record(name, BLOCKED, {'blocked': reason, **detail})


def judge(checks):
    """List of (label, reason) for entries that explicitly record a FAIL."""
    failures = []
    for _, label, entry in _entries(checks):
        if status_of(entry) == FAIL:
            failures.append((label, _reason(entry) or 'status=FAIL'))
    return failures


def tally(checks):
    """Counts per verdict plus the legacy passed/failed names used by results.json."""
    counts = {PASS: 0, FAIL: 0, SKIP: 0, BLOCKED: 0, INFO: 0, 'total': 0}
    for _, _, entry in _entries(checks):
        counts['total'] += 1
        counts[status_of(entry)] += 1
    counts.update(passed=counts[PASS], failed=counts[FAIL], skipped=counts[SKIP],
                  blocked=counts[BLOCKED], info=counts[INFO])
    return counts


def finish(name, checks, evidence=None, stream=None, strict=False):
    """Print a summary and return the process exit code (1 when any verdict failed)."""
    stream = sys.stdout if stream is None else stream
    counts = tally(checks)
    failures = judge(checks)
    print('\n[drill-gate] %s: 记录 %d 项 | PASS %d / FAIL %d / SKIP %d / BLOCKED %d / INFO %d'
          % (name, counts['total'], counts['passed'], counts['failed'], counts['skipped'],
             counts['blocked'], counts['info']), file=stream)
    for label, reason in failures:
        print('  FAIL %s: %s' % (label, reason), file=stream)
    if evidence is not None:
        print('  evidence: %s' % evidence, file=stream)
    failed = bool(failures) or (strict and counts['blocked'] > 0)
    print('[drill-gate] %s' % ('FAILED' if failed else 'OK'), file=stream)
    return 1 if failed else 0


def gate_from_evidence(name, evidence, stream=None):
    """Convenience for drills that only keep results.json around: read it, then gate."""
    record = json.loads((evidence / 'results.json').read_text())
    return finish(name, record.get('checks') or record, evidence, stream)
