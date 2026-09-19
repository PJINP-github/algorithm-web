#!/usr/bin/env bash
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] 未找到 python3，请安装 Python 3.9+。"
  exit 1
fi

python3 portal_entry.py
exit $?