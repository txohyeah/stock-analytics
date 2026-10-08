#!/usr/bin/env python3
"""大盘复盘日报 —— 只报状态，不给建议（2026-10-08，B2）。

数据源：`market_state_daily`（脚本 `market_state.py` 维护）+ 当日 `daily` 涨跌家数。
输出四件：① 等权指数 vs MA60 ② 20 日宽度 ③ 当日涨跌家数/中位涨幅 ④ 定性（当日 + 中期两段，2026-10-08 定稿）。

**纪律（弱耦合，勿破）**：
  - 只给状态判断，**不给买卖建议**、**不当过滤器**（不参与任何信号筛选）；
  - 天天标注口径与「结论来自 2018-2026 样本内」，避免被当成承诺；
  - 与信号层共用市场层这一份计算结果，禁止联合调参。

用法：
    ./venv/bin/python scripts/market_review.py                  # 增量更新后输出最新交易日
    ./venv/bin/python scripts/market_review.py --date 20260930   # 指定日期
    ./venv/bin/python scripts/market_review.py --no-update       # 不更新状态表
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.market_state import connect, update  # noqa: E402

DB = ROOT / "data" / "stock.db"

# 各状态样本内的起爆信号表现（2018-2026，29,818 笔事件研究；来源
# research/pa-state-label/reports/state_at_events_v1.csv，勿改口径）
STATE_STATS = {
    "above_宽": (-0.01, -1.18, 2432),
    "above_窄": (1.67, -0.40, 3862),
    "below_宽": (5.66, 4.08, 1336),
    "below_窄": (5.55, 4.45, 22188),
}
STATE_ZH = {
    "above_窄": "指数在 MA60 上方、宽度窄 —— 指数偏强但赚钱面窄（普涨不足）",
    "above_宽": "指数在 MA60 上方、宽度宽 —— 指数与普涨同步，样本内这种格局反而最差",
    "below_窄": "指数在 MA60 下方、宽度窄 —— 指数弱、面也窄，样本内反而是起爆信号的主场",
    "below_宽": "指数在 MA60 下方、宽度宽 —— 指数弱但普涨修复中，样本内与 below_窄 相当",
}


def day_counts(con: sqlite3.Connection, date: str) -> dict:
    px = pd.read_sql_query("SELECT pct_chg FROM daily WHERE trade_date = ? AND pct_chg IS NOT NULL",
                           con, params=(date,))["pct_chg"]
    return {"up": int((px > 0).sum()), "down": int((px < 0).sum()), "flat": int((px == 0).sum()),
            "median": float(px.median()), "down5": int((px < -5).sum()), "n": int(len(px))}


def fmt_review(con: sqlite3.Connection, date: str) -> str:
    rows = pd.read_sql_query(
        "SELECT * FROM market_state_daily WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 2",
        con, params=(date,))
    if rows.empty:
        return f"⚠️ market_state_daily 无 {date} 及以前的数据（先跑 scripts/market_state.py update）"
    cur = rows.iloc[0]
    prev = rows.iloc[1] if len(rows) > 1 else None
    c = day_counts(con, cur["trade_date"])
    d = f"{cur['trade_date'][:4]}-{cur['trade_date'][4:6]}-{cur['trade_date'][6:]}"
    wk = "一二三四五六日"[pd.Timestamp(d).weekday()]
    dir_zh = "上方" if cur["mkt_dir"] == "above" else "下方"
    gap = (cur["ew_idx"] / cur["ew_ma60"] - 1) * 100
    out = [f"📊 大盘复盘 {d}（周{wk}）", ""]
    out.append(f"①方向：等权指数 {cur['ew_idx']:.4f}｜MA60 {cur['ew_ma60']:.4f} → 在 MA60 {dir_zh}"
               f"（{gap:+.2f}%）；MA60 十日斜率 {(cur['ew_slope60'] or 0)*100:+.2f}%")
    b_now = cur["breadth20"] * 100
    b_prev = f"（前一日 {prev['breadth20']*100:.1f}%）" if prev is not None else ""
    out.append(f"②宽度：20 日上涨家数占比 {b_now:.1f}%{b_prev} → {cur['mkt_breadth']}（阈 50%）")
    out.append(f"③当日：涨 {c['up']} / 跌 {c['down']} / 平 {c['flat']}（共 {c['n']} 只，剔无行情）；"
               f"中位涨幅 {c['median']:+.2f}%；跌超 5% {c['down5']} 家")
    # ④定性（2026-10-08 定稿）：当日与中期分开说，跌得狠的日子先说当日
    # 当日分档：等权涨跌 ±0.5% 内=平稳；超出且上涨占比 <40%=普跌、>60%=普涨；其余=分化
    ew_ret = (cur["ret_mean"] or 0) * 100
    up_pct = c["up"] / c["n"] * 100 if c["n"] else 0.0
    if abs(ew_ret) <= 0.5:
        day_tag = "平稳"
    elif ew_ret < 0 and up_pct < 40:
        day_tag = "普跌"
    elif ew_ret > 0 and up_pct > 60:
        day_tag = "普涨"
    else:
        day_tag = "分化"
    slope_pct = (cur["ew_slope60"] or 0) * 100
    slope_zh = "走平" if abs(slope_pct) <= 0.05 else ("上行" if slope_pct > 0 else "下行")
    if cur["mkt_dir"] == "above":
        mid_seg = (f"仍在 MA60 上方但缓冲薄（{gap:+.2f}%）" if abs(gap) < 1
                   else f"仍在 MA60 上方（缓冲 {gap:+.2f}%）")
    else:
        mid_seg = f"位于 MA60 下方（{gap:+.2f}%）"
    out.append(f"④定性：当日{day_tag}（等权 {ew_ret:+.2f}%，上涨 {up_pct:.1f}%）"
               f"｜中期：{mid_seg}，MA60 {slope_zh}")
    st = STATE_STATS.get(cur["mkt_state"])
    if st:
        state_short = STATE_ZH.get(cur["mkt_state"], "状态样本不足").split(" —— ")[0]
        out.append(f"　（中期状态「{state_short}」样本内参照 2018-2026：该状态 {st[2]:,} 笔起爆，"
                   f"f20 均值 {st[0]:+.2f}%、中位 {st[1]:+.2f}%——历史统计，不是承诺）")
    out.append("")
    out.append("口径：等权指数 = 全市场个股当日涨跌幅（截尾 ±21%）横截面均值逐日累乘（2017 起）；"
               "方向 = 指数 vs 自身 MA60；宽度 = 20 日上涨家数占比 >50% 判宽；"
               "当日定性 = 等权涨跌 ±0.5% 内为平稳，超出且上涨占比 <40% 为普跌、>60% 为普涨，其余为分化。")
    out.append("纪律：只报状态，不作买卖建议、不当过滤器；结论出自 2018-2026 样本内。")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="大盘复盘日报")
    ap.add_argument("--date", help="指定交易日 YYYYMMDD，默认最新")
    ap.add_argument("--no-update", action="store_true", help="不先增量更新 market_state_daily")
    args = ap.parse_args()

    if not args.no_update:
        ucon = connect()
        n = update(ucon)
        ucon.close()
        print(f"[market_state 增量 +{n} 行]", file=sys.stderr)
    con = sqlite3.connect(DB)
    date = args.date or con.execute("SELECT MAX(trade_date) FROM market_state_daily").fetchone()[0]
    print(fmt_review(con, date))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
