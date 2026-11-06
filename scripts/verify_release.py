"""Verify a release: manifest present, SBOM/manifest checksums valid, baseline docs present."""
import argparse
import json
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def release_metadata_commit(root, released, current):
    """True when the only change from the released baseline to HEAD is release metadata.

    Generating the manifest itself creates a new commit, so requiring HEAD == released would
    never hold right after packaging. A code baseline plus a content digest is the release
    definition; a commit that only touched release/ or offline-bundle/ preserves that baseline.
    """
    if not released or not current:
        return False
    try:
        result = subprocess.run(['git', '-C', str(root), 'diff', '--name-only',
                                 released + '..' + current],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    paths = [path for path in result.stdout.split() if path.strip()]
    return all(path.startswith('release/') or path.startswith('offline-bundle/') for path in paths)


def current_commit(root=ROOT):
    """Short HEAD of the working tree, or None outside a git checkout."""
    try:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--short', 'HEAD'],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def verify(root=ROOT):
    root = Path(root)
    problems = []
    release_path = root / 'release/RELEASE.json'
    if not release_path.exists():
        return {'ok': False, 'problems': ['missing release/RELEASE.json']}
    try:
        release = json.loads(release_path.read_text())
    except (OSError, ValueError):
        return {'ok': False, 'problems': ['release manifest is malformed or unreadable']}
    if not isinstance(release, dict):
        return {'ok': False, 'problems': ['release manifest must be an object']}
    for field in ['product', 'version', 'baseline', 'evidence_levels', 'git_commit']:
        if not release.get(field):
            problems.append('release manifest missing ' + field)
    for field in ('product', 'version', 'baseline', 'git_commit'):
        value = release.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            problems.append('release manifest invalid ' + field)
    if release.get('git_commit') is not None and not isinstance(release['git_commit'], str):
        release['git_commit'] = None
    if isinstance(release.get('git_commit'), str) and not re.fullmatch(r'[0-9a-f]{7,40}', release['git_commit']):
        problems.append('release manifest invalid git_commit')
        release['git_commit'] = None
    levels = release.get('evidence_levels')
    if not isinstance(levels, list) or not levels or any(level not in ('设计','已实现','隔离验证','实机验证','现场验收') for level in levels):
        problems.append('release manifest invalid evidence_levels')
    for doc, key in [('docs/BASELINE.md', 'baseline'),
                     ('docs/ACCEPTANCE_MATRIX.md', 'acceptance_matrix'),
                     ('docs/REMAINING_WORK.md', 'remaining_work')]:
        if not (root / doc).exists():
            problems.append('missing ' + doc)
        elif release.get(key) and release[key] != doc:
            problems.append('%s does not match release manifest (%s)' % (doc, release.get(key)))
    try:
        from scripts import offline_bundle, sbom
        manifest_path = root / 'offline-bundle/manifest.json'
        if not manifest_path.is_file():
            problems.append('missing offline-bundle/manifest.json')
        else:
            try:
                manifest_doc = json.loads(manifest_path.read_text())
            except ValueError:
                problems.append('artifact manifest is malformed')
            else:
                if not offline_bundle.valid_artifact_metadata(manifest_doc):
                    problems.append('artifact manifest is malformed')
                elif not offline_bundle.verify(manifest_doc, root):
                    problems.append('offline bundle manifest does not match the NATS binary')
        sbom_path = root / 'offline-bundle/sbom.json'
        if not sbom_path.is_file():
            problems.append('missing offline-bundle/sbom.json')
        else:
            try:
                sbom_doc = json.loads(sbom_path.read_text())
            except ValueError:
                problems.append('SBOM is malformed')
            else:
                if not offline_bundle.valid_artifact_metadata(sbom_doc):
                    problems.append('SBOM is malformed')
                elif not sbom.verify(sbom_doc, root):
                    problems.append('SBOM does not match the NATS binary')
    except (OSError, ValueError) as error:
        problems.append('artifact check failed: ' + str(error))
    # The manifest commits the *released* artifact; the working tree may be ahead.
    # Report the drift explicitly instead of letting an old manifest stand for HEAD.
    current = current_commit(root)
    released = release.get('git_commit')
    matches = bool(current and released and (current == released or released.startswith(current) or current.startswith(released)))
    baseline_only = False
    if not matches:
        baseline_only = release_metadata_commit(root, released, current)
        matches = baseline_only
    # Content-level check: the manifest also commits per-file hashes, so drift is visible
    # even when the commit id matches (for example a manifest built from a dirty tree).
    content = None
    try:
        from scripts.source_integrity import hashes
        expected = release.get('source_sha256')
        valid_hashes = (isinstance(expected, dict) and bool(expected)
                        and all(isinstance(name, str) and name
                                and not Path(name).is_absolute() and '..' not in Path(name).parts
                                and isinstance(checksum, str)
                                and re.fullmatch(r'[0-9a-f]{64}', checksum)
                                for name, checksum in expected.items()))
        if not valid_hashes:
            problems.append('release source content hashes are missing or malformed')
        else:
            actual = hashes(root)
            changed = sorted(name for name in set(expected) | set(actual)
                             if expected.get(name) != actual.get(name))
            content = not changed
            if changed:
                problems.append('source content differs from the manifest (%d file(s), e.g. %s)'
                                % (len(changed), ', '.join(changed[:3])))
    except (OSError, ValueError) as error:
        problems.append('source integrity check failed: ' + str(error))
    status = 'release matches source' if matches else 'source is ahead of the released manifest; re-package before release'
    if content is False: status = 'source content differs from the released manifest; re-package before release'
    # Installed archives/container images deliberately omit .git. Their current
    # source can still be verified against every committed content hash.
    if current is None and not (root / '.git').exists() and content is True:
        matches = True
        status = 'release source hashes match; Git metadata unavailable'
    # Public-source gate: the same rules CI runs. A release must not go out while a runtime
    # artifact, credential file or private key is tracked, so a hit is a hard problem rather
    # than a reported drift. Skipped outside a git checkout (nothing to publish).
    public_ok = None
    if (root / '.git').exists():
        try:
            from scripts import check_public
            bad = check_public.problems(check_public.tracked(root), root=root)
            public_ok = not bad
            if bad:
                problems.append('public source check failed (%d file(s), e.g. %s)'
                                % (len(bad), '; '.join(bad[:2])))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            public_ok = None
            problems.append('public source check unavailable: ' + str(error))
    return {'ok': not problems, 'version': release.get('version'), 'problems': problems,
            'released_commit': released, 'current_commit': current, 'baseline_only_drift': baseline_only,
            'git_metadata_available': current is not None,
            'source_matches_release': matches, 'source_content_matches': content,
            'public_source_ok': public_ok, 'release_status': status}


TOLERATED_WITHOUT_BINARY = ('offline bundle manifest does not match the NATS binary',
                            'SBOM does not match the NATS binary')


def gate_problems(report, root=ROOT):
    """CI gate: tolerate artifact mismatches only when there is no binary to verify against.

    A fresh clone has no NATS binary; a *malformed* artifact JSON is a real defect and blocks.
    """
    binary_present = (Path(root) / '.runtime/bin/nats-server').exists()
    hard = []
    for problem in report.get('problems', []):
        if not binary_present and problem in TOLERATED_WITHOUT_BINARY:
            continue
        hard.append(problem)
    if report.get('source_content_matches') is not True:
        # A missing source_sha256 (or a skipped integrity check) must block: otherwise deleting
        # the hashes and editing source passes the gate.
        hard.append('source content hashes are missing or do not match the release manifest')
    return hard


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--strict', action='store_true',
                        help='treat commit/content drift as failure, not just a report')
    parser.add_argument('--gate', action='store_true',
                        help='CI gate: block on source-content drift or hard metadata problems; '
                             'a fresh clone missing the NATS binary is tolerated')
    parser.add_argument('--production', action='store_true', help='also require current field evidence and named sign-offs')
    parser.add_argument('--acceptance-manifest', type=Path)
    args = parser.parse_args()
    report = verify(args.root)
    if args.production:
        from scripts.enterprise_acceptance import verify as verify_acceptance
        acceptance = verify_acceptance(args.acceptance_manifest, args.root) if args.acceptance_manifest else {'ok':False, 'problems':['production acceptance manifest is required']}
        report['production_acceptance'] = acceptance
        drift = not report.get('source_matches_release') or report.get('source_content_matches') is not True
        # CI checks source/evidence without an installed binary; actual host admission
        # omits --gate and also requires the installed artifact to match.
        source_ok = not gate_problems(report, args.root) if args.gate else report['ok']
        ready = bool(source_ok and not drift and acceptance['ok'])
        report['source_release_ok'] = report['ok']
        report['production_ready'] = report['ok'] = ready
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if ready else 1
    if args.gate:
        report['source_gate_ok'] = not gate_problems(report, args.root)
        report['artifact_checks_deferred'] = [problem for problem in report.get('problems', [])
                                             if problem in TOLERATED_WITHOUT_BINARY
                                             and not (args.root / '.runtime/bin/nats-server').exists()]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.gate:
        return 0 if report['source_gate_ok'] else 1
    drift = not report.get('source_matches_release') or report.get('source_content_matches') is not True
    return 0 if report['ok'] and (not args.strict or not drift) else 1


if __name__ == '__main__':
    raise SystemExit(main())
