#!/bin/bash
# Idle IO, lowest CPU niceness. Never writes world files.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PYTHON="$ROOT/venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi
export PYTHONUNBUFFERED=1
exec nice -n 19 ionice -c 3 "$PYTHON" "$ROOT/scanner.py" \
  --world /mnt/pool/survival/world \
  --out "$ROOT/output" \
  --push \
  "$@"
