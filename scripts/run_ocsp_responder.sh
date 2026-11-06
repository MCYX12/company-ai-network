#!/bin/bash
# OCSP responder: loopback-only threaded Python service (replaces single-threaded openssl ocsp).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CA="${ENTERPRISE_CA_DIR:-$ROOT/.runtime-lan/enterprise-ca}"
PORT="${OCSP_PORT:-2560}"
BIND="${OCSP_BIND:-127.0.0.1}"
PY="${PYTHON:-}"
if [ -z "$PY" ] && [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; fi
if [ -z "$PY" ]; then PY="$(command -v python3)"; fi
exec "$PY" "$ROOT/scripts/ocsp_responder.py" --ca-dir "$CA" --port "$PORT" --bind "$BIND"
