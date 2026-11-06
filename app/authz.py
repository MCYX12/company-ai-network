"""Unified authorization entry (assessment item 4).

One decision and one reason vocabulary across conversation, file, history and model egress.
Call sites can migrate onto this facade without changing the underlying stores.
"""
from pathlib import Path
import time

KINDS = ('conversation', 'file', 'history', 'model_egress')
REASONS = ('OK', 'NO_APPROVAL', 'NO_SCOPE', 'NO_GRANT', 'SCOPE_MISMATCH', 'BUDGET_EXCEEDED', 'PROJECT_REQUIRED', 'UNKNOWN_KIND', 'EGRESS_NOT_APPROVED')


def _base(runtime=None):
    # A shared authority always wins over a caller's explicit local path: in HA the Router must
    # read the same approvals every other Router reads (assessment 2026-10-08).
    import os
    shared = os.environ.get('POC_AUTHZ_RUNTIME')
    if shared:
        return Path(shared)
    if runtime:
        return Path(runtime)
    from .common import authority_base
    return authority_base()


def decide(actor, target, kind, project=None, projects=None, runtime=None, now=None,
           require_conversation=True, scope_id=None):
    now = time.time() if now is None else now
    if kind not in KINDS:
        return {'allowed': False, 'reason': 'UNKNOWN_KIND', 'source': 'authz'}
    base = _base(runtime)
    if kind == 'model_egress' and not require_conversation:
        # The worker already passed the Router's conversation check; here only the egress budget applies.
        from .model_budget import ModelBudget
        if not ModelBudget(base / 'model-budget.sqlite3').allow(actor, target, now):
            return {'allowed': False, 'reason': 'BUDGET_EXCEEDED', 'source': 'model_budget'}
        return {'allowed': True, 'reason': 'OK', 'source': 'model_budget'}
    if kind in ('conversation', 'model_egress'):
        from .conversation_access import AccessStore
        scopes = projects if projects is not None else ([project] if project else [])
        allowed = bool(scopes) and AccessStore(base / 'conversation-access.sqlite3').permits(actor, target, scopes)
        if not allowed:
            return {'allowed': False, 'reason': 'NO_APPROVAL', 'source': 'conversation_access'}
        if kind == 'model_egress':
            from .model_budget import ModelBudget
            if not ModelBudget(base / 'model-budget.sqlite3').allow(actor, target, now):
                return {'allowed': False, 'reason': 'BUDGET_EXCEEDED', 'source': 'model_budget'}
        return {'allowed': True, 'reason': 'OK', 'source': 'conversation_access'}
    if kind == 'file':
        if not project:
            return {'allowed': False, 'reason': 'PROJECT_REQUIRED', 'source': 'file_access'}
        from .file_access import FileAccess
        for scope in FileAccess(base / 'file-access.sqlite3').listing():
            if (scope.get('caller') == actor and scope.get('target') == target and scope.get('project') == project
                    and (scope_id is None or scope.get('id') == scope_id)
                    and not scope.get('revoked') and (scope.get('expires') or 0) > now):
                return {'allowed': True, 'reason': 'OK', 'source': 'file_access', 'scope_id': scope.get('id')}
        return {'allowed': False, 'reason': 'NO_SCOPE', 'source': 'file_access'}
    # History is a tri-state administrative veto over device-owner consent, not a hard gate:
    # with no record (or only a lapsed one) the device owner decides. This branch must agree with
    # the execution path (authz.history_scope) or the facade denies what the answer path allows
    # (assessment 2026-10-05).
    scope = project
    if scope is None and isinstance(projects, list) and len(projects) == 1:
        scope = projects[0]
    from .history_access import HistoryAccess
    grants = HistoryAccess(base / 'history-access.sqlite3').grants(actor, target, now + 86400)
    decision = history_scope(actor, target, grants, project=scope, now=now)
    return {'allowed': decision['allowed'], 'reason': decision['reason'], 'source': 'history_access',
            'scopes': decision['scopes'], 'managed': decision['managed']}


def decide_egress(actor, target, project=None, scope_ids=None, runtime=None, now=None, require_conversation=True):
    """Permission to send material to a model: budget plus every file scope's cloud approval.

    A model-call budget check must never stand in for data-egress permission, so
    the file scopes that will actually be uploaded are re-checked here (active,
    owner/target/project match, cloud allowed). Returns one decision and reason.
    """
    now = time.time() if now is None else now
    decision = decide(actor, target, 'model_egress', project=project, runtime=runtime, now=now,
                      require_conversation=require_conversation)
    if not decision['allowed']:
        return decision
    if scope_ids:
        from .file_access import FileAccess
        base = _base(runtime)
        scopes = {s.get('id'): s for s in FileAccess(base / 'file-access.sqlite3').listing()}
        for scope_id in scope_ids:
            scope = scopes.get(scope_id)
            if scope is None:
                return {'allowed': False, 'reason': 'NO_SCOPE', 'source': 'file_access', 'scope_id': scope_id}
            if (scope.get('caller'), scope.get('target'), scope.get('project')) != (actor, target, project):
                return {'allowed': False, 'reason': 'SCOPE_MISMATCH', 'source': 'file_access', 'scope_id': scope_id}
            if scope.get('revoked') or (scope.get('expires') or 0) <= now:
                return {'allowed': False, 'reason': 'NO_SCOPE', 'source': 'file_access', 'scope_id': scope_id}
            if not scope.get('cloud'):
                return {'allowed': False, 'reason': 'EGRESS_NOT_APPROVED', 'source': 'file_access', 'scope_id': scope_id}
    return {'allowed': True, 'reason': 'OK', 'source': decision['source']}


def history_scope(actor, target, grants, project=None, runtime=None, now=None):
    """Administrative veto layered over device-owner consent, one reason vocabulary.

    UNMANAGED (no administrative record) means device consent alone decides, so it
    stays allowed with an empty scope set; only an explicit non-matching grant is
    DENIED. This preserves the tri-state while removing duplicated parsing.
    """
    from .history_access import decision as history_decision, allowed_scopes
    status = history_decision(actor, target, grants, now=now, scope=project)
    scopes = allowed_scopes(grants, actor, target, now=now)
    if status == 'DENIED':
        return {'allowed': False, 'reason': 'SCOPE_MISMATCH' if scopes else 'NO_GRANT',
                'source': 'history_access', 'scopes': scopes, 'managed': True}
    return {'allowed': True, 'reason': 'OK', 'source': 'history_access',
            'scopes': scopes, 'managed': status == 'APPROVED'}
