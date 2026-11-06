#!/bin/bash
# One-command human refresh of the published project record.
#
#   bash scripts/publish_record.sh "只跑了验收，状态未变"
#
# Re-derives the machine-checkable facts; if they changed it publishes normally, and if nothing
# changed it re-asserts the narrative with your note (the only action the automation will not
# take on your behalf). Mirrors are pushed whenever something was published.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NOTE="${1:-}"
if [ -z "$NOTE" ]; then
  echo "用法: bash scripts/publish_record.sh \"<这次确认或变更的一句话>\"" >&2
  exit 2
fi
exec bash "$ROOT/scripts/run_record_refresh.sh" --confirm "$NOTE"
