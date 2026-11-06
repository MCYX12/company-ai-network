#!/bin/bash
# Periodic project-record refresh: re-derive the machine-checkable facts and publish only when
# something actually changed (see scripts/refresh_project_record.py for why re-stamping a
# timestamp would be dishonest). Local publish always; remote mirrors only when the local file
# changed and a non-interactive SSH path is available.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -z "${POC_RUNTIME:-}" ]; then
  if [ -f "$ROOT/.runtime-lan/endpoint.txt" ]; then POC_RUNTIME="$ROOT/.runtime-lan"
  elif [ -f "$ROOT/.runtime/endpoint.txt" ]; then POC_RUNTIME="$ROOT/.runtime"
  else POC_RUNTIME="$ROOT/.runtime"; fi
fi
export POC_RUNTIME
PY="${PYTHON:-}"
if [ -z "$PY" ] && [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; fi
if [ -z "$PY" ]; then PY="$(command -v python3)"; fi

SOURCE="${POC_RECORD_SOURCE:-$ROOT/release/project-records/company-ai-network.json}"
OUTPUT="$("$PY" -m scripts.refresh_project_record --source "$SOURCE" --publish "$POC_RUNTIME" "$@")"
echo "$OUTPUT"

# A frozen remote must not be contacted by the periodic publisher either.
if [ "${POC_RECORD_MIRRORS:-1}" = "0" ]; then
  echo "remote mirrors disabled by operator; local record refresh retained"
  exit 0
fi

# Mirrors: one "ssh-target|remote project-records dir" per line; missing file = local only.
# The mirror runs every cycle (not only when the local record changed) and keeps a durable pending
# list: a transient scp/ssh failure is retried and the remote sha256 verified until it matches, so
# a sleeping device cannot keep answering from an old snapshot unnoticed.
TARGETS="$ROOT/release/project-records/push-targets.txt"
[ -f "$TARGETS" ] || { echo "no remote push: $TARGETS not configured (local publish only)"; exit 0; }
RECORD="$POC_RUNTIME/project-records/$(basename "$SOURCE")"
[ -f "$RECORD" ] || { echo "no remote push: $RECORD does not exist yet"; exit 0; }
"$PY" -m scripts.record_mirror --record "$RECORD" --targets "$TARGETS" \
  --state "$POC_RUNTIME/.mirror-state.json"
