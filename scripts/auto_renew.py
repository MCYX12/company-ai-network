"""Scheduled enterprise-CA renewal and expiry alerting.

Checks the runtime's leaf certificates; when one is inside the renewal window it
re-issues from the intermediate CA (reusing keys), rebuilds chains, validates with
nats-server -t and backups, writes health/renewal.json and, on failure, appends an
alert. Optionally restarts the stack, builds a Mac update bundle and (with a
non-interactive SSH host configured) pushes it. Never touches a runtime that is not
on the enterprise CA.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.renew_credentials import cert_seconds_remaining
from scripts.renew_enterprise_ca import renew_runtime


def expiring(runtime, threshold_days=30):
    certs = Path(runtime).resolve() / 'certs'
    rows = []
    for path in sorted(certs.glob('*.crt')):
        name = path.stem
        if name == 'ca' or name.startswith('ca.'):
            continue
        if not (certs / f'{name}.key').exists():
            continue
        seconds = cert_seconds_remaining(path)
        if seconds < threshold_days * 86400:
            rows.append({'name': name, 'seconds_remaining': round(seconds),
                         'not_after': datetime.fromtimestamp(seconds + __import__('time').time(), timezone.utc).isoformat()})
    return rows


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.renewal-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if Path(name).exists():
            Path(name).unlink()


def _status(runtime, report):
    _atomic(Path(runtime) / 'health' / 'renewal.json', json.dumps(report, ensure_ascii=False, indent=2))


def _alert(runtime, report, message):
    entry = {'time': datetime.now(timezone.utc).isoformat(), 'event': 'certificate-renewal', 'message': message,
             'expiring': report.get('expiring'), 'error': report.get('error')}
    state = Path(runtime) / 'health'
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'alerts.jsonl').open('a') as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
    logger = shutil.which('logger')
    if logger:
        subprocess.run([logger, '-t', 'company-agent', 'certificate renewal: ' + message], check=False)


def _push_mac(bundle, host, options):
    subprocess.run(['scp', *options, str(bundle), f'{host}:/tmp/company-agent-certs.zip'], check=True, timeout=60)
    remote = ('cd /tmp && rm -rf ca-update && mkdir ca-update && cd ca-update && '
              'unzip -q /tmp/company-agent-certs.zip && bash install-certs.command')
    subprocess.run(['ssh', *options, host, remote], check=True, timeout=120)


def auto_renew(runtime, threshold_days=30, apply=False, restart=False, device=None, bundle_dir=None,
               mac_host=None, ssh_options=None):
    runtime = Path(runtime).resolve()
    certs = runtime / 'certs'
    if not (certs / 'ENTERPRISE_CA.json').is_file():
        raise ValueError('Runtime is not on the enterprise CA (missing certs/ENTERPRISE_CA.json)')
    report = {'runtime': str(runtime), 'checked_at': datetime.now(timezone.utc).isoformat(),
              'threshold_days': threshold_days, 'expiring': expiring(runtime, threshold_days),
              'renewed': False, 'restarted': False}
    if not report['expiring']:
        report['action'] = 'none: all certificates outside the renewal window'
        _status(runtime, report)
        return report
    if not apply:
        report['action'] = 'dry run: certificates would be renewed'
        _status(runtime, report)
        return report
    blocked = []
    try:
        from app.registry import load_registry
        from app import iam
        for name, row in load_registry().items():
            if iam.is_blocked(row.get('employee_id')):
                blocked.append(name)
    except Exception:
        blocked = []
    report['skipped_inactive'] = sorted(blocked)

    def _recheck(name):
        # Under the CA lock: the status may have changed since the skip list was computed.
        from app.registry import load_registry
        from app import iam
        row = load_registry().get(name)
        return row is None or not iam.is_blocked(row.get('employee_id'))

    try:
        report['renewal'] = renew_runtime(runtime, days=365, apply=True, skip=blocked, authorize=_recheck)
        report['renewed'] = True
    except Exception as error:
        report['action'] = 'renewal failed'
        report['error'] = repr(error)
        _status(runtime, report)
        _alert(runtime, report, 'renewal failed for ' + ', '.join(item['name'] for item in report['expiring']))
        raise
    if device and bundle_dir:
        from scripts.export_cert_renewal import export
        bundle_dir = Path(bundle_dir)
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle = bundle_dir / ('mac-cert-renewal-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.zip')
        export(runtime, device, bundle)
        report['mac_bundle'] = str(bundle)
        report['mac_distribution'] = 'manual: run install-certs.command on the device'
        if mac_host:
            try:
                _push_mac(bundle, mac_host, ssh_options or [])
                report['mac_distribution'] = 'pushed to ' + mac_host
            except Exception as error:
                report['mac_distribution'] = 'push failed: ' + repr(error)
                _alert(runtime, report, 'Mac certificate push failed: ' + repr(error))
    if restart:
        subprocess.run(['systemctl', '--user', 'restart', 'company-agent-stack'], check=True, timeout=30)
        report['restarted'] = True
    report['action'] = 'renewed'
    _status(runtime, report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--threshold-days', type=int, default=30)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--restart', action='store_true')
    parser.add_argument('--device')
    parser.add_argument('--bundle-dir', type=Path)
    parser.add_argument('--mac-host')
    parser.add_argument('--ssh-option', action='append', default=[])
    args = parser.parse_args()
    report = auto_renew(args.runtime, args.threshold_days, args.apply, args.restart, args.device,
                        args.bundle_dir, args.mac_host, args.ssh_option)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
