#!/bin/bash
# Periodic health check + log rotation. Works on Ubuntu (systemd user timer) and macOS (launchd).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -z "${POC_RUNTIME:-}" ]; then
  if [ -f "$ROOT/.runtime-lan/endpoint.txt" ]; then POC_RUNTIME="$ROOT/.runtime-lan"
  elif [ -f "$ROOT/.runtime/endpoint.txt" ]; then POC_RUNTIME="$ROOT/.runtime"
  else POC_RUNTIME="$ROOT/.runtime"; fi
fi
export POC_RUNTIME
if [ -z "${NATS_URL:-}" ]; then
  if [ -f "$POC_RUNTIME/endpoint.txt" ]; then NATS_URL="$(cat "$POC_RUNTIME/endpoint.txt")"
  elif [ -f "$ROOT/endpoint.txt" ]; then NATS_URL="$(cat "$ROOT/endpoint.txt")"
  else NATS_URL="tls://localhost:14222"; fi
fi
export NATS_URL
PY="${PYTHON:-}"
if [ -z "$PY" ] && [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; fi
if [ -z "$PY" ]; then PY="$(command -v python3)"; fi
exec "$PY" -m scripts.healthcheck_alert --identity "${HEALTH_IDENTITY:-ag-a}" --runtime "$POC_RUNTIME" "$@"
