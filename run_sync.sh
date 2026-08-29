#!/bin/bash
# crontab entry for tushare stock sync.
# Usage: run_sync.sh market|finance [extra args...]
set -u
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"

PY="${BASE_DIR}/venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi
[ -n "$PY" ] || { echo "no python3 found"; exit 2; }

GROUP="${1:-market}"
shift || true

mkdir -p logs
LOG="logs/sync_$(date +%Y%m%d_%H%M%S).log"

"$PY" -m app.cli sync "$GROUP" "$@" >>"$LOG" 2>&1
RC=$?
echo "exit=$RC group=$GROUP" >>"$LOG"

# keep the last 30 logs
ls -t logs/sync_*.log 2>/dev/null | tail -n +31 | xargs -r rm -f

exit $RC