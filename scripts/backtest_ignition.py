#!/usr/bin/env python3
"""起爆点策略组合级回测 —— 信号与参数一律取自 tech_indicators.ignition，杜绝"回测一套、实盘另一套"。

设计要点：
1. 买点由库函数给出：`ignition_signal_series`（定稿 v2，含三道买入过滤）
   或 `ignition_cross_signal`（裸 CROSS(RSI6,40)，用于看过滤的边际贡献）。
2. 卖出参数（止损比例/重锚窗口/利润奔跑门槛/移动止盈比例/上沿容差）全部读库里的常量，
   不在本脚本里重复定义数字。
3. 票池按"当年时点"逐年重建：用 Y-1 年的成交额与 Y-1 年末流通市值筛选，
   含退市股（需先跑 scripts/backfill_delisted_basic.py，否则有幸存者偏差）。
4. 组合级单一资金池：每笔 = 当时权益 × frac，最多 slot 只，满槽则丢弃信号。
5. 价格用前复权（close × adj_factor / 末值）；RSI 与位置过滤都在复权价上算。

用法：
    cd /home/application/stock-analytics
    ./venv/bin/python scripts/backtest_ignition.py --start-year 2018 --end-year 2026
    ./venv/bin/python scripts/backtest_ignition.py --years 2018 2022 --variant both --trades-out /tmp/t.csv
    ./venv/bin/python scripts/backtest_ignition.py --start-year 2024 --frac 0.2 --slot 5
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tech_indicators.ignition import (  # noqa: E402
    IGNITION_RUN_GAIN_PCT,
    IGNITION_STOP_BARS,
    IGNITION_STOP_PCT,
    IGNITION_TRAIL_FRACTION,
    IGNITION_UPPER_TOUCH,
    golden_channel_state,
    ignition_cross_signal,
    ignition_signal_series,
)

DB = ROOT / "data" / "stock.db"
FEE = 0.001
STAMP = 0.0005
LOOKBACK_YEARS = 2          # 复权因子与滚动指标需要的预热长度


def load_stock(con, code: str, start: str, end: str) -> pd.DataFrame | None:
    """读日线并前复权，附上金牛通道状态与库给出的两套信号。"""
    bars = pd.read_sql_query(
        "SELECT trade_date,open,high,low,close,pct_chg,vol FROM daily "
        "WHERE ts_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        con, params=(code, start, end),
    )
    if len(bars) < 150:
        return None
    fac = pd.read_sql_query(
        "SELECT trade_date,adj_factor FROM adj_factor WHERE ts_code=? ORDER BY trade_date",
        con, params=(code,),
    )
    if fac.empty:
        return None
    bars = bars.merge(fac, on="trade_date", how="left")
    bars["adj_factor"] = bars["adj_factor"].ffill().bfill()
    if bars["adj_factor"].isna().any():
        return None
    k = bars["adj_factor"] / bars["adj_factor"].iloc[-1]
    std = pd.DataFrame({"trade_date": bars["trade_date"].values})
    for col in ("open", "high", "low", "close"):
        std[col] = (bars[col] * k).astype("float64").values
    std["vol"] = bars["vol"].values
    std["pct_chg"] = bars["pct_chg"].values

    # 信号一律来自库；本脚本不重复实现任何判据
    std["sig_v2"] = ignition_signal_series(std).values
    std["sig_bare"] = ignition_cross_signal(std).values
    channel = golden_channel_state(std)
    std["upper"] = channel["upper"].values
    std["bear"] = channel["bear"].values
    return std


def make_pool(con, year: int, size: int, min_mv: float, max_mv: float) -> list[str]:
    """按 Y-1 年时点选池（无前视），含退市股。"""
    prior = str(year - 1)
    amt = pd.read_sql_query(
        "SELECT ts_code, AVG(amount) a FROM daily WHERE trade_date BETWEEN ? AND ? GROUP BY ts_code",
        con, params=(prior + "0101", prior + "1231"),
    )
    mv = pd.read_sql_query(
        "SELECT ts_code, circ_mv FROM daily_basic WHERE trade_date = "
        "(SELECT MAX(trade_date) FROM daily_basic WHERE trade_date <= ?)",
        con, params=(prior + "1231",),
    )
    basic = pd.read_sql_query(
        "SELECT ts_code, name, list_date FROM stock_basic WHERE name NOT LIKE '%ST%'", con
    )
    universe = basic.merge(amt, on="ts_code").merge(mv, on="ts_code", how="left")
    universe = universe[universe.list_date <= f"{prior}-01-01"]        # 年初即上市满一年
    universe = universe[(universe.circ_mv >= min_mv) & (universe.circ_mv <= max_mv)]
    return universe.sort_values("a", ascending=False).head(size).ts_code.tolist()


def simulate(bars: dict[str, pd.DataFrame], year: int, sig_col: str, frac: float, slot: int) -> dict:
    """单年组合模拟：bars 为 code -> 已含信号的 DataFrame。"""
    dates = sorted({d for df in bars.values() for d in df.trade_date if str(d).startswith(str(year))})
    if not dates:
        return None
    look = {c: {df.trade_date[i]: i for i in range(len(df))} for c, df in bars.items()}
    arr = {c: {col: df[col].values for col in
               ("open", "high", "low", "close", "pct_chg", sig_col, "upper", "bear", "trade_date")}
           for c, df in bars.items()}

    cash = 1_000_000.0
    held: dict[str, dict] = {}
    pending: list[tuple[str, int]] = []
    trades: list[dict] = []
    curve = []

    for t_idx, day in enumerate(dates):
        def equity() -> float:
            return sum(p["shares"] * p["last"] for p in held.values())

        def open_position(code: str, i: int) -> None:
            nonlocal cash
            a = arr[code]
            price = a["close"][i]
            alloc = min(frac * (cash + equity()), cash)
            if alloc < 1e4 or not np.isfinite(price) or price <= 0:
                return
            cash -= alloc
            held[code] = {
                "i0": i, "entry": price, "shares": alloc / (price * (1 + FEE)), "last": price,
                "hi": a["high"][i], "running": False, "half": False,
                "stop": max(price * (1 - IGNITION_STOP_PCT),
                            float(np.nanmin(a["low"][max(0, i - IGNITION_STOP_BARS + 1):i + 1]))),
                "sig_i": i,
            }

        for code, when in list(pending):
            if day < when:
                continue
            pending.remove((code, when))
            i = look[code].get(day)
            if code in held or len(held) >= slot or i is None:
                continue
            open_position(code, i)

        for code in list(held):
            a = arr[code]
            i = look[code].get(day)
            if i is None:
                continue
            close = a["close"][i]
            if not np.isfinite(close):
                continue
            p = held[code]
            p["last"] = close
            p["hi"] = max(p["hi"], a["high"][i])
            hi = p["hi"]
            if not p["running"] and hi / p["entry"] - 1 >= IGNITION_RUN_GAIN_PCT:
                p["running"] = True
            if not p["running"] and a[sig_col][i] and i > p["sig_i"]:
                p["stop"] = max(p["entry"] * (1 - IGNITION_STOP_PCT),
                                float(np.nanmin(a["low"][max(0, i - IGNITION_STOP_BARS + 1):i + 1])))
                p["sig_i"] = i

            exit_px = None
            reason = None
            if p["running"]:
                if (hi - close) >= IGNITION_TRAIL_FRACTION * (hi - p["entry"]):
                    exit_px, reason = close, "移动止盈"
            elif close < p["stop"]:
                exit_px, reason = close, "止损"

            half_now = False
            if exit_px is None and not p["half"] \
                    and a["high"][i] >= a["upper"][i] * IGNITION_UPPER_TOUCH and close < a["upper"][i]:
                body = abs(close - a["open"][i])
                shadow = a["high"][i] - max(close, a["open"][i])
                bearish = close < a["open"][i]
                long_shadow = close >= a["open"][i] and body > 0 and shadow >= 2 * body and shadow >= 0.03 * close
                if bearish or long_shadow:
                    if bool(a["bear"][i]):
                        exit_px, reason = close, "上沿压制·熊市清仓"
                    else:
                        half_now = True
                        reason = "上沿压制·减半"

            if exit_px is not None:
                cash += p["shares"] * exit_px * (1 - FEE - STAMP)
                trades.append(dict(code=code, buy=str(a["trade_date"][p["i0"]]), sell=day,
                                   ret=exit_px / p["entry"] - 1,
                                   pnl=p["shares"] * (exit_px - p["entry"]), days=i - p["i0"], reason=reason))
                del held[code]
            elif half_now:
                cut = p["shares"] * 0.5
                cash += cut * close * (1 - FEE - STAMP)
                p["shares"] -= cut
                p["half"] = True
                trades.append(dict(code=code, buy=str(a["trade_date"][p["i0"]]), sell=day,
                                   ret=close / p["entry"] - 1,
                                   pnl=cut * (close - p["entry"]), days=i - p["i0"], reason=reason))

        for code in arr:
            a = arr[code]
            i = look[code].get(day)
            if i is None or code in held or len(held) >= slot:
                continue
            if not a[sig_col][i]:
                continue
            if a["pct_chg"][i] > 5:
                if t_idx + 1 < len(dates):
                    pending.append((code, dates[t_idx + 1]))
            else:
                open_position(code, i)

        curve.append(cash + equity())

    for code, p in list(held.items()):
        cash += p["shares"] * p["last"] * (1 - FEE - STAMP)
        a = arr[code]
        trades.append(dict(code=code, buy=str(a["trade_date"][p["i0"]]), sell=dates[-1],
                           ret=p["last"] / p["entry"] - 1,
                           pnl=p["shares"] * (p["last"] - p["entry"]),
                           days=len(a["trade_date"]) - 1 - p["i0"], reason="期末平仓"))
    curve[-1] = cash

    series = pd.Series(curve, index=dates)
    rets = series.pct_change().dropna()
    tdf = pd.DataFrame(trades)
    return dict(
        year=year, ret=round(float((series.iloc[-1] / 1_000_000 - 1) * 100), 2),
        max_dd=round(float(((series / series.cummax()) - 1).min() * 100), 2),
        sharpe=round(float(rets.mean() / rets.std() * np.sqrt(244)), 2) if rets.std() > 0 else 0.0,
        trades=len(tdf), win=round(float((tdf.ret > 0).mean() * 100), 1) if len(tdf) else 0.0,
        worst=round(float(tdf.ret.min() * 100), 1) if len(tdf) else 0.0,
        avg_hold=round(float(tdf.days.mean()), 1) if len(tdf) else 0.0,
        trade_frame=tdf,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="起爆点策略组合级回测（信号取自 tech_indicators.ignition）")
    ap.add_argument("--start-year", type=int, default=2018)
    ap.add_argument("--end-year", type=int, default=2026)
    ap.add_argument("--years", type=int, nargs="*", help="只跑指定年份（覆盖 start/end）")
    ap.add_argument("--pool-size", type=int, default=60)
    ap.add_argument("--min-mv", type=float, default=800000, help="流通市值下限（万元）")
    ap.add_argument("--max-mv", type=float, default=1e12)
    ap.add_argument("--frac", type=float, default=0.10, help="每笔占当时权益比例")
    ap.add_argument("--slot", type=int, default=10, help="最大同时持仓数")
    ap.add_argument("--variant", choices=["v2", "bare", "both"], default="v2")
    ap.add_argument("--trades-out", help="把逐笔交易导出成 CSV（仅最后一个变体）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    years = args.years or list(range(args.start_year, args.end_year + 1))
    cols = {"v2": ["sig_v2"], "bare": ["sig_bare"], "both": ["sig_v2", "sig_bare"]}[args.variant]
    labels = {"sig_v2": "定稿v2(含买入过滤)", "sig_bare": "裸CROSS(RSI6,40)"}
    con = sqlite3.connect(DB)
    out = {}
    for col in cols:
        rows = []
        for year in years:
            pool = make_pool(con, year, args.pool_size, args.min_mv, args.max_mv)
            if len(pool) < 10:
                print(f"  {year} 池子只有 {len(pool)} 只，跳过（先跑 scripts/backfill_delisted_basic.py）")
                continue
            lo, hi = f"{max(year - LOOKBACK_YEARS, 2017)}0101", f"{year}1231"
            bars = {}
            for code in pool:
                df = load_stock(con, code, lo, hi)
                if df is not None:
                    bars[code] = df
            if not bars:
                continue
            r = simulate(bars, year, col, args.frac, args.slot)
            if r:
                tdf = r.pop("trade_frame")
                if args.trades_out and col == cols[-1]:
                    tdf.to_csv(args.trades_out, index=False)
                rows.append(r)
        if not rows:
            continue
        cum = float(np.prod([1 + x["ret"] / 100 for x in rows]))
        out[labels[col]] = dict(
            yearly=rows, cum_pct=round((cum - 1) * 100, 1),
            annualized=round((cum ** (1 / len(rows)) - 1) * 100, 2),
            worst_year=min(x["ret"] for x in rows), trades=sum(x["trades"] for x in rows),
        )
    con.close()

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        for label, blk in out.items():
            print(f"\n=== {label} · 每笔{args.frac:.0%}×{args.slot}槽 · 池{args.pool_size}只 ===")
            for x in blk["yearly"]:
                print(f"  {x['year']}: 收益{x['ret']:+7.2f}%  年内回撤{x['max_dd']:6.2f}%  夏普{x['sharpe']:5.2f}  "
                      f"{x['trades']:3d}笔  胜率{x['win']:5.1f}%  最差单笔{x['worst']:+6.1f}%  均持{x['avg_hold']:5.1f}天")
            print(f"  → 累计 {blk['cum_pct']:+.1f}%  年化 {blk['annualized']:+.2f}%  "
                  f"最差单年 {blk['worst_year']:+.1f}%  共 {blk['trades']} 笔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
