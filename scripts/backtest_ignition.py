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


def load_window(con, code: str, start_year: int, end: str) -> pd.DataFrame | None:
    """取 start_year 年初起的那段行情，但指标/信号在前两年预热数据上算好，避免边界失真。"""
    df = load_stock(con, code, f"{start_year - LOOKBACK_YEARS}0101", end)
    if df is None:
        return None
    part = df[df.trade_date.astype(str) >= f"{start_year}0101"].reset_index(drop=True)
    return part if len(part) >= 120 else None


def add_filter_variants(df: pd.DataFrame) -> pd.DataFrame:
    """补上三种买入口径：s0 裸 CROSS、s2 裸+形态、s3 定稿v2（位置+形态）。"""
    from tech_indicators.ignition import ignition_candle_filter, ignition_position_filter
    std = df[["trade_date", "open", "high", "low", "close", "vol", "pct_chg"]].reset_index(drop=True)
    bare = ignition_cross_signal(std).values
    pos = ignition_position_filter(std).fillna(False).values
    candle = ignition_candle_filter(std).fillna(False).values
    out = df.copy()
    out["s0"] = bare
    out["s2"] = bare & candle
    out["s3"] = bare & pos & candle
    return out


def run_single(con, cohorts: list[int], end: str, pool_size: int, min_mv: float, max_mv: float,
               variants: dict[str, str]) -> None:
    args_end = [end]
    """单票串行回测：一只票独占 100 万、一次只持一笔 —— 对应"手动、盯得住几只票"的真实场景。"""
    loaded = []
    for y0 in cohorts:
        for code in make_pool(con, y0, pool_size, min_mv, max_mv):
            df = load_window(con, code, y0, args_end[0])
            if df is not None:
                loaded.append((code, y0, add_filter_variants(df)))
    for label, col in variants.items():
        rows = []
        for code, y0, df in loaded:
            r = simulate_single(df, col)
            if r:
                r["code"], r["cohort"] = code, y0
                r.pop("trade_frame", None)
                rows.append(r)
        if not rows:
            continue
        t = pd.DataFrame(rows)
        print(f"\n=== 单票串行 · {label} · {len(cohorts)} 批 × {pool_size} 只 = {len(t)} 个独立账户 ===")
        print("  （每只票单独 100 万，一次只持一笔；收益不可跨票相加，看分布）")
        q = t.total_pct.quantile
        print(f"  累计收益：中位 {t.total_pct.median():+.1f}%  均值 {t.total_pct.mean():+.1f}%  "
              f"25分位 {q(.25):+.1f}%  75分位 {q(.75):+.1f}%  最差 {t.total_pct.min():+.1f}%  最好 {t.total_pct.max():+.1f}%")
        print(f"  年化：中位 {t.annualized.median():+.1f}%  25分位 {t.annualized.quantile(.25):+.1f}%  "
              f"75分位 {t.annualized.quantile(.75):+.1f}%")
        print(f"  赚钱的票占比 {(t.total_pct > 0).mean() * 100:.0f}%   零信号的票 {(t.trades == 0).sum()} 只   "
              f"中位最大回撤 {t.max_dd.median():.1f}%   25分位回撤 {t.max_dd.quantile(.25):.1f}%   "
              f"中位交易 {t.trades.median():.0f} 笔 / {t.bars.median() / 244:.1f} 年")
        for y0 in cohorts:
            sub = t[t.cohort == y0]
            if len(sub):
                print(f"    {y0}年起做{len(sub):3d}只：中位累计 {sub.total_pct.median():+7.1f}%  "
                      f"中位年化 {sub.annualized.median():+6.1f}%  赚钱占比 {(sub.total_pct>0).mean()*100:3.0f}%  "
                      f"中位回撤 {sub.max_dd.median():6.1f}%  中位笔数 {sub.trades.median():4.0f}")


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


def position_step(a: dict, i: int, p: dict, sig_col: str) -> tuple[bool, bool, str]:
    """推进一根 K 线，返回 (是否全平, 是否减半, 原因)。组合回测与单票回测共用同一套卖出规则。

    a 需含 open/high/low/close/sig_col/upper/bear 数组；p 为持仓字典。
    """
    close = a["close"][i]
    if not np.isfinite(close):
        return False, False, ""
    p["last"] = close
    p["hi"] = max(p["hi"], a["high"][i])
    hi = p["hi"]
    if not p["running"] and hi / p["entry"] - 1 >= IGNITION_RUN_GAIN_PCT:
        p["running"] = True
    if not p["running"] and a[sig_col][i] and i > p["sig_i"]:          # 新起爆点重锚止损
        p["stop"] = max(p["entry"] * (1 - IGNITION_STOP_PCT),
                        float(np.nanmin(a["low"][max(0, i - IGNITION_STOP_BARS + 1):i + 1])))
        p["sig_i"] = i
    if p["running"]:
        if (hi - close) >= IGNITION_TRAIL_FRACTION * (hi - p["entry"]):
            return True, False, "移动止盈"
    elif close < p["stop"]:
        return True, False, "止损"
    if not p["half_reduced"] and a["high"][i] >= a["upper"][i] * IGNITION_UPPER_TOUCH \
            and close < a["upper"][i]:
        body = abs(close - a["open"][i])
        shadow = a["high"][i] - max(close, a["open"][i])
        bearish = close < a["open"][i]
        long_shadow = close >= a["open"][i] and body > 0 and shadow >= 2 * body and shadow >= 0.03 * close
        if bearish or long_shadow:
            if a["bear"][i]:
                return True, False, "上沿压制·熊市清仓"
            return False, True, "上沿压制·减半"
    return False, False, ""


def new_position(a: dict, i: int, alloc: float) -> dict:
    """按同一规则建仓：止损 = max(买价×(1-10%), 起爆点及其前 4 根最低价)。"""
    price = a["close"][i]
    return {"i0": i, "entry": price, "shares": alloc / (price * (1 + FEE)), "last": price,
            "hi": a["high"][i], "running": False, "half_reduced": False, "sig_i": i,
            "stop": max(price * (1 - IGNITION_STOP_PCT),
                        float(np.nanmin(a["low"][max(0, i - IGNITION_STOP_BARS + 1):i + 1])))}


def bar_arrays(df: pd.DataFrame, sig_col: str) -> dict:
    return {col: df[col].values for col in
            ("open", "high", "low", "close", "pct_chg", "trade_date", sig_col)} | {
            "upper": df["upper"].values, "bear": df["bear"].values}


def simulate_single(df: pd.DataFrame, sig_col: str, capital: float = 1_000_000.0) -> dict:
    """单票串行：一只票独占全部资金，一次只持一笔，信号来了就满仓进、按同一套规则出。

    这是"手动交易、一年做不了几笔、只想盯一两只票"的真实场景。
    返回该票这段时期的完整净值曲线与逐笔交易。
    """
    a = bar_arrays(df, sig_col)
    n = len(df)
    cash, pos, pending, trades = capital, None, None, []
    curve = np.full(n, np.nan)
    for i in range(n):
        if pos is not None:
            go, half, reason = position_step(a, i, pos, sig_col)
            if go:
                cash += pos["shares"] * pos["last"] * (1 - FEE - STAMP)
                trades.append(dict(code=str(df.ts_code.iloc[0]) if "ts_code" in df else "",
                                   buy=str(a["trade_date"][pos["i0"]]), sell=str(a["trade_date"][i]),
                                   ret=pos["last"] / pos["entry"] - 1,
                                   pnl=pos["shares"] * (pos["last"] - pos["entry"]),
                                   days=i - pos["i0"], reason=reason))
                pos = None
            elif half:
                cut = pos["shares"] * 0.5
                cash += cut * pos["last"] * (1 - FEE - STAMP)
                pos["shares"] -= cut
                pos["half_reduced"] = True
                trades.append(dict(buy=str(a["trade_date"][pos["i0"]]), sell=str(a["trade_date"][i]),
                                   ret=pos["last"] / pos["entry"] - 1,
                                   pnl=cut * (pos["last"] - pos["entry"]),
                                   days=i - pos["i0"], reason=reason))
        if pending is not None and i >= pending and pos is None:     # 挂单成交
            price = a["close"][i]
            if np.isfinite(price) and price > 0:
                pos = new_position(a, i, cash)
                cash = 0.0
            pending = None

        if pos is None and pending is None and a[sig_col][i] and i < n - 1:
            if a["pct_chg"][i] > 5:                                   # 起爆日涨>5% 不追，次日买
                pending = i + 1
            else:
                pos = new_position(a, i, cash)
                cash = 0.0

        curve[i] = cash + (pos["shares"] * pos["last"] if pos else 0.0)
    if pos is not None:                                      # 期末（含退市）平仓
        cash += pos["shares"] * pos["last"] * (1 - FEE - STAMP)
        trades.append(dict(buy=str(a["trade_date"][pos["i0"]]), sell=str(a["trade_date"][n - 1]),
                           ret=pos["last"] / pos["entry"] - 1,
                           pnl=pos["shares"] * (pos["last"] - pos["entry"]),
                           days=n - 1 - pos["i0"], reason="期末平仓"))
        curve[n - 1] = cash
    series = pd.Series(curve, index=df.trade_date.values).dropna()
    if series.empty:
        return None
    dd = float(((series / series.cummax()) - 1).min())
    tdf = pd.DataFrame(trades)
    years = max(len(series) / 244, 0.5)
    total = float(series.iloc[-1] / capital - 1)
    return dict(total_pct=round(total * 100, 1), annualized=round(((1 + total) ** (1 / years) - 1) * 100, 1),
                max_dd=round(dd * 100, 1), trades=len(tdf),
                win=round(float((tdf.ret > 0).mean() * 100), 1) if len(tdf) else 0.0,
                worst=round(float(tdf.ret.min() * 100), 1) if len(tdf) else 0.0,
                bars=len(series), trade_frame=tdf)


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
            held[code] = new_position(a, i, alloc)

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
            p = held[code]
            go, half, reason = position_step(a, i, p, sig_col)
            if go:
                exit_px = p["last"]
                cash += p["shares"] * exit_px * (1 - FEE - STAMP)
                trades.append(dict(code=code, buy=str(a["trade_date"][p["i0"]]), sell=day,
                                   ret=exit_px / p["entry"] - 1,
                                   pnl=p["shares"] * (exit_px - p["entry"]), days=i - p["i0"], reason=reason))
                del held[code]
            elif half:
                close = p["last"]
                cut = p["shares"] * 0.5
                cash += cut * close * (1 - FEE - STAMP)
                p["shares"] -= cut
                p["half_reduced"] = True
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
    ap.add_argument("--mode", choices=["portfolio", "single"], default="portfolio",
                    help="portfolio=组合资金池；single=每只票独立100万串行（手动交易场景）")
    ap.add_argument("--cohorts", type=int, nargs="*", default=[2018, 2020, 2022, 2024],
                    help="single 模式：从哪些年份各选一批票一直做到样本末")
    ap.add_argument("--end", default="20260903")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.mode == "single":
        con = sqlite3.connect(DB)
        try:
            run_single(con, args.cohorts, args.end, args.pool_size, args.min_mv, args.max_mv,
                       {"裸 CROSS(RSI6,40)": "s0", "裸+形态过滤": "s2", "定稿v2(位置+形态)": "s3"})
        finally:
            con.close()
        return 0

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
