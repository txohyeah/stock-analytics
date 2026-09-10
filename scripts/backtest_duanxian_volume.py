#!/usr/bin/env python3
"""短线是银量能买入信号回测 —— 事件研究法（先只看买入信号）。

买入信号（逐日向量化，口径与 tech_indicators.indicators 的 duanxian_auxiliary 一致）：
  - 量托 volume_support_triangle
  - 芝麻量 sesame_volume
  - OBV金叉 obv_golden_cross
  - 多方炮 bullish_sandwich
  - 组合 any_volume（四者任一）

卖出（三种独立口径，全部卖出）：
  - f5  ：买入后第 5 个有效交易日收盘卖出
  - f20 ：买入后第 20 个有效交易日收盘卖出
  - 金牛上沿：T+1 起第一个收盘价 >= 金牛上沿(causal) 的日子收盘卖出；
              样本末仍未触及则期末平仓（单独标注未触及比例）

统计：笔数、平均/中位收益、胜率、最差/最好、平均持有天数。
价格全部前复权；默认不含手续费（先看信号本身质量，--fee 可加）。

用法：
    cd /home/application/stock-analytics
    ./venv/bin/python scripts/backtest_duanxian_volume.py --years 2024 --pool-size 60   # 快速验证
    ./venv/bin/python scripts/backtest_duanxian_volume.py --start-year 2018 --end-year 2026 --pool-size 200
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

from tech_indicators.indicators import compute_golden_bull_lines  # noqa: E402
from backtest_ignition import load_stock, make_pool  # noqa: E402

DB = ROOT / "data" / "stock.db"
PREHEAT_START = "20170101"   # daily 最早 2017-01-03，够 OBV MA100 / 量托 40 日窗口预热
FEE = 0.001
STAMP = 0.0005
POOL_CACHE = ROOT / "data" / "pool_cache"


def cached_make_pool(con, year: int, size: int, min_mv: float, max_mv: float) -> list[str]:
    """make_pool 结果缓存（全表 GROUP BY 每次 ~40s，9 年池子只算一次）。"""
    POOL_CACHE.mkdir(exist_ok=True)
    cache_file = POOL_CACHE / f"pool_{year}_{size}_{int(min_mv)}.csv"
    if cache_file.exists():
        codes = pd.read_csv(cache_file)["ts_code"].tolist()
        if len(codes) == size:
            return codes
    codes = make_pool(con, year, size, min_mv, max_mv)
    pd.DataFrame({"ts_code": codes}).to_csv(cache_file, index=False)
    return codes


def volume_confirm_signals(df: pd.DataFrame) -> pd.DataFrame:
    """逐日向量化量能确认信号（量托 + OBV金叉），口径与 indicators.py 的 duanxian_auxiliary 一致。

    只含量能部分，不含金牛上沿（调用方按需另算）。回测与扫描器共用，保证同一套判定。
    """
    out = pd.DataFrame(index=df.index)
    close = pd.to_numeric(df["close"], errors="coerce")
    vol = pd.to_numeric(df["vol"], errors="coerce")

    v5 = vol.rolling(5).mean()
    v10 = vol.rolling(10).mean()
    v20 = vol.rolling(20).mean()

    # ---- 量托：20日内三个均量金叉 + 出现过空头排列 + 当前多头排列 ----
    cross_5_10 = (v5 > v10) & ~(v5.shift(1) > v10.shift(1))
    cross_5_20 = (v5 > v20) & ~(v5.shift(1) > v20.shift(1))
    cross_10_20 = (v10 > v20) & ~(v10.shift(1) > v20.shift(1))
    bear_order = (v20 > v10) & (v10 > v5)
    bull_order = (v5 > v10) & (v10 > v20)
    out["sig_liangtuo"] = (
        bull_order.fillna(False)
        & cross_5_10.rolling(20, min_periods=1).max().fillna(0).astype(bool)
        & cross_5_20.rolling(20, min_periods=1).max().fillna(0).astype(bool)
        & cross_10_20.rolling(20, min_periods=1).max().fillna(0).astype(bool)
        & bear_order.rolling(20, min_periods=1).max().fillna(0).astype(bool)
    )

    # ---- OBV金叉：OBV 上穿 OBV_MA100（MA100 预热 min_periods=20，与原函数一致）----
    direction = np.sign(close.diff()).fillna(0)
    obv = (direction * vol).cumsum()
    obv_ma100 = obv.rolling(100, min_periods=20).mean()
    out["sig_obv"] = ((obv > obv_ma100) & (obv.shift(1) <= obv_ma100.shift(1))).fillna(False)
    return out


def volume_signals(df: pd.DataFrame) -> pd.DataFrame:
    """逐日向量化 4 个量能买入信号 + 金牛上沿(causal)。口径对齐 indicators.py 的 duanxian_auxiliary。"""
    out = volume_confirm_signals(df)
    close = pd.to_numeric(df["close"], errors="coerce")
    vol = pd.to_numeric(df["vol"], errors="coerce")
    pct = pd.to_numeric(df["pct_chg"], errors="coerce")

    v20 = vol.rolling(20).mean()
    ma20 = close.rolling(20).mean()

    # ---- 芝麻量：量 <= MA20量*0.55 且 <= 20日量分位0.2，价不破MA20，波动温和 ----
    vol_q20 = vol.rolling(20).quantile(0.2)
    out["sig_zhima"] = (
        (vol <= v20 * 0.55)
        & (vol <= vol_q20)
        & (close >= ma20 * 0.98)
        & (pct.abs() <= 3.0)
    ).fillna(False)

    # ---- 多方炮：阳-阴(或小实体)-阳，第三根放量收复第一根 ----
    a_open, a_close = df["open"].shift(2), close.shift(2)
    b_open, b_close = df["open"].shift(1), close.shift(1)
    c_open, c_close = df["open"], close
    avg_vol = vol.rolling(5).mean().shift(4)          # 最近3根之前的前5根均量
    first_yang = a_close > a_open
    middle_rest = (b_close <= b_open) | ((b_close - b_open).abs() <= (a_close - a_open).abs() * 0.5)
    third_yang = c_close > c_open
    reclaim = c_close >= pd.concat([a_open, a_close], axis=1).max(axis=1)
    vol_confirm = avg_vol.notna() & (avg_vol > 0) & (vol >= avg_vol * 1.1)
    out["sig_duofangpao"] = (first_yang & middle_rest & third_yang & reclaim & vol_confirm).fillna(False)

    out["sig_any"] = out[["sig_liangtuo", "sig_zhima", "sig_obv", "sig_duofangpao"]].any(axis=1)

    # ---- 金牛上沿（严格因果版）----
    lines = compute_golden_bull_lines(df, causal=True)
    out["upper"] = lines["channel_upper"]
    return out


def event_study(df: pd.DataFrame, sig_col: str, upper_col: str = "upper", cooldown: int = 0) -> list[dict]:
    """对每个信号日做事件研究：f5 / f20 / 金牛上沿三种卖出收益。

    cooldown>0 时，同一只票距上一个已计数信号不足 cooldown 个交易日的信号跳过
    （避免同一波行情连续触发导致的样本重叠）。
    """
    n = len(df)
    close = pd.to_numeric(df["close"], errors="coerce").values
    upper = pd.to_numeric(df[upper_col], errors="coerce").values
    dates = df["trade_date"].astype(str).values
    sig = df[sig_col].fillna(False).values
    events: list[dict] = []
    last_counted = -10**9
    for i in range(n):
        if not sig[i]:
            continue
        if cooldown and i - last_counted < cooldown:
            continue
        entry = close[i]
        if not np.isfinite(entry) or entry <= 0:
            continue

        def forward_ret(target: int) -> tuple[float | None, int | None]:
            cnt, j = 0, i + 1
            while j < n and cnt < target:
                if np.isfinite(close[j]):
                    cnt += 1
                    if cnt == target:
                        return close[j] / entry - 1, j - i
                j += 1
            return None, None

        f5_ret, f5_days = forward_ret(5)
        f20_ret, f20_days = forward_ret(20)

        upper_ret, upper_days, upper_hit = None, None, False
        j = i + 1
        while j < n:
            if np.isfinite(close[j]) and np.isfinite(upper[j]):
                if close[j] >= upper[j]:
                    upper_ret, upper_days, upper_hit = close[j] / entry - 1, j - i, True
                    break
            j += 1
        if not upper_hit:                       # 样本末未触及上沿 → 期末平仓
            k = n - 1
            while k > i and not np.isfinite(close[k]):
                k -= 1
            if k > i:
                upper_ret, upper_days = close[k] / entry - 1, k - i

        events.append(dict(date=dates[i], f5=f5_ret, f5_days=f5_days,
                           f20=f20_ret, f20_days=f20_days,
                           upper=upper_ret, upper_days=upper_days, upper_hit=upper_hit))
        last_counted = i
    return events


def summarize(events: list[dict], key: str, days_key: str, fee: float = 0.0) -> dict:
    vals = [e[key] for e in events if e[key] is not None]
    if not vals:
        return {"n": 0}
    arr = np.array(vals)
    if fee:
        arr = arr - fee - STAMP
    days = [e[days_key] for e in events if e[key] is not None and e[days_key] is not None]
    return {
        "n": len(arr),
        "mean": float(arr.mean() * 100),
        "median": float(np.median(arr) * 100),
        "win": float((arr > 0).mean() * 100),
        "worst": float(arr.min() * 100),
        "best": float(arr.max() * 100),
        "avg_days": float(np.mean(days)) if days else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="短线是银量能买入信号回测（事件研究）")
    ap.add_argument("--start-year", type=int, default=2018)
    ap.add_argument("--end-year", type=int, default=2026)
    ap.add_argument("--years", type=int, nargs="*", help="只跑指定年份（覆盖 start/end）")
    ap.add_argument("--pool-size", type=int, default=200)
    ap.add_argument("--min-mv", type=float, default=800000, help="流通市值下限（万元）")
    ap.add_argument("--max-mv", type=float, default=1e12)
    ap.add_argument("--end", default="20260909")
    ap.add_argument("--cooldown", type=int, default=0, help="同票信号冷却期（交易日），0=不冷却")
    ap.add_argument("--fee", action="store_true", help="收益扣双边费用（万1佣金+万0.5印花税）")
    ap.add_argument("--csv", help="导出逐笔事件 CSV")
    args = ap.parse_args()

    years = args.years or list(range(args.start_year, args.end_year + 1))
    sig_labels = {
        "sig_liangtuo": "量托",
        "sig_zhima": "芝麻量",
        "sig_obv": "OBV金叉",
        "sig_duofangpao": "多方炮",
        "sig_any": "任一量能信号",
    }
    exit_labels = {"f5": "f5卖出", "f20": "f20卖出", "upper": "金牛上沿卖出"}

    con = sqlite3.connect(DB)
    all_events: dict[str, list[dict]] = {k: [] for k in sig_labels}
    per_year: dict[str, dict[str, int]] = {}
    try:
        for year in years:
            pool = cached_make_pool(con, year, args.pool_size, args.min_mv, args.max_mv)
            if len(pool) < 10:
                print(f"  {year} 池子只有 {len(pool)} 只，跳过")
                continue
            ycount = {k: 0 for k in sig_labels}
            for code in pool:
                df = load_stock(con, code, PREHEAT_START, args.end)
                if df is None:
                    continue
                sig = volume_signals(df)
                df = df.reset_index(drop=True)
                sig = sig.reset_index(drop=True)
                lo, hi = f"{year}0101", f"{year + 1}0101"
                full = df.copy()
                for skey in sig_labels:
                    full["_sig"] = sig[skey].values
                    full["_upper"] = sig["upper"].values
                    ev = event_study(full, "_sig", "_upper", cooldown=args.cooldown)
                    ev = [e for e in ev if lo <= e["date"] < hi]   # 只统计当年信号日
                    all_events[skey].extend(ev)
                    ycount[skey] += len(ev)
            per_year[year] = ycount
            print(f"  {year}: " + "  ".join(f"{sig_labels[k]}{v}" for k, v in ycount.items()))
    finally:
        con.close()

    print("\n" + "=" * 100)
    print(f"短线是银量能买入信号回测（事件研究）· {years[0]}-{years[-1]} · 池 {args.pool_size} 只/年 · "
          f"冷却 {args.cooldown} 天 · {'含费' if args.fee else '不含费'}")
    print("=" * 100)
    rows = []
    for skey, label in sig_labels.items():
        evs = all_events[skey]
        for ekey, elabel in exit_labels.items():
            s = summarize(evs, ekey, ekey + "_days", fee=FEE if args.fee else 0.0)
            if s["n"] == 0:
                continue
            rows.append((label, elabel, s))
            print(f"\n【{label}】×【{elabel}】  {s['n']} 笔")
            print(f"  平均收益 {s['mean']:+.2f}%   中位 {s['median']:+.2f}%   胜率 {s['win']:.1f}%")
            print(f"  最差 {s['worst']:+.1f}%   最好 {s['best']:+.1f}%   平均持有 {s['avg_days']:.1f} 天")
        if ekey == "upper" and evs:
            hit = sum(1 for e in evs if e["upper_hit"])
            print(f"  （金牛上沿口径：触及上沿卖出 {hit}/{len(evs)} = {hit / len(evs) * 100:.1f}%，"
                  f"其余为样本末平仓）")

    if args.csv:
        out_rows = []
        for skey, label in sig_labels.items():
            for e in all_events[skey]:
                out_rows.append({"信号": label, "信号日": e["date"], "f5收益%": e["f5"],
                                 "f20收益%": e["f20"], "上沿收益%": e["upper"],
                                 "上沿持有天数": e["upper_days"], "触及上沿": e["upper_hit"]})
        pd.DataFrame(out_rows).to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"\n逐笔事件已导出：{args.csv}（{len(out_rows)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())