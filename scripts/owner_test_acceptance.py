"""Verify a source-bound owner testing receipt; it never grants production admission.

The user may cover all testing roles. This verifies local proof integrity and receipt
completeness, not the person's legal identity or truth of external business assertions.
Production continues to use enterprise_acceptance and real field evidence.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from scripts.enterprise_acceptance import ROOT, source_digest

CHECKS = ('source_release', 'regression', 'live_runtime', 'demo_retirement')


def template():
    return {'format': 1, 'scope': 'owner_test', 'production_ready': False,
            'source_digest': None, 'environment': {'kind': 'local_test', 'site': None},
            'owner': {'name': None, 'role': '测试负责人', 'authorization_quote': None,
                      'accepted_at': None},
            'checks': {key: {'status': 'pending', 'evidence': []} for key in CHECKS}}


def verify(path, root=ROOT, now=None):
    path = Path(path); problems = []; now = now or datetime.now(timezone.utc)
    def named(value): return isinstance(value, str) and 1 <= len(value.strip()) <= 200
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict): raise ValueError('receipt must be an object')
    except (OSError, ValueError) as error:
        return {'ok': False, 'test_ready': False, 'production_ready': False, 'problems': [str(error)]}
    if (type(data.get('format')) is not int or data['format'] != 1 or
            data.get('scope') != 'owner_test' or data.get('production_ready') is not False):
        problems.append('owner_test scope with production_ready=false required')
    if data.get('source_digest') != source_digest(root): problems.append('source digest missing or stale')
    env = data.get('environment')
    if not isinstance(env, dict) or env.get('kind') != 'local_test' or not named(env.get('site')):
        problems.append('named local_test environment required')
    owner = data.get('owner')
    if (not isinstance(owner, dict) or owner.get('role') != '测试负责人' or
            not named(owner.get('name')) or not named(owner.get('authorization_quote'))):
        problems.append('named testing owner and original user authorization required')
    try:
        accepted = datetime.fromisoformat(owner['accepted_at'].replace('Z', '+00:00'))
        if accepted.tzinfo is None or not 0 <= (now - accepted).total_seconds() <= 30 * 86400:
            raise ValueError('stale/future testing receipt')
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        problems.append('fresh timezone-aware owner testing receipt time required')
    rows = data.get('checks')
    if not isinstance(rows, dict): rows = {}
    for key in CHECKS:
        row = rows.get(key)
        if not isinstance(row, dict) or row.get('status') != 'passed':
            problems.append(key + ': passing local check required'); continue
        proofs = row.get('evidence')
        if not isinstance(proofs, list) or not proofs:
            problems.append(key + ': evidence required'); continue
        for entry in proofs:
            try:
                if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
                    raise ValueError('invalid descriptor')
                relative = Path(entry['path']); proof = path.parent / relative
                if (relative.is_absolute() or '..' in relative.parts or proof.is_symlink() or
                        not proof.resolve().is_relative_to(path.parent.resolve())):
                    raise ValueError('proof must stay within receipt directory')
                if hashlib.sha256(proof.read_bytes()).hexdigest() != entry.get('sha256'):
                    raise ValueError('proof checksum mismatch')
            except (OSError, TypeError, ValueError) as error:
                problems.append(key + ': ' + str(error))
    return {'ok': not problems, 'test_ready': not problems, 'production_ready': False,
            'scope': 'owner_test', 'problems': problems,
            'note': 'Local proof integrity only; no production admission or independent signature attestation.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--template', type=Path)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    if args.template:
        with args.template.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(template(), ensure_ascii=False, indent=2) + '\n')
        return 0
    if not args.manifest: parser.error('--manifest or --template required')
    result = verify(args.manifest, args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__': raise SystemExit(main())
