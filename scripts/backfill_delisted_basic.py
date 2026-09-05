#!/usr/bin/env python3
"""补拉退市/暂停上市股票进 stock_basic —— 历史回测的前置步骤。

为什么需要：registry 里 stock_basic 的 `list_status` 写死为 "L"（只上市中），
导致 stock_basic 里没有任何退市股。做 2018/2022 这类熊市回测时，如果票池只能从
"今天还活着的股票"里选，就带**幸存者偏差**（乐视、长生这类跌到退市的票根本进不了池子，
回测收益会被系统性高估）。daily/adj_factor/daily_basic 是按交易日全市场拉的，
退市股的行情本来就在库里，缺的只是 stock_basic 里的身份记录。

本脚本不改 registry（那是每日定时同步的行为，改动需另行评审），只做一次性补录，
可重复执行（幂等 upsert）。

用法：
    cd /home/application/stock-analytics
    ./venv/bin/python scripts/backfill_delisted_basic.py            # 补 D + P
    ./venv/bin/python scripts/backfill_delisted_basic.py --check    # 只看现状
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from app.cli import build_context  # noqa: E402
from app.db import upsert_dataframe  # noqa: E402
from app.sync.registry import DATASETS  # noqa: E402

FIELDS = (
    "ts_code,symbol,name,area,industry,fullname,enname,cnspell,market,exchange,curr_type,"
    "list_status,list_date,delist_date,is_hs,act_name,act_ent_type"
)


def report(ctx) -> None:
    rows = ctx.store.query("SELECT list_status, COUNT(*) FROM stock_basic GROUP BY list_status")
    print("stock_basic 现状：")
    for status, count in rows:
        print(f"  list_status={status}: {count} 只")
    d = ctx.store.query(
        "SELECT COUNT(*) FROM stock_basic WHERE delist_date IS NOT NULL AND delist_date <> ''"
    )[0][0]
    print(f"  带退市日期的：{d} 只")


def main() -> int:
    parser = argparse.ArgumentParser(description="补录退市/暂停上市股票到 stock_basic")
    parser.add_argument("--statuses", default="D,P", help="要补的 list_status，默认 D,P")
    parser.add_argument("--check", action="store_true", help="只打印现状，不拉数据")
    args = parser.parse_args()

    ctx = build_context(enable_fallback=False)
    try:
        report(ctx)
        if args.check:
            return 0
        dataset = DATASETS["stock_basic"]
        unique = list(dataset.unique_columns)
        for status in [s.strip().upper() for s in args.statuses.split(",") if s.strip()]:
            try:
                frame = ctx.client.query("stock_basic", exchange="", list_status=status, fields=FIELDS)
            except Exception as exc:  # 限流/权限问题不重试，直接报告
                print(f"  ⚠️ list_status={status} 拉取失败：{type(exc).__name__}: {exc}")
                continue
            if frame.empty:
                print(f"  list_status={status}: 返回 0 行")
                continue
            missing = [c for c in unique if c not in frame.columns]
            if missing:
                print(f"  ⚠️ list_status={status}: 缺唯一键列 {missing}，跳过")
                continue
            affected = upsert_dataframe(
                ctx.store, dataset.table_name, frame, dataset.unique_columns, ctx.settings.sync_batch_size
            )
            print(f"  list_status={status}: 拉取 {len(frame)} 行，upsert affected={affected}")
        ctx.store.commit()
        print("\n补录后：")
        report(ctx)
    finally:
        ctx.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
