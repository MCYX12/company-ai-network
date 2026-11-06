"""Employment-status provider.

A manual local roster (roster.sqlite3) overlays the account members' state so renewal and
offboarding can query one interface. The optional OIDC provider verifies an enterprise token
(Azure AD/Entra, Okta, Keycloak, ...) and writes its employment_status into the same roster,
so every existing caller keeps using provider().status(employee_id) unchanged.
"""
import json
import os
import sqlite3
import time
from pathlib import Path
from .common import RUNTIME


def authority_base():
    """Authorization/identity authority: the shared directory in HA, else the local runtime."""
    shared = os.environ.get('POC_AUTHZ_RUNTIME')
    return Path(shared) if shared else RUNTIME

ACTIVE = {'active', 'unknown'}
BLOCKED = {'suspended', 'left'}
MEMBER_MAP = {'active': 'active', 'disabled': 'suspended', 'rejected': 'left', 'provisioning': 'unknown'}


class RosterStore:
    def __init__(self, path=None):
        self.path = path or authority_base() / 'roster.sqlite3'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS employment(employee_id TEXT PRIMARY KEY,status TEXT NOT NULL,source TEXT,updated_at REAL)')

    def db(self):
        return sqlite3.connect(self.path, timeout=5)

    def _row(self, employee_id):
        with self.db() as db:
            row = db.execute('SELECT status,source,updated_at FROM employment WHERE employee_id=?',
                             (employee_id,)).fetchone()
        return {'status': row[0], 'source': row[1], 'updated_at': row[2]} if row else None

    def set(self, employee_id, status, source='manual', observed_at=None):
        """Write a status unless a manual revocation or an older event must win.

        - A manual 'offboard' record is sticky: NO identity sync may change its source or state,
          not even a same-status left token (otherwise a later active token would slip through).
        - observed_at (e.g. the token iat) must not be older than the stored event time.
        - The read, priority decision and write run in one BEGIN IMMEDIATE transaction, so two
          writers cannot both pass the check and then overwrite each other.
        """
        if status not in {'active', 'suspended', 'left', 'unknown'}:
            raise ValueError('Invalid employment status')
        when = observed_at if observed_at is not None else time.time()
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT status,source,updated_at FROM employment WHERE employee_id=?',
                                     (employee_id,)).fetchone()
            existing = {'status': row[0], 'source': row[1], 'updated_at': row[2]} if row else None
            if existing and existing.get('source') == 'offboard' and source != 'offboard':
                connection.execute('ROLLBACK')
                return False
            if existing and observed_at is not None and existing.get('updated_at') and observed_at < existing['updated_at']:
                connection.execute('ROLLBACK')
                return False
            connection.execute('INSERT INTO employment VALUES(?,?,?,?) ON CONFLICT(employee_id) DO UPDATE SET status=excluded.status,source=excluded.source,updated_at=excluded.updated_at',
                               (employee_id, status, source, when))
            connection.execute('COMMIT')
            return True
        except Exception:
            try:
                connection.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def status(self, employee_id):
        with self.db() as db:
            row = db.execute('SELECT status FROM employment WHERE employee_id=?', (employee_id,)).fetchone()
        return row[0] if row else 'unknown'

    def listing(self):
        with self.db() as db:
            return [dict(zip(['employee_id', 'status', 'source', 'updated_at'], row))
                    for row in db.execute('SELECT employee_id,status,source,updated_at FROM employment ORDER BY updated_at DESC')]


class AccountsProvider:
    """Maps the existing account members state to employment status."""

    def __init__(self, path=None):
        self.path = path

    def status(self, employee_id):
        database = self.path or (authority_base() / 'accounts.sqlite3')
        if not database.exists():
            return 'unknown'
        try:
            with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True) as db:
                row = db.execute('SELECT state FROM members WHERE id=?', (employee_id,)).fetchone()
        except sqlite3.Error:
            return 'unknown'
        return MEMBER_MAP.get(row[0], 'unknown') if row else 'unknown'


class LocalProvider:
    """Manual roster overlays account state; unknown stays unknown (does not block)."""

    def __init__(self, roster=None, accounts=None):
        self.roster = roster or RosterStore()
        self.accounts = accounts or AccountsProvider()

    def status(self, employee_id):
        manual = self.roster.status(employee_id)
        return self.accounts.status(employee_id) if manual == 'unknown' else manual


class OidcProvider(LocalProvider):
    """Verify an OIDC token, then persist its employment status into the roster.

    Keeps the read interface identical: callers still ask status(employee_id). The IdP is
    configured with POC_OIDC_JWKS (a JWKS JSON file), POC_OIDC_ISSUER and POC_OIDC_AUDIENCE.
    """

    def __init__(self, jwks_path=None, issuer=None, audience=None, jwks_url=None,
                 subject_map=None, roster=None, accounts=None, jwks_ttl=300,
                 subject_claim=None, status_claim=None):
        super().__init__(roster=roster, accounts=accounts)
        self.jwks_path = jwks_path or os.environ.get('POC_OIDC_JWKS')
        self.jwks_url = jwks_url or os.environ.get('POC_OIDC_JWKS_URL')
        self.issuer = issuer or os.environ.get('POC_OIDC_ISSUER')
        self.audience = audience or os.environ.get('POC_OIDC_AUDIENCE')
        self.subject_map = subject_map
        self.jwks_ttl = jwks_ttl
        self.subject_claim = subject_claim or os.environ.get('POC_OIDC_SUBJECT_CLAIM') or 'sub'
        self.status_claim = status_claim or os.environ.get('POC_OIDC_STATUS_CLAIM') or 'employment_status'
        self._jwks = None
        self._jwks_expires = 0.0

    @classmethod
    def from_environment(cls, **kwargs):
        mapping = None
        path = os.environ.get('POC_OIDC_SUBJECT_MAP')
        if path and kwargs.get('subject_map') is None:
            with open(path, encoding='utf-8') as stream:
                mapping = json.load(stream)
        kwargs.setdefault('subject_map', mapping)
        return cls(**kwargs)

    def _load_jwks(self, force=False):
        from . import oidc
        if not force and self._jwks and time.time() < self._jwks_expires:
            return self._jwks
        if self.jwks_url:
            self._jwks = oidc.fetch_jwks(self.jwks_url)
        elif self.jwks_path:
            with open(self.jwks_path, encoding='utf-8') as stream:
                self._jwks = json.load(stream)
        else:
            raise ValueError('POC_OIDC_JWKS or POC_OIDC_JWKS_URL is required')
        self._jwks_expires = time.time() + self.jwks_ttl
        return self._jwks

    def verify(self, token, now=None):
        return self._claims(token, now)

    def _claims(self, token, now):
        from . import oidc
        jwks = self._load_jwks()
        try:
            return oidc.verify_jwt(token, jwks, self.issuer, self.audience, now=now)
        except oidc.TokenError as error:
            # A new kid after a rotation: refetch once, then fail closed.
            if self.jwks_url and 'no matching JWK' in str(error):
                return oidc.verify_jwt(token, self._load_jwks(force=True), self.issuer, self.audience, now=now)
            raise

    def sync(self, token, now=None):
        from . import oidc
        if not (self.issuer and self.audience):
            raise ValueError('POC_OIDC_ISSUER/POC_OIDC_AUDIENCE are required')
        claims = self._claims(token, now)
        identity = oidc.identity_from_claims(claims, employee_claim=self.subject_claim,
                                             status_claim=self.status_claim)
        subject = identity['employee_id']
        if self.subject_map is not None:
            mapped = self.subject_map.get('%s|%s' % (self.issuer, subject))
            if not mapped:
                raise ValueError('UNKNOWN_SUBJECT')     # never invent a local employee id
            subject = mapped
        status = identity['employment_status']
        if status not in {'active', 'suspended', 'left'}:
            # A standard OIDC token has no employment claim: do not persist 'unknown' over a
            # real state (it would shadow the accounts provider), just report it.
            return {'employee_id': subject, 'status': 'unknown',
                    'groups': identity['groups'], 'synced': False, 'reason': 'NO_EMPLOYMENT_STATUS'}
        observed_at = claims.get('iat')
        if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)):
            observed_at = None
        applied = self.roster.set(subject, status, source='oidc', observed_at=observed_at)
        # Report the ACTUAL effective state so a rejected sync cannot be misread as active.
        return {'employee_id': subject, 'status': status, 'effective_status': self.status(subject),
                'groups': identity['groups'], 'synced': applied}


def provider():
    kind = os.environ.get('POC_IAM_PROVIDER', 'local')
    if kind == 'accounts':
        return AccountsProvider()
    if kind == 'oidc':
        return OidcProvider.from_environment()
    return LocalProvider()


def status(employee_id):
    return provider().status(employee_id)


def is_active(employee_id):
    return status(employee_id) in ACTIVE


def is_blocked(employee_id):
    return status(employee_id) in BLOCKED
