"""Package renewed certificates for one already-enrolled device (certificates only).

Used after scripts/renew_credentials.py to update a remote device without shipping
application code or credentials for any other identity. The archive carries the
refreshed ca.crt plus the crt/key of every enabled identity on the device, and an
installer that backs up the current files and restarts the device service.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.registry import validate_registry

INSTALL = '''#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
RUNTIME="${COMPANY_AGENT_RUNTIME:-$HOME/Library/Application Support/CompanyAgent}"
CERTS="$RUNTIME/.runtime/certs"
if [ ! -d "$CERTS" ]; then
  echo "ERROR: certificate directory not found: $CERTS"
  echo "Set COMPANY_AGENT_RUNTIME to the CompanyAgent runtime, then rerun."
  exit 1
fi
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$CERTS/history/$STAMP"
mkdir -p "$BACKUP"
for source in certs/*; do
  name="$(basename "$source")"
  [ -f "$CERTS/$name" ] && cp -p "$CERTS/$name" "$BACKUP/$name"
done
cp -p certs/* "$CERTS/"
chmod 700 "$CERTS" "$CERTS/history" 2>/dev/null || true
chmod 600 "$CERTS/"*.key
chmod 644 "$CERTS/"*.crt
echo "Certificates updated. Backup: $BACKUP"
echo "Certificate expiry now:"
for leaf in "$CERTS"/*.crt; do
  printf '  %s ' "$(basename "$leaf")"
  openssl x509 -in "$leaf" -noout -enddate | cut -d= -f2
done
if launchctl kickstart -k "gui/$(id -u)/com.companyagent.device" 2>/dev/null; then
  echo "Restarted com.companyagent.device"
else
  echo "Restart manually: launchctl kickstart -k gui/$(id -u)/com.companyagent.device"
fi
'''

README = '''Company Agent certificate renewal for one device.

1. Copy the zip to the device and unzip it.
2. Run:  bash install-certs.command
3. The installer backs up the old certificates under .runtime/certs/history/<stamp>,
   copies the renewed files, then restarts com.companyagent.device.

The files are private credentials: transfer them over an approved channel and delete
the archive afterwards. Private keys are unchanged by a same-key renewal; if a CA key
rotation was performed, every device must install this update before the old CA is retired.
'''


def export(runtime, device, output):
    runtime = Path(runtime).resolve()
    output = Path(output)
    registry = validate_registry(json.loads((runtime / 'registry.json').read_text()))
    rows = [row for row in registry.values() if row['device_id'] == device and row['enabled']]
    if not rows:
        raise ValueError('Device has no enabled identities')
    if output.exists():
        raise ValueError('Output already exists; choose a new filename')
    if not (runtime / 'certs/ca.crt').is_file():
        raise ValueError('Missing ca.crt; renew the runtime first')
    os.umask(0o077)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        archive.write(runtime / 'certs/ca.crt', 'certs/ca.crt')
        for row in sorted(rows, key=lambda item: item['agent_id']):
            for suffix in ['crt', 'key']:
                archive.write(runtime / 'certs' / f"{row['agent_id']}.{suffix}", f"certs/{row['agent_id']}.{suffix}")
        archive.writestr('install-certs.command', INSTALL)
        archive.writestr('README.txt', README)
    output.chmod(0o600)
    print(f"Exported {len(rows)} identities for {device}: {output}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--device', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    export(args.runtime, args.device, args.output)
