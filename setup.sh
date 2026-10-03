#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ ! -x "$ROOT/.venv/bin/python" ]] || ! "$ROOT/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
  if [[ -e "$ROOT/.venv" ]]; then
    echo 'Existing .venv is incomplete; rename/remove it manually before setup.' >&2
    exit 1
  fi
  "${PYTHON:-python3}" -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install --index-url "${DOWNLOAD_TOOLS_PIP_INDEX:-https://pypi.org/simple}" -r "$ROOT/requirements.txt"
"$ROOT/.venv/bin/python" "$ROOT/transfer.py" --config "${DOWNLOAD_TOOLS_CONFIG:-$ROOT/config.yaml}" init
if [[ ! -x "$ROOT/bin/aria2c" ]] || [[ ! -f "$ROOT/bin/build-info.json" ]]; then
  bash "$ROOT/build_aria2.sh"
fi
printf 'Ready. Edit %s, then run upload.sh or download.sh.\n' "${DOWNLOAD_TOOLS_CONFIG:-$ROOT/config.yaml}"
