"""Admin-only identity sync from an enterprise OIDC token (protected entry).

    python -m scripts.oidc_sync --token-file /path/token.jwt --apply

The token must verify against the configured IdP (POC_OIDC_ISSUER/AUDIENCE and
POC_OIDC_JWKS or POC_OIDC_JWKS_URL); the employment status is written to the roster.
Without --apply it only verifies and prints the mapped identity. Unknown subjects (when a
subject map is configured) are rejected, so a token can never invent a local employee.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.common import RUNTIME, authority_base
from app.iam import OidcProvider, RosterStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=RUNTIME)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='write the roster (otherwise verify only)')
    args = parser.parse_args()
    token = args.token_file.read_text().strip()
    provider = OidcProvider.from_environment(roster=RosterStore(authority_base(args.runtime) / 'roster.sqlite3'))
    if args.apply:
        result = provider.sync(token)
    else:
        claims = provider.verify(token)
        result = {'verified': True, 'subject': claims.get(provider.subject_claim),
                  'issuer': claims.get('iss'), 'note': 'dry run; nothing written'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
