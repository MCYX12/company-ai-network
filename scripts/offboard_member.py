"""Administrator member offboarding: employment status -> registry/NATS disable -> optional CA revoke.

Order of operations (assessment 2026-10-06):
1. Block business admission first: write the roster (left/offboard) and the registry/NATS config.
2. Certificate revocation and CRL generation are recoverable stages; a real openssl non-zero exit
   (CalledProcessError) is caught, and an already-revoked certificate counts as achieved.
3. Transport activation is a separate, explicit step; without it the result is
   pending_transport_activation, not complete. A SIGHUP is reported as 'signaled', not 'confirmed'.
The authoritative files are read INSIDE the shared admin lock (no stale plan) and only this
employee's identities are affected even on a shared device.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cryptography import x509
from app.registry import validate_registry
from app.iam import RosterStore
from scripts.revoke_device import atomic

CA_ERRORS = (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired)


def _parse(raw_registry, raw_config):
    registry = json.loads(raw_registry)
    config = json.loads(raw_config)
    rows = validate_registry(registry)
    return registry, config, rows


def _stage_for_offboard(registry, config, identities):
    """Disable ONLY this employee's identities; other employees on a shared device are untouched."""
    registry = json.loads(json.dumps(registry))
    config = json.loads(json.dumps(config))
    users = config['accounts']['POC']['users']
    if any(u.get('user') in {'router', 'server'} for u in users if u.get('user') in identities):
        raise ValueError('Infrastructure identities cannot be revoked here')
    for row in registry['agents']:
        if row['agent_id'] in identities:
            row['enabled'] = False
    config['accounts']['POC']['users'] = [u for u in users if u.get('user') not in identities]
    validate_registry(registry)
    return registry, config


def plan(runtime, employee):
    """Read-only dry run used by --help/-report; apply always re-reads inside the lock."""
    runtime = Path(runtime).resolve()
    registry, config, rows = _parse((runtime / 'registry.json').read_bytes(),
                                    (runtime / 'nats.conf').read_bytes())
    identities = sorted(k for k, v in rows.items() if v['employee_id'] == employee)
    if not identities:
        raise ValueError('Unknown employee; no changes made')
    return runtime, registry, config, identities, sorted({rows[i]['device_id'] for i in identities})


def _certificate_serial(ca_dir, name):
    """Serial number of the identity's CURRENT certificate, or None if unreadable."""
    path = Path(ca_dir) / 'intermediate' / 'certs' / (name + '.crt')
    try:
        return x509.load_pem_x509_certificate(path.read_bytes()).serial_number
    except (OSError, ValueError):
        return None


def _subject_cn(subject):
    for part in str(subject or '').split('/'):
        if part.startswith('CN='):
            return part[3:]
    return str(subject or '')


def _as_int(value):
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return None


def _revoke(ca_tool, ca_dir, name):
    """Revoke EVERY non-revoked certificate for this identity (renewals leave several).

    The CA index subject is '/CN=ag-a' and a renewal leaves old V rows plus the new one, so an
    offboard must revoke all valid serials, not just the current certificate file. Never inspect
    the command path; genuine CA failures stay failures.
    """
    try:
        issued = ca_tool.status(ca_dir).get('issued') or []
    except CA_ERRORS:
        issued = []
    same = [row for row in issued if _subject_cn(row.get('name')) == name]
    valid = [row for row in same if row.get('state') == 'V']
    already = [str(row.get('serial')) for row in same if row.get('state') == 'R' and row.get('serial')]
    if valid:
        revoked_serials = []
        failed_serials = []
        for row in valid:
            try:
                outcome = ca_tool.revoke_serial(ca_dir, row.get('serial'))
                revoked_serials.append(outcome.get('revoked') or row.get('serial'))
            except CA_ERRORS as error:
                failed_serials.append({'serial': row.get('serial'), 'error': type(error).__name__})
        if failed_serials:
            return {'outcome': 'partial', 'revoked_serials': revoked_serials,
                    'failed_serials': failed_serials, 'serials': revoked_serials + already}
        # Every success branch must confirm the CURRENT certificate is revoked, even when old V
        # rows existed: a missing current row means the index lost it, not that we are done.
        rows = ca_tool.status(ca_dir).get('issued') or []
        subject_rows = [row for row in rows if _subject_cn(row.get('name')) == name]
        serial = _certificate_serial(ca_dir, name)
        if serial is not None:
            current = [row for row in subject_rows if _as_int(row.get('serial')) == serial]
            if not current or current[0].get('state') != 'R':
                raise ValueError('CA_INDEX_INCONSISTENT: current certificate for %s is not revoked' % name)
        elif any(row.get('state') == 'V' for row in subject_rows):
            raise ValueError('CA_INDEX_INCONSISTENT: valid material remains for %s' % name)
        return {'outcome': 'revoked', 'revoked_serials': revoked_serials, 'failed_serials': [],
                'serials': [str(row.get('serial')) for row in subject_rows if row.get('serial')]}
    if same:
        # Only old rows with this subject exist: confirm the CURRENT certificate's serial is one
        # of the revoked rows. A same-subject old R row does not prove the current cert is revoked.
        serial = _certificate_serial(ca_dir, name)
        if serial is not None and any(_as_int(value) == serial for value in already):
            return {'outcome': 'already_revoked', 'revoked_serials': [], 'failed_serials': [],
                    'serials': already}
        raise ValueError('CA_INDEX_INCONSISTENT: current certificate for %s is not confirmed revoked' % name)
    # The CA index has no row for this subject: fall back to the certificate file.
    try:
        ca_tool.revoke(ca_dir, name, 'cessationOfOperation')
        serial = _certificate_serial(ca_dir, name)
        return {'outcome': 'revoked', 'revoked_serials': [], 'failed_serials': [],
                'serials': ['%X' % serial] if serial is not None else []}
    except CA_ERRORS as error:
        stderr = str(getattr(error, 'stderr', '') or '').lower()
        if 'already revoked' in stderr or 'already in the crl' in stderr:
            return {'outcome': 'already_revoked', 'revoked_serials': [], 'failed_serials': [], 'serials': []}
        raise


def activate_transport(runtime, nats_pid=None):
    """Send SIGHUP so the config change takes effect. Reported as 'signaled', not confirmed."""
    if nats_pid is None:
        pidfile = Path(runtime) / 'nats.pid'
        if pidfile.exists():
            nats_pid = int(pidfile.read_text().strip())
    if not nats_pid:
        raise ValueError('NATS pid unknown; pass --nats-pid or provide runtime/nats.pid')
    os.kill(int(nats_pid), signal.SIGHUP)
    return int(nats_pid)


def offboard(runtime, employee, reason, apply=False, activate=False, nats_pid=None):
    if not reason.strip() or len(reason) > 500:
        raise ValueError('Reason required (1..500 characters)')
    runtime = Path(runtime).resolve()
    registry_path, config_path = runtime / 'registry.json', runtime / 'nats.conf'
    if any(p.is_symlink() or not p.is_file() for p in (registry_path, config_path)):
        raise ValueError('Explicit regular registry and NATS config required')
    report = {'employee_id': employee, 'identities': [], 'devices': [], 'reason': reason,
              'applied': False, 'transport_activation_required': True,
              'revoked_certificates': [], 'stages': {}}
    if not apply:
        _, _, _, identities, devices = plan(runtime, employee)
        report.update(identities=identities, devices=devices, stages={'plan': 'ok'})
        return report
    with (runtime / '.admin.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        originals = [registry_path.read_bytes(), config_path.read_bytes()]
        registry, config, rows = _parse(*originals)
        identities = sorted(k for k, v in rows.items() if v['employee_id'] == employee)
        if not identities:
            raise ValueError('Unknown employee; no changes made')
        devices = sorted({rows[i]['device_id'] for i in identities})
        report.update(identities=identities, devices=devices)
        staged_registry, staged_config = _stage_for_offboard(registry, config, identities)
        staged = [json.dumps(x, ensure_ascii=False, indent=2).encode()
                  for x in [staged_registry, staged_config]]
        fd, name = tempfile.mkstemp(prefix='.nats-offboard-', dir=runtime)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(staged[1])
            subprocess.run([str(ROOT / '.runtime/bin/nats-server'), '-t', '-c', name],
                           check=True, capture_output=True, timeout=10)
        finally:
            Path(name).unlink(missing_ok=True)
        if any(p.read_bytes() != raw for p, raw in zip([registry_path, config_path], originals)):
            raise ValueError('Configuration changed during validation')
        # 1) block business admission first, so a later CA failure cannot leave the member active
        shared = os.environ.get('POC_AUTHZ_RUNTIME')
        roster = Path(shared) / 'roster.sqlite3' if shared else runtime / 'roster.sqlite3'
        RosterStore(roster).set(employee, 'left', source='offboard')
        report['stages']['roster'] = 'ok'
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = runtime / 'admin-history' / stamp
        backup.mkdir(parents=True, mode=0o700)
        os.chmod(backup.parent, 0o700)
        for path, raw in zip([registry_path, config_path], originals):
            atomic(backup / path.name, raw)
        atomic(backup / 'report.json', json.dumps(report, indent=2).encode())
        for path, data in zip([registry_path, config_path], staged):
            atomic(path, data)
        report['stages']['registry'] = 'ok'
        # 2) certificate + CRL are recoverable stages: already-revoked is success, real errors partial
        marker = runtime / 'certs/ENTERPRISE_CA.json'
        if marker.exists():
            from scripts import ca_tool
            ca_dir = Path(json.loads(marker.read_text())['ca_dir'])
            errors = []
            expected = set()
            # ONE CA transaction: enumerate, revoke every valid serial, re-read, regenerate and
            # verify the CRL. A renewal either completes before (and is seen) or waits for the
            # lock and is then refused by its lock-time employment re-check.
            with ca_tool._ca_lock(ca_dir):
                for name in identities:
                    try:
                        outcome = _revoke(ca_tool, ca_dir, name)
                        if outcome['outcome'] == 'partial':
                            errors.append({'identity': name, 'error': 'CA_REVOKE_PARTIAL',
                                           'detail': str(outcome['failed_serials'])[:160]})
                        report['revoked_certificates'].append({'identity': name, **outcome})
                        for value in outcome.get('serials') or []:
                            number = _as_int(value)
                            if number is not None:
                                expected.add(number)
                    except CA_ERRORS as error:
                        errors.append({'identity': name, 'error': type(error).__name__, 'detail': str(error)[:160]})
                rows = ca_tool.status(ca_dir).get('issued') or []
                leftovers = sorted('%s(%s)' % (row.get('serial'), _subject_cn(row.get('name')))
                                   for row in rows if row.get('state') == 'V'
                                   and _subject_cn(row.get('name')) in identities)
                if leftovers:
                    errors.append({'identity': ','.join(identities), 'error': 'VALID_MATERIAL_REMAINS',
                                   'detail': ','.join(leftovers)[:160]})
                    report['leftover_valid_serials'] = leftovers
                report['stages']['certificate'] = 'failed' if errors else 'ok'
                if errors:
                    report['certificate_errors'] = errors
                try:
                    ca_tool.crl(ca_dir)
                    present = ca_tool.crl_serials(ca_dir)
                    missing = sorted(number for number in expected if number not in present)
                    if missing:
                        report['stages']['crl'] = 'failed'
                        report['crl_error'] = 'CRL missing serials: ' + ','.join('%X' % number for number in missing)
                    else:
                        report['stages']['crl'] = 'ok'
                except CA_ERRORS as error:
                    report['stages']['crl'] = 'failed'
                    report['crl_error'] = '%s: %s' % (type(error).__name__, str(error)[:160])
        else:
            report['stages']['certificate'] = 'not_configured'
            report['stages']['crl'] = 'not_configured'
        # 3) transport activation is explicit and only reported as signaled
        if activate:
            try:
                pid = activate_transport(runtime, nats_pid)
                report['stages']['transport'] = 'signaled'
                report['transport_pid'] = pid
                report['transport_confirmation'] = 'not_checked (SIGHUP sent)'
                report['transport_activation_required'] = False
            except (ValueError, OSError) as error:
                report['stages']['transport'] = 'failed'
                report['transport_error'] = str(error)[:160]
        failed = [key for key, value in report['stages'].items() if value == 'failed']
        if failed:
            status = 'partial'
        elif report['stages'].get('transport') == 'signaled':
            status = 'complete'
        else:
            status = 'pending_transport_activation'
        report.update(applied=True, status=status, failed_stages=failed, backup=str(backup),
                      applied_at=datetime.now(timezone.utc).isoformat())
        atomic(backup / 'report.json', json.dumps(report, indent=2).encode())
        return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--employee', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--activate-transport', action='store_true',
                        help='send SIGHUP to the running NATS so the config change takes effect')
    parser.add_argument('--nats-pid', type=int)
    args = parser.parse_args()
    report = offboard(args.runtime, args.employee, args.reason, args.apply,
                      args.activate_transport, args.nats_pid)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.apply:
        return 0
    return {'complete': 0, 'partial': 1, 'pending_transport_activation': 3}.get(report.get('status'), 1)


if __name__ == '__main__':
    raise SystemExit(main())
