#!/bin/bash
# 补充数据回填：fina_audit（排雷审计意见）+ top_list（龙虎榜）+ stock_company（公司资料）
# 用法：等 backfill_finance.sh 跑完后手动执行（避免并发 OOM / sqlite 锁竞争）
cd /home/application/stock-analytics || exit 1
LOG=/home/application/stock-analytics/logs/backfill_extras.log
mkdir -p logs
echo "[$(date '+%F %T')] START backfill extras" >> "$LOG"

# 1) fina_audit 全市场（per-stock 全量，不带日期；约 5500+ 次调用 ~35 分钟）
echo "[$(date '+%F %T')] fina_audit all market" >> "$LOG"
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
  echo "[$(date '+%F %T')] fina_audit batch $i (offset $OFFSET)" >> "$LOG"
  ./venv/bin/python -m app.cli sync fina_audit --ts-codes "$CODES" --mode history >> "$LOG" 2>&1
done

# 2) top_list 近 30 个自然日（交易日由 sync 内部 trade_date 策略自动筛）
echo "[$(date '+%F %T')] top_list recent 30d" >> "$LOG"
./venv/bin/python -m app.cli sync top_list --start "$(date -d '30 days ago' '+%Y%m%d')" --end "$(date '+%Y%m%d')" >> "$LOG" 2>&1

# 3) stock_company 全量（幂等，重复跑无副作用）
echo "[$(date '+%F %T')] stock_company full" >> "$LOG"
./venv/bin/python -m app.cli sync stock_company >> "$LOG" 2>&1

echo "[$(date '+%F %T')] ALL DONE" >> "$LOG"