#!/usr/bin/env python3
"""量能信号 × 价格信号 共振回测（事件研究法）。

价格信号（库内因果口径）：
  - 超跌起爆 sig_chaodie  = ignition_signal_series（定稿 v2：RSI6上穿40+超跌位置+形态）
  - 回踩起爆 sig_huicai   = trend_pullback_signal_series（用户三条件版：RSI6上穿40+放量或触趋势线+非空头通道）

共振定义（窗口 3 个交易日，含当天）：
  res[t] = (量能信号在 [t-3,t] 内触发过) AND (价格信号在 [t-3,t] 内触发过) AND (t 当天有新信号触发)
  → 买入日 = 第二个信号出现的那天（收盘买入）

卖出（3 主卖出 × 3 止损，止损优先，先触发哪个算哪个）：
  主卖出：f5（第5个有效交易日收盘）/ f20（第20个）/ 金牛上沿（收盘≥上沿causal）
  止损：无 / 结构5根（买入日之前5根K线最低价，不含买入日）/ 硬10%（买入价×0.90）
  触发：收盘价跌破止损线（收盘触发，非盘中）

用法：
    cd /home/application/stock-analytics
    ./venv/bin/python scripts/backtest_resonance.py --years 2024 --pool-size 60   # 快速验证
    ./venv/bin/python scripts/backtest_resonance.py --start-year 2018 --end-year 2026 --pool-size 200
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
    ignition_signal_series,
    trend_pullback_signal_series,
)
from backtest_ignition import load_stock  # noqa: E402
from backtest_duanxian_volume import cached_make_pool, volume_signals  # noqa: E402

DB = ROOT / "data" / "stock.db"
PREHEAT_START = "20170101"
FEE = 0.001
STAMP = 0.0005
RESONANCE_WINDOW = 4          # [t-3, t] 共 4 天，即"3 个交易日内（含当天）"


def price_signals(df: pd.DataFrame) -> pd.DataFrame:
    """价格信号：超跌起爆 + 回踩起爆（库内函数，因果口径）。"""
    out = pd.DataFrame(index=df.index)
    out["sig_chaodie"] = ignition_signal_series(df).fillna(False).values
    out["sig_huicai"] = trend_pullback_signal_series(df).fillna(False).values
    out["sig_any_price"] = out[["sig_chaodie", "sig_huicai"]].any(axis=1)
    return out


def resonance_series(vol_sig: pd.Series, price_sig: pd.Series, window: int = RESONANCE_WINDOW,
                     mode: str = "symmetric") -> pd.Series:
    """共振信号。

    mode=symmetric：两信号在 window 天内都触发过，且当天有新触发（买入日=第二个信号日）。
    mode=confirm  ：价格信号为主，当天价格触发 + window 天窗口内有量能确认（买入日=价格信号日，
                    只会过滤不会换入量能日）。
    """
    vol_win = vol_sig.rolling(window, min_periods=1).max().fillna(0).astype(bool)
    price_win = price_sig.rolling(window, min_periods=1).max().fillna(0).astype(bool)
    if mode == "confirm":
        return (price_sig.fillna(False) & vol_win).fillna(False)
    new_trigger = (vol_sig | price_sig).fillna(False)
    return (vol_win & price_win & new_trigger).fillna(False)


def event_study(df: pd.DataFrame, sig_col: str, upper_col: str = "upper",
                stop_mode: str = "none", cooldown: int = 20) -> list[dict]:
    """事件研究：f5 / f20 / 金牛上沿 三种主卖出 × 止损（止损优先）。

    stop_mode: none=无止损 / struct5=买入日之前5根最低价 / hard10=买价×0.90
    """
    n = len(df)
    close = pd.to_numeric(df["close"], errors="coerce").values
    low = pd.to_numeric(df["low"], errors="coerce").values
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
        if stop_mode == "struct5":
            stop_line = float(np.nanmin(low[max(0, i - 5):i])) if i >= 5 else -np.inf
        elif stop_mode == "hard10":
            stop_line = entry * 0.90
        else:
            stop_line = -np.inf

        def forward(target: int) -> tuple[float | None, int | None, bool]:
            """最多持有 target 个有效交易日；期间收盘<止损线则提前止损。"""
            cnt, j = 0, i + 1
            while j < n and cnt < target:
                if np.isfinite(close[j]):
                    cnt += 1
                    if np.isfinite(stop_line) and close[j] < stop_line:
                        return close[j] / entry - 1, j - i, True
                    if cnt == target:
                        return close[j] / entry - 1, j - i, False
                j += 1
            return None, None, False

        f5_ret, f5_days, f5_stopped = forward(5)
        f20_ret, f20_days, f20_stopped = forward(20)

        upper_ret, upper_days, upper_hit, upper_stopped = None, None, False, False
        j = i + 1
        while j < n:
            if np.isfinite(close[j]):
                if np.isfinite(stop_line) and close[j] < stop_line:
                    upper_ret, upper_days, upper_stopped = close[j] / entry - 1, j - i, True
                    break
                if np.isfinite(upper[j]) and close[j] >= upper[j]:
                    upper_ret, upper_days, upper_hit = close[j] / entry - 1, j - i, True
                    break
            j += 1
        if upper_ret is None:                       # 样本末未触发任何卖出 → 期末平仓
            k = n - 1
            while k > i and not np.isfinite(close[k]):
                k -= 1
            if k > i:
                upper_ret, upper_days = close[k] / entry - 1, k - i

        events.append(dict(date=dates[i], f5=f5_ret, f5_days=f5_days, f5_stopped=f5_stopped,
                           f20=f20_ret, f20_days=f20_days, f20_stopped=f20_stopped,
                           upper=upper_ret, upper_days=upper_days, upper_hit=upper_hit,
                           upper_stopped=upper_stopped))
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
    stopped = [e for e in events if e[key] is not None and e.get(key + "_stopped")]
    return {
        "n": len(arr),
        "mean": float(arr.mean() * 100),
        "median": float(np.median(arr) * 100),
        "win": float((arr > 0).mean() * 100),
        "worst": float(arr.min() * 100),
        "best": float(arr.max() * 100),
        "avg_days": float(np.mean(days)) if days else 0.0,
        "stopped": len(stopped),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="量能×价格 共振回测（事件研究）")
    ap.add_argument("--start-year", type=int, default=2018)
    ap.add_argument("--end-year", type=int, default=2026)
    ap.add_argument("--years", type=int, nargs="*")
    ap.add_argument("--pool-size", type=int, default=200)
    ap.add_argument("--min-mv", type=float, default=800000)
    ap.add_argument("--max-mv", type=float, default=1e12)
    ap.add_argument("--end", default="20260909")
    ap.add_argument("--cooldown", type=int, default=20)
    ap.add_argument("--mode", choices=["symmetric", "confirm"], default="symmetric",
                    help="symmetric=对称窗口(买入日=第二个信号日) / confirm=价格为主+量能确认(买入日=价格信号日)")
    ap.add_argument("--fee", action="store_true")
    ap.add_argument("--csv", help="导出全部组合逐笔/汇总 CSV")
    args = ap.parse_args()

    years = args.years or list(range(args.start_year, args.end_year + 1))
    vol_labels = {"sig_liangtuo": "量托", "sig_zhima": "芝麻量", "sig_obv": "OBV金叉",
                  "sig_duofangpao": "多方炮", "sig_any": "任一量能",
                  "sig_always": "恒真(价格单独)"}
    price_labels = {"sig_chaodie": "超跌起爆", "sig_huicai": "回踩起爆", "sig_any_price": "任一价格"}
    exit_labels = {"f5": "f5", "f20": "f20", "upper": "金牛上沿"}
    stop_labels = {"none": "无止损", "struct5": "结构5根", "hard10": "硬10%"}

    con = sqlite3.connect(DB)
    # events[vol_key][price_key][stop_key] = list[dict]
    all_events: dict[str, dict[str, dict[str, list[dict]]]] = {}
    for vk in vol_labels:
        all_events[vk] = {pk: {sk: [] for sk in stop_labels} for pk in price_labels}
    try:
        for year in years:
            pool = cached_make_pool(con, year, args.pool_size, args.min_mv, args.max_mv)
            if len(pool) < 10:
                print(f"  {year} 池子只有 {len(pool)} 只，跳过")
                continue
            ycount = 0
            for code in pool:
                df = load_stock(con, code, PREHEAT_START, args.end)
                if df is None:
                    continue
                df = df.reset_index(drop=True)
                vs = volume_signals(df).reset_index(drop=True)
                vs["sig_always"] = True            # 恒真量能 → 共振退化为价格信号单独
                ps = price_signals(df).reset_index(drop=True)
                lo, hi = f"{year}0101", f"{year + 1}0101"
                full = df.copy()
                for vk in vol_labels:
                    for pk in price_labels:
                        res = resonance_series(vs[vk], ps[pk], mode=args.mode)
                        full["_sig"] = res.values
                        full["_upper"] = vs["upper"].values
                        for sk in stop_labels:
                            ev = event_study(full, "_sig", "_upper", stop_mode=sk,
                                             cooldown=args.cooldown)
                            ev = [e for e in ev if lo <= e["date"] < hi]
                            all_events[vk][pk][sk].extend(ev)
                            ycount += len(ev)
            print(f"  {year}: 共振事件 {ycount} 笔")
    finally:
        con.close()

    # ---- 主表：任一量能 × 任一价格 ----
    print("\n" + "=" * 110)
    print(f"量能×价格 共振回测 · {years[0]}-{years[-1]} · 池 {args.pool_size} 只/年 · 共振窗口3天 · "
          f"模式{args.mode} · 冷却{args.cooldown}天 · {'含费' if args.fee else '不含费'}")
    print("=" * 110)
    print("\n【主表】任一量能 × 任一价格 共振（3 主卖出 × 3 止损）")
    hdr = f"{'卖出\\止损':<12}" + "".join(f"{s:>28}" for s in stop_labels.values())
    print(hdr)
    for ek, el in exit_labels.items():
        cells = []
        for sk in stop_labels:
            s = summarize(all_events["sig_any"]["sig_any_price"][sk], ek, ek + "_days",
                          fee=FEE if args.fee else 0.0)
            if s["n"] == 0:
                cells.append(f"{'无信号':>28}")
            else:
                cells.append(f"{s['n']}笔 {s['mean']:+.2f}% 胜{s['win']:.0f}% 最差{s['worst']:+.0f}%")
        print(f"{el:<12}" + "".join(cells))

    # ---- 细分：f20 + 结构5根 与 f20 + 硬10% ----
    print("\n【细分】f20 卖出 × 止损，各信号组合（笔数/平均/胜率/最差）")
    print(f"{'量能':<8}{'价格':<10}{'无止损':>26}{'结构5根':>26}{'硬10%':>26}")
    for vk, vl in vol_labels.items():
        for pk, pl in price_labels.items():
            cells = []
            for sk in stop_labels:
                s = summarize(all_events[vk][pk][sk], "f20", "f20_days",
                              fee=FEE if args.fee else 0.0)
                if s["n"] == 0:
                    cells.append(f"{'无信号':>26}")
                else:
                    cells.append(f"{s['n']}笔 {s['mean']:+.2f}% 胜{s['win']:.0f}% 最差{s['worst']:+.0f}%")
            print(f"{vl:<8}{pl:<10}" + "".join(cells))

    # ---- 止损效果：任一量能×任一价格，各主卖出的止损触发率 ----
    print("\n【止损触发率】任一量能 × 任一价格（止损触发的交易占比）")
    for ek, el in exit_labels.items():
        row = []
        for sk in stop_labels:
            s = summarize(all_events["sig_any"]["sig_any_price"][sk], ek, ek + "_days")
            row.append(f"{el}+{stop_labels[sk]}: {s['stopped']}/{s['n']} = "
                       f"{s['stopped'] / s['n'] * 100:.0f}%" if s["n"] else f"{el}+{stop_labels[sk]}: 无")
        print("  " + "  ".join(row))

    if args.csv:
        rows = []
        for vk, vl in vol_labels.items():
            for pk, pl in price_labels.items():
                for sk, sl in stop_labels.items():
                    for e in all_events[vk][pk][sk]:
                        rows.append({"量能": vl, "价格": pl, "止损": sl, "信号日": e["date"],
                                     "f5收益%": e["f5"], "f20收益%": e["f20"], "上沿收益%": e["upper"],
                                     "上沿持有天数": e["upper_days"], "触及上沿": e["upper_hit"]})
        pd.DataFrame(rows).to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"\n逐笔事件已导出：{args.csv}（{len(rows)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())