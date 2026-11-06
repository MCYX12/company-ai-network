#!/bin/bash
# Daily enterprise-CA renewal check; renews only when inside the threshold window.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
POC_RUNTIME="${POC_RUNTIME:-$ROOT/.runtime-lan}"
export POC_RUNTIME
PY="${PYTHON:-}"
if [ -z "$PY" ] && [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; fi
if [ -z "$PY" ]; then PY="$(command -v python3)"; fi
ARGS=(--runtime "$POC_RUNTIME" --threshold-days "${RENEW_THRESHOLD_DAYS:-30}" --apply --restart --device "${RENEW_DEVICE:-mac-demo}" --bundle-dir "$ROOT/dist-private")
if [ -n "${RENEW_MAC_HOST:-}" ]; then
  ARGS+=(--mac-host "$RENEW_MAC_HOST")
  if [ -n "${RENEW_SSH_KEY:-}" ]; then ARGS+=(--ssh-option "-i" --ssh-option "$RENEW_SSH_KEY"); fi
  ARGS+=(--ssh-option "-o" --ssh-option "BatchMode=yes" --ssh-option "-o" --ssh-option "StrictHostKeyChecking=accept-new")
fi
exec "$PY" -m scripts.auto_renew "${ARGS[@]}" "$@"
