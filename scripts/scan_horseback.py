#!/usr/bin/env python3
"""主力回马枪 v2 每日盘后扫描（watchlist_site 关注池）。

形态定义（specs/money-flow-indicators.md v1.0 定稿）：
  介入日 D0：主力净额/成交额 >= 12%，且主力净额 >= 流通市值 x 0.3%（兜底）。
  回调期：D0 后 1~10 个交易日内，任一日最低价不跌破 D0 最低价；
          任一日收盘不超过 D0 收盘 x 1.03（回调允许横盘微涨）；
          主力留存率 = (净额0 + 回调期净额合计) / 净额0 >= 70%；
          无单日净流出 > 净额0 x 50%（熔断）；
          总涨幅 = 当前收盘 / D0 前收盘 - 1 <= 15%。

口径说明：本扫描只给候选名单，不含板块联动与次日竞价确认（该数据无历史库），
由使用者人工把关。2026-09-18 决策：回马枪不做历史回测（板块/竞价数据缺失），
改为每日盘后推送候选。

用法：
    ./venv/bin/python scripts/scan_horseback.py                     # 最新交易日
    ./venv/bin/python scripts/scan_horseback.py --date 20260917
    ./venv/bin/python scripts/scan_horseback.py --json
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "stock.db"
WL = ROOT / "data" / "watchlist_site.csv"

# ---- 参数（定稿 v1.0，勿随手改；调整须走 spec 变更）----
MIN_RATIO = 0.12          # 主力净额 / 成交额
MIN_MV_PCT = 0.003        # 主力净额 / 流通市值（兜底）
MAX_DAYS = 10             # 回调期上限（交易日）
MAX_PULL_GAIN = 0.03      # 回调期任一日收盘 <= D0 收盘 x 1.03
MAX_TOTAL_GAIN = 0.15     # 总涨幅上限（D0 前收盘 -> 当前收盘）
MIN_KEEP = 0.70           # 主力留存率
BREAK_PCT = 0.50          # 单日净流出 > D0 净额 x 50% 熔断
LOOKBACK = 40             # 往前取的交易日数（> MAX_DAYS + 预热余量）

NET_SQL = "(m.buy_elg_amount + m.buy_lg_amount - m.sell_elg_amount - m.sell_lg_amount)"


def latest_trade_date(con: sqlite3.Connection) -> str:
    return con.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]


def load_pool() -> dict[str, tuple[str, str]]:
    pool = {}
    with open(WL, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            pool[r["code"]] = (r["name"], r.get("category") or "")
    return pool


def scan(date: str) -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    pool = load_pool()
    ph = ",".join("?" * len(pool))
    # 每票只取 date 及其前 LOOKBACK 个交易日
    rows = con.execute(
        f"""WITH days AS (
              SELECT DISTINCT trade_date FROM daily WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?
            )
            SELECT d.ts_code, d.trade_date, d.low, d.close, d.pre_close,
                   d.amount / 10.0 AS amt, {NET_SQL} AS net,
                   db.circ_mv / 10000.0 AS mv_yi
            FROM daily d
            JOIN moneyflow m ON m.ts_code = d.ts_code AND m.trade_date = d.trade_date
            LEFT JOIN daily_basic db ON db.ts_code = d.ts_code AND db.trade_date = d.trade_date
            WHERE d.ts_code IN ({ph}) AND d.trade_date IN (SELECT trade_date FROM days)
            ORDER BY d.ts_code, d.trade_date""",
        [date, LOOKBACK] + list(pool),
    ).fetchall()
    con.close()

    by = defaultdict(list)
    for r in rows:
        by[r["ts_code"]].append(dict(r))

    hits = []
    for code, s in by.items():
        if code not in pool or not s or s[-1]["trade_date"] != date:
            continue  # 当日停牌/缺资金流的票不扫
        name, cat = pool[code]
        cur = s[-1]
        for k in range(1, MAX_DAYS + 1):
            j = len(s) - 1 - k          # D0 下标
            if j < 0:
                break
            d0 = s[j]
            after = s[j + 1:]           # 回调期：D0+1 .. 当前日
            amt0, net0 = d0["amt"], d0["net"]
            if not amt0 or not d0["mv_yi"] or not net0 or net0 <= 0:
                continue
            ratio0 = net0 / amt0
            if ratio0 < MIN_RATIO:
                continue
            if net0 < d0["mv_yi"] * 10000 * MIN_MV_PCT:
                continue
            if any(a["low"] < d0["low"] for a in after):
                continue
            if max(a["close"] for a in after) > d0["close"] * (1 + MAX_PULL_GAIN):
                continue
            cum = sum(a["net"] for a in after)
            keep = (net0 + cum) / net0
            if keep < MIN_KEEP:
                continue
            if any(a["net"] < -BREAK_PCT * net0 for a in after):
                continue
            gain = cur["close"] / d0["pre_close"] - 1
            if gain > MAX_TOTAL_GAIN:
                continue
            hits.append(dict(
                code=code, name=name, cat=cat, d0=d0["trade_date"], days=len(after),
                ratio0=ratio0, mv_pct=net0 / (d0["mv_yi"] * 10000), net0=net0,
                keep=keep, gain=gain,
            ))
            break                        # 取最近的合格介入日
    hits.sort(key=lambda x: -x["net0"])
    return hits


def fmt_text(date: str, hits: list[dict], pool_n: int) -> str:
    ds = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    lines = [f"🎯 主力回马枪候选 {ds}", f"命中 {len(hits)} 只 / 池 {pool_n} 只", ""]
    if not hits:
        lines = [f"🎯 主力回马枪候选 {ds}", f"今日无命中（池 {pool_n} 只）"]
    for h in hits:
        d0 = h["d0"]
        lines.append(
            f"● {h['code']} {h['name']} [{h['cat']}] 介入{d0[4:6]}/{d0[6:]}·回调第{h['days']}天"
            f"｜占比 {h['ratio0']:.1%}｜占流通 {h['mv_pct']:.2%}｜介入净额 {h['net0']/1e4:.1f}亿"
            f"｜留存率 {h['keep']:.0%}｜介入以来 {h['gain']:+.1%}"
        )
    lines.append("")
    lines.append("注：候选仅供参考，板块联动与次日竞价请自行确认。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="主力回马枪 v2 每日扫描")
    ap.add_argument("--date", help="YYYYMMDD，默认最新交易日")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    date = args.date or latest_trade_date(con)
    con.close()

    pool = load_pool()
    hits = scan(date)
    if args.json:
        print(json.dumps({"date": date, "pool": len(pool), "hits": hits},
                         ensure_ascii=False, indent=1))
    else:
        print(fmt_text(date, hits, len(pool)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
