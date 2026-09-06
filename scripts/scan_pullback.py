#!/usr/bin/env python3
"""趋势回踩起爆扫描 —— 只扫指定个股池（默认 = stocks 站点关注池），不做全市扫描。

定位必须先说清楚（2026-09-06 口径）：
  旧版「趋势回踩起爆」四条件在无前视重建池上机械执行已被证伪（各卖法单票中位累计全负；
  此前的优势经查全部来自金牛通道的未来函数，修复后因果口径仅 +0.18%/胜率 41.3%）。
  但在用户自选池（人工精选）上组合层复验年化 22.4%，与用户两年实盘自述吻合
  —— edge 在选股、不在信号。当前三条件版移除了 60 日回撤门槛，尚未重新回测，
  因此本扫描器**不选股**，只替你盯你自己选的票；票池一换结论就不成立，勿当全市选股器用。

三条件（共享函数 `trend_pullback_signal_series`，通道为因果口径 causal=True）：
  1. RSI6 从下穿上 40                       —— 重新发力
  2. 当日成交量 ≥ 5 日均量 × 1.2，或最低价 ≤ 金牛趋势线 —— 放量或回踩支撑
  3. 金牛通道非空头（确认线不在生命线上方）    —— 长期上升结构没坏

「★重手」标记：现价距金牛上沿 ≥8%（历史加码期望显著更高的位置；用法是加码信号，
  不是筛选条件——当必要条件会滤掉约 80% 机会）。
「飞行区」：距上沿为负 = 收盘已站上沿上方，上沿压制出场规则不会触发。

用法：
    ./venv/bin/python scripts/scan_pullback.py                    # 扫关注池最新交易日
    ./venv/bin/python scripts/scan_pullback.py --days 5           # 最近 5 个交易日的信号都列出
    ./venv/bin/python scripts/scan_pullback.py --codes 300308.SZ,600114.SH
    ./venv/bin/python scripts/scan_pullback.py --date 20260904 --csv /tmp/pullback.csv
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tech_indicators.ignition import (  # noqa: E402
    golden_channel_state,
    ignition_rsi,
    ignition_stop_line,
    trend_pullback_signal_series,
)

DB = ROOT / "data" / "stock.db"
BARS = 140                    # 预热：60日位置窗口 + 通道（因果版预热约24根）都够
DEFAULT_POOL = ROOT / "data" / "watchlist_site.csv"   # sync_watchlist.py 的产物
HEAVY_UPPER_PCT = 8.0           # 重手标记：距上沿 ≥8%


def latest_date(con) -> str:
    return con.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]


def load_pool(args) -> list[str]:
    if args.codes:
        return ([c.strip() for c in open(args.codes[1:]).read().split()]
                if args.codes.startswith("@")
                else [c.strip() for c in args.codes.split(",") if c.strip()])
    if not DEFAULT_POOL.exists():
        sys.exit(f"票池不存在：{DEFAULT_POOL}（先跑 scripts/sync_watchlist.py 从 stocks 站点同步，"
                 f"或用 --codes 指定个股）")
    frame = pd.read_csv(DEFAULT_POOL, dtype=str).fillna("")
    return frame["code"].tolist()


def load_bars(con, code: str) -> pd.DataFrame | None:
    rows = pd.read_sql_query(
        "SELECT d.trade_date, d.open, d.high, d.low, d.close, d.pct_chg, d.vol, a.adj_factor "
        "FROM daily d JOIN adj_factor a ON a.ts_code = d.ts_code AND a.trade_date = d.trade_date "
        "WHERE d.ts_code = ? ORDER BY d.trade_date DESC LIMIT ?",
        con, params=(code, BARS),
    )
    if rows.empty:
        return None
    rows = rows.iloc[::-1].reset_index(drop=True)
    if rows["adj_factor"].isna().any():
        return None
    k = rows["adj_factor"] / rows["adj_factor"].iloc[-1]   # 前复权：一律以样本末日为基准
    out = pd.DataFrame({"trade_date": rows.trade_date.values})
    for col in ("open", "high", "low", "close"):
        out[col] = (rows[col] * k).astype("float64").values
    out["vol"] = rows.vol.values
    out["pct_chg"] = rows.pct_chg.values
    return out


def scan_one(df: pd.DataFrame, name: str, category: str) -> list[dict]:
    """返回该票所有命中「趋势回踩起爆」三条件的交易日明细。"""
    c = df.close.values
    lows = df.low.values
    rsi = ignition_rsi(df).values
    dd60 = (c / pd.Series(df.high.values).rolling(60).max().values - 1) * 100  # 仅展示，不参与信号
    vol5 = pd.Series(df.vol.values).rolling(5).mean().shift(1).values
    vol5x = df.vol.values / np.where(vol5 == 0, np.nan, vol5)
    signal = trend_pullback_signal_series(df).values
    ch = golden_channel_state(df, causal=True)
    upper = ch["upper"].values

    out = []
    for i in range(len(df)):
        if (np.isnan(rsi[i]) or np.isnan(vol5x[i])
                or not np.isfinite(upper[i])):
            continue                        # 预热不足，跳过这几根
        if not signal[i]:
            continue
        stop = ignition_stop_line(float(c[i]), lows, i + 1)   # C2 兜底线：max(买价×0.90, 最近30根最低)
        to_upper = float((upper[i] / c[i] - 1) * 100)
        out.append(dict(
            trade_date=str(df.trade_date.iloc[i]), close=round(float(c[i]), 2),
            pct_chg=round(float(df.pct_chg.values[i]), 2), rsi=round(float(rsi[i]), 1),
            dd60=round(float(dd60[i]), 1), vol5x=round(float(vol5x[i]), 2),
            to_upper=round(to_upper, 1),
            stop=round(stop, 2), stop_pct=round((stop / c[i] - 1) * 100, 1),
            heavy=bool(to_upper >= HEAVY_UPPER_PCT),
            name=name, category=category,
        ))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="趋势回踩起爆 · 指定个股池扫描")
    ap.add_argument("--date", help="扫描日，默认库里最新交易日")
    ap.add_argument("--days", type=int, default=1, help="回看几个交易日内的信号（默认 1 = 只看当日）")
    ap.add_argument("--codes", help="只扫指定票：逗号分隔代码，或 @文件（每行一个 ts_code）")
    ap.add_argument("--csv", help="把结果导出成 CSV")
    ap.add_argument("--limit", type=int, default=60, help="最多列多少行")
    args = ap.parse_args()

    pool = load_pool(args)
    con = sqlite3.connect(DB)
    day = args.date or latest_date(con)
    basic = pd.read_sql_query("SELECT ts_code, name FROM stock_basic", con).set_index("ts_code")
    meta: dict[str, dict] = {}
    if DEFAULT_POOL.exists():
        frame = pd.read_csv(DEFAULT_POOL, dtype=str).fillna("")
        meta = frame.set_index("code").to_dict("index")

    rows: list[dict] = []
    skipped: list[str] = []
    for code in pool:
        bars = load_bars(con, code)
        if bars is None or len(bars) < 90:
            skipped.append(code)
            continue
        name = basic.at[code, "name"] if code in basic.index else meta.get(code, {}).get("name", "")
        category = meta.get(code, {}).get("category", "")
        for hit in scan_one(bars, name, category):
            hit["ts_code"] = code
            rows.append(hit)
    con.close()

    print(f"扫描日 {day}（回看 {args.days} 个交易日）｜关注池 {len(pool)} 只（stocks 站点同步）"
          + (f"，其中 {len(skipped)} 只 K 线不足 90 根（次新股/停牌）跳过" if skipped else ""))
    if skipped:
        print(f"  跳过：{', '.join(skipped)}")
    if not rows:
        print("无信号。")
        return 0

    hits = pd.DataFrame(rows)
    # 窗口 = 全池最近 args.days 个交易日（交易日历取自 daily 表），窗口外的旧信号不列
    con = sqlite3.connect(DB)
    window = [r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM daily WHERE trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT ?", (day, args.days))]
    con.close()
    hits = hits[hits.trade_date.isin(window)]
    if hits.empty:
        print(f"最近 {args.days} 个交易日（{min(window)}~{max(window)}）无信号。")
        return 0
    hits["trade_date"] = pd.to_datetime(hits.trade_date, format="%Y%m%d").dt.strftime("%Y-%m-%d")
    hits = hits.sort_values(["heavy", "trade_date"], ascending=[False, False])

    if args.csv:
        hits.rename(columns=dict(
            ts_code="代码", name="名称", category="分类", trade_date="信号日", close="收盘价",
            stop="参考止损", stop_pct="距止损%", pct_chg="当日涨幅%", rsi="RSI6",
            dd60="距60日高点%", vol5x="量/5日均", to_upper="距上沿%", heavy="重手",
        )).to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"\n已导出 {len(hits)} 行 → {args.csv}")

    n_heavy = int(hits.heavy.sum())
    print(f"命中趋势回踩起爆 {len(hits)} 笔（{hits.ts_code.nunique()} 只票）"
          f"，其中可下重手（距上沿≥{HEAVY_UPPER_PCT:.0f}%）{n_heavy} 笔\n")
    cols = hits.head(args.limit).rename(columns=dict(
        ts_code="代码", name="名称", category="分类", trade_date="信号日", close="收盘价",
        stop="参考止损", stop_pct="距止损%", pct_chg="涨幅%", rsi="RSI6",
        dd60="距60高%", vol5x="量/5均", to_upper="距上沿%", heavy="重手"))
    cols["重手"] = cols["重手"].map(lambda x: "★" if x else "")
    pd.set_option("display.unicode.east_asian_width", True)
    with pd.option_context("display.max_columns", None, "display.width", 220):
        print(cols[["信号日", "代码", "名称", "分类", "收盘价", "参考止损", "距止损%",
                    "涨幅%", "RSI6", "距60高%", "量/5均", "距上沿%", "重手"]].to_string(index=False))
    print("\n口径（2026-09-06）：买入价=信号日收盘（当日涨幅>5% 则次日再买，不追）；"
          "「参考止损」= max(买价×0.90, 最近30根最低价) 收盘触发、逐日滚动（C2 兜底线）；"
          "主出场=盘中摸金牛上沿、收盘收回下方（阴线或长上影）→ 清仓，故「距上沿%」是到清仓价的空间；"
          "空头通道中的票直接不列（长期结构坏）。"
          "\n提醒：本扫描器只盯关注池、不选股；机械执行在无前视池已证伪，edge 在你的选股。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
