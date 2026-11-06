"""Project summary snapshot state: freshness window and guidance (assessment: waiting/cache)."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time

FRESH_SECONDS = 6 * 3600
# Within a week a record is usable but stale; older than that it is treated as expired.
STALE_SECONDS = 7 * 24 * 3600

# Sidecars sit next to a record: refresh_summaries marks <project>.refresh.json and
# refresh_project_record keeps the observation/confirmation clocks in <project>.meta.json.
# They are not records and must never be parsed as one.
SIDECAR_SUFFIXES = ('.refresh.json', '.meta.json', '.mirror-state.json')


def is_sidecar(name):
    """Sidecars are never records: known suffixes, plus any dotfile the runtime writes."""
    text = str(name)
    return text.startswith('.') or text.endswith(SIDECAR_SUFFIXES)


def meta_record(folder, project):
    """The observation/confirmation sidecar for one project, or an empty mapping."""
    try:
        meta = json.loads((Path(folder) / (project + '.meta.json')).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def confirmation_clock(record, meta=None, now=None):
    """Seconds since a human last asserted the narrative, and which clock supplied it.

    Automation re-deriving facts must never move this clock: confirmed_at is written only by a
    human --confirm. When no sidecar confirmation exists (older deployments or a record published
    before 2026-10-05) it falls back to updated_at, so behaviour is unchanged until the first
    human confirmation is recorded.
    """
    now = time.time() if now is None else now
    raw = (meta or {}).get('confirmed_at') or record.get('updated_at')
    source = 'confirmed_at' if (meta or {}).get('confirmed_at') else 'updated_at'
    try:
        parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, now - parsed.timestamp()), source
    except (ValueError, TypeError):
        return None, source


def snapshot(record, now=None, fresh_seconds=FRESH_SECONDS, stale_seconds=STALE_SECONDS):
    now = time.time() if now is None else now
    raw = record.get('updated_at')
    try:
        parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = max(0.0, now - parsed.timestamp())
    except (ValueError, TypeError):
        return {'snapshot_at': raw, 'age_seconds': None, 'freshness': 'unknown', 'state': 'ready',
                'stale_after_seconds': stale_seconds, 'note': '缺少可用快照时间，无法判断新鲜度'}
    if age <= fresh_seconds:
        freshness, note = 'current', '资料在新鲜窗口内'
    elif age <= stale_seconds:
        freshness, note = 'stale', '资料可能已过期，回答请注明快照时间'
    else:
        freshness, note = 'expired', '资料已过期，回答必须明确说明并建议刷新'
    return {'snapshot_at': raw, 'age_seconds': age, 'freshness': freshness, 'state': 'ready',
            'stale_after_seconds': stale_seconds, 'note': note}


def statuses(runtime=None):
    """Per-project snapshot/freshness plus whether a background refresh is due (control-panel view)."""
    if runtime:
        base = Path(runtime)
    else:
        from .common import RUNTIME
        base = RUNTIME
    folder = Path(base) / 'project-records'
    rows = []
    if not folder.is_dir():
        return rows
    for path in sorted(folder.glob('*.json')):
        if is_sidecar(path.name):
            continue
        try:
            from .project_retirement import retired
            if retired(base,path.stem):
                continue
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            rows.append({'project': path.stem, 'snapshot_at': None, 'age_seconds': None,
                         'freshness': 'invalid', 'note': '记录无法解析', 'refresh_due': True,
                         'narrative_clock': None, 'narrative_age_seconds': None})
            continue
        shot = snapshot(record)
        meta = meta_record(folder, path.stem)
        narrative_age, narrative_clock = confirmation_clock(record, meta)
        refresh_due = shot['freshness'] in ('stale', 'expired', 'unknown')
        sidecar = folder / (path.stem + '.refresh.json')
        if sidecar.exists():
            try:
                refresh_due = bool(json.loads(sidecar.read_text(encoding='utf-8')).get('refresh_due', refresh_due))
            except (OSError, ValueError):
                pass
        rows.append({'project': path.stem, 'snapshot_at': shot['snapshot_at'], 'age_seconds': shot['age_seconds'],
                     'freshness': shot['freshness'], 'note': shot['note'], 'refresh_due': refresh_due,
                     'narrative_clock': narrative_clock,
                     'narrative_age_seconds': None if narrative_age is None else round(narrative_age, 1)})
    return rows
