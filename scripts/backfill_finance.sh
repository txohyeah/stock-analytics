#!/bin/bash
# 全市场财务数据补全：fina_indicator + income + balancesheet + cashflow
# 逐股同步（tushare 财务接口必须 ts_code），公告窗口 2023-01-01 起
cd /home/application/stock-analytics || exit 1
LOG=/home/application/stock-analytics/logs/finance_backfill.log
mkdir -p logs
echo "[$(date '+%F %T')] START finance backfill (all market)" >> "$LOG"
for i in $(seq 0 11); do
  OFFSET=$((i * 500))
  CODES=$(./venv/bin/python -c "
import sqlite3
con = sqlite3.connect('data/stock.db')
rows = con.execute(\"SELECT ts_code FROM stock_basic WHERE list_status NOT IN ('D','P') ORDER BY ts_code LIMIT 500 OFFSET $OFFSET\").fetchall()
print(','.join(r[0] for r in rows))
")
  if [ -z "$CODES" ]; then
    echo "[$(date '+%F %T')] batch $i empty, stop" >> "$LOG"
    break
  fi
  echo "[$(date '+%F %T')] batch $i (offset $OFFSET)" >> "$LOG"
  ./venv/bin/python -m app.cli sync finance --ts-codes "$CODES" --start 20230101 --mode history >> "$LOG" 2>&1
done
echo "[$(date '+%F %T')] ALL DONE" >> "$LOG"