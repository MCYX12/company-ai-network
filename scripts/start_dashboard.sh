#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
export POC_RUNTIME="$PWD/.runtime-lan"
export NATS_URL="$(cat .runtime-lan/endpoint.txt)"
exec .venv/bin/python -m app.dashboard --identity ag-a --port 8787 --admin
