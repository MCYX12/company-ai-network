#!/bin/bash
# One-command self-check for any computer in this deployment.
# Works on the Ubuntu server, the Mac worker and a member client install.
# Exit code: 0 normal, 1 failed, 2 warning.
set -o pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$PYTHON"
if [ -z "$PY" ] && [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; fi
if [ -z "$PY" ]; then PY="$(command -v python3)"; fi
if [ -z "$PY" ]; then echo "FAIL 未找到 Python 3.11+"; exit 2; fi

RUNTIME="$POC_RUNTIME"
if [ -z "$RUNTIME" ]; then
  for d in "$ROOT/.runtime-lan" "$ROOT/.runtime" "$HOME/Library/Application Support/CompanyAgent/.runtime" "$HOME/Library/Application Support/CompanyAgentClient/.runtime" "$HOME/.local/share/CompanyAgentClient/.runtime"; do
    if [ -d "$d" ]; then RUNTIME="$d"; break; fi
  done
fi
IDENTITY="$AGENT_IDENTITY"
if [ -z "$IDENTITY" ]; then IDENTITY="$HEALTH_IDENTITY"; fi
if [ -z "$IDENTITY" ]; then IDENTITY="ag-a"; fi

echo "== $(hostname) 自查 =="
if [ -n "$RUNTIME" ]; then echo "身份: $IDENTITY   运行目录: $RUNTIME"; else echo "身份: $IDENTITY   运行目录: 未找到"; fi
echo "磁盘可用: $(df -h "$ROOT" | awk 'NR==2{print $4}')   时间: $(date '+%F %T')"

if [ -n "$RUNTIME" ] && [ -f "$RUNTIME/endpoint.txt" ]; then
  export POC_RUNTIME="$RUNTIME"
  export NATS_URL="$(cat "$RUNTIME/endpoint.txt")"
fi

STATUS=0
if [ -f "$ROOT/scripts/run_healthcheck.sh" ]; then
  echo "-- 服务器/终端健康检查 --"
  HEALTH_IDENTITY="$IDENTITY" bash "$ROOT/scripts/run_healthcheck.sh"
  STATUS=$?
else
  echo "-- 成员客户端自查 --"
  OUT="$("$PY" -m app.peer_client --identity "$IDENTITY" --doctor)"
  echo "$OUT"
  if echo "$OUT" | grep -qE '"status"[[:space:]]*:[[:space:]]*"error"'; then STATUS=1
  elif echo "$OUT" | grep -qE '"status"[[:space:]]*:[[:space:]]*"warning"'; then STATUS=2
  else STATUS=0; fi
fi

if [ "$STATUS" = "0" ]; then echo "结果: 正常"
elif [ "$STATUS" = "2" ]; then echo "结果: 有警告，请看上面"
else echo "结果: 有失败，请看上面"; fi
exit "$STATUS"
