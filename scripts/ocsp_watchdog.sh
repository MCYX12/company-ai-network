#!/bin/bash
# Restart the OCSP responder if it stops answering. openssl ocsp is single-threaded
# and has been observed to spin without answering, which fail-closes every handshake.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CA="${ENTERPRISE_CA_DIR:-$ROOT/.runtime-lan/enterprise-ca}"
CERT="${OCSP_SELFTEST_CERT:-$ROOT/.runtime-lan/certs/ag-a.crt}"
URL="${OCSP_URL:-http://127.0.0.1:2560}"
# The root CA is offline, so $CA/root/root.crt never exists. Probing with a missing
# -CAfile made every check fail and restarted a healthy responder once a minute,
# which fail-closed handshakes for every peer. Use the bundle that does exist: the
# intermediate chain (intermediate + root) or the runtime's root copy.
CAFILE="$CA/intermediate/chain.crt"
[ -f "$CAFILE" ] || CAFILE="$ROOT/.runtime-lan/certs/ca.crt"
if [ ! -f "$CERT" ] || [ ! -f "$CA/intermediate/index.txt" ] || [ ! -f "$CAFILE" ]; then
  exit 0
fi
probe() {
  timeout 6 openssl ocsp -issuer "$CA/intermediate/intermediate.crt" -cert "$CERT" \
    -url "$URL" -CAfile "$CAFILE" -no_nonce >/dev/null 2>&1
}
# One failed probe is not evidence of a wedged responder; require two before
# restarting, so a transient timeout cannot kill in-flight OCSP checks.
if probe || { sleep 2; probe; }; then
  exit 0
fi
logger -t company-agent "OCSP responder unresponsive; restarting company-agent-ocsp.service"
systemctl --user restart company-agent-ocsp.service
