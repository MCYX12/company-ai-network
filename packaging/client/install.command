#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
for candidate in python3.14 python3.13 python3.12 python3.11 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 "$HOME/.local/bin/python3"; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,11))' >/dev/null 2>&1; then
    exec "$candidate" install.py "$@"
  fi
done
printf '%s\n' '需要 Python 3.11 或以上。请先由管理员安装 Python，再运行本安装器。'
exit 1
