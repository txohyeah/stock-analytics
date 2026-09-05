#!/usr/bin/env python3
"""起爆点每日扫描 —— 全市场跑一遍，给出买点候选、优先级与可直接下单的止损位。

判据一律来自 tech_indicators.ignition（与回测、实盘计划同一套代码），本脚本不重复实现任何一条。

优先级怎么排（不用假精确的综合分，只按两条已被数据支持的规则）：
  1) 位置档位：A = 距60日高点<-40% 且 距60日低点>5%（历史单票胜率 73% 那一档）
               B = 只满足其中一条；C = 都不满足
     —— 位置条件在这里是**排序权重**，不是一票否决（回测已证明当否决用会让 71% 的票几年等不到一次信号）
  2) 同档内按「燃料」降序：近 60 日日均振幅。逐票分布显示头部全是高波动票，
     尾部全是每天只磨 1% 的阴跌白马 —— 波动率是这个策略的燃料，不是噪音。

用法：
    cd /home/application/stock-analytics
    ./venv/bin/python scripts/scan_ignition.py                 # 扫最新交易日
    ./venv/bin/python scripts/scan_ignition.py --days 3        # 最近3个交易日内的信号都列出来
    ./venv/bin/python scripts/scan_ignition.py --date 20260904 --csv /tmp/scan.csv
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
    IGNITION_DEEP_DRAWDOWN_PCT,
    IGNITION_STOP_BARS,
    IGNITION_STOP_PCT,
    IGNITION_OFF_BOTTOM_PCT,
    IGNITION_RSI_PERIOD,
    golden_channel_state,
    ignition_candle_filter,
    ignition_cross_signal,
    ignition_position_filter,
    ignition_rsi,
)

DB = ROOT / "data" / "stock.db"
BARS = 140          # 预热长度：60日位置窗口 + RSI/通道都要留够
ADJ = "adj_factor"


def latest_date(con) -> str:
    return con.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]


def universe(con, min_list_days: int) -> pd.DataFrame:
    """非 ST、已上市满一年、有近期行情的票。"""
    frame = pd.read_sql_query(
        "SELECT ts_code, name, industry, list_date, delist_date FROM stock_basic "
        "WHERE name NOT LIKE '%ST%' AND list_date <= ?",
        con, params=(min_list_days,),
    )
    frame = frame[frame.delist_date.isna() | (frame.delist_date == "")]
    return frame


def load_bars(con, code: str) -> pd.DataFrame | None:
    rows = pd.read_sql_query(
        f"SELECT d.trade_date, d.open, d.high, d.low, d.close, d.pct_chg, d.vol, a.{ADJ} "
        f"FROM daily d JOIN {ADJ} a ON a.ts_code = d.ts_code AND a.trade_date = d.trade_date "
        "WHERE d.ts_code = ? ORDER BY d.trade_date DESC LIMIT ?",
        con, params=(code, BARS),
    )
    if rows.empty:
        return None
    rows = rows.iloc[::-1].reset_index(drop=True)
    if rows[f"{ADJ}"].isna().any():
        return None
    k = rows[ADJ] / rows[ADJ].iloc[-1]                  # 前复权：一律以样本末日为基准
    out = pd.DataFrame({"trade_date": rows.trade_date.values})
    for col in ("open", "high", "low", "close"):
        out[col] = (rows[col] * k).astype("float64").values
    out["vol"] = rows.vol.values
    out["pct_chg"] = rows.pct_chg.values
    return out


def scan_one(df: pd.DataFrame, name: str, industry: str) -> list[dict]:
    """返回该票最近若干根的判定明细（每根一条，调用方按日期筛）。"""
    cross = ignition_cross_signal(df).values
    pos = ignition_position_filter(df).fillna(False).values
    candle = ignition_candle_filter(df).fillna(False).values
    rsi = ignition_rsi(df).values
    closes = df.close.values
    dd60 = (closes / pd.Series(df.high.values).rolling(60).max().values - 1) * 100
    low60 = (closes / pd.Series(df.low.values).rolling(60).min().values - 1) * 100
    amp = ((df.high.values - df.low.values) / np.where(closes == 0, np.nan, closes) * 100)[-60:]
    volx = df.vol.values / np.where(pd.Series(df.vol.values).shift(1).values == 0, np.nan,
                                    pd.Series(df.vol.values).shift(1).values)
    channel = golden_channel_state(df)
    upper = channel["upper"].values
    bear = channel["bear"].values
    prev_rsi = np.concatenate([[np.nan], rsi[:-1]])
    out = []
    for i in range(len(df)):
        if np.isnan(dd60[i]) or np.isnan(rsi[i]) or np.isnan(np.nanmean(amp)):
            continue                       # 预热不足 60 根的开头几根跳过（不是整只票作废）
        # 与回测/实盘同一套止损定义：max(买价×(1-10%), 起爆点及其前4根最低价)
        stop = max(float(closes[i]) * (1 - IGNITION_STOP_PCT),
                   float(np.nanmin(df.low.values[max(0, i - IGNITION_STOP_BARS + 1):i + 1])))
        out.append(dict(
            trade_date=str(df.trade_date.iloc[i]), close=round(float(closes[i]), 2),
            stop=round(stop, 2), stop_pct=round((stop / float(closes[i]) - 1) * 100, 1),
            pct_chg=round(float(df.pct_chg.values[i]), 2), rsi=round(float(rsi[i]), 1),
            cross=bool(cross[i]),
            first_cross=bool(cross[i]) and (np.isnan(prev_rsi[i]) or prev_rsi[i] < 40),
            position_ok=bool(pos[i]), candle_ok=bool(candle[i]),
            dd60=round(float(dd60[i]), 1), off_low=round(float(low60[i]), 1),
            amp60=round(float(np.nanmean(amp)), 2),
            vol_x=round(float(volx[i]), 2) if not np.isnan(volx[i]) else None,
            to_upper=round((float(upper[i]) / float(closes[i]) - 1) * 100, 1) if np.isfinite(upper[i]) else None,
            bear=bool(bear[i]), name=name, industry=industry, ts_code=None,
        ))
    return out


def tier(hit: dict) -> tuple[int, str]:
    deep = hit["dd60"] <= IGNITION_DEEP_DRAWDOWN_PCT
    off = hit["off_low"] >= IGNITION_OFF_BOTTOM_PCT
    if deep and off:
        return 0, "A"
    if deep or off:
        return 1, "B"
    return 2, "C"


def main() -> int:
    ap = argparse.ArgumentParser(description="起爆点全市场扫描")
    ap.add_argument("--date", help="扫描日，默认库里最新交易日")
    ap.add_argument("--days", type=int, default=1, help="回看几个交易日内的上穿（默认 1 = 只看当日）")
    ap.add_argument("--csv", help="把结果导出成 CSV")
    ap.add_argument("--limit", type=int, default=60, help="最多列多少行")
    ap.add_argument("--all", action="store_true", help="包含 C 档（深跌未到位且未离底）")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    day = args.date or latest_date(con)
    uni = universe(con, str(int(day[:4]) - 1) + "0101" if day[4:] >= "0101" else day)
    basic = pd.read_sql_query(
        "SELECT ts_code, name, industry FROM stock_basic WHERE name NOT LIKE '%ST%'", con
    ).set_index("ts_code")

    rows: list[dict] = []
    done = 0
    for code in uni.ts_code:
        bars = load_bars(con, code)
        done += 1
        if done % 800 == 0:
            print(f"  … 已扫 {done}/{len(uni)}", file=sys.stderr)
        if bars is None or len(bars) < 90:
            continue
        name = basic.at[code, "name"] if code in basic.index else ""
        industry = basic.at[code, "industry"] if code in basic.index else ""
        detail = scan_one(bars, name, industry)
        window = {h["trade_date"]: h for h in detail}
        dates = [d for d in window if d <= day][-args.days:]
        for d in dates:
            hit = window[d]
            if not hit["cross"]:
                continue
            hit["ts_code"] = code
            hit["signal_date"] = d
            rows.append(hit)
    con.close()

    if not rows:
        print("无信号。")
        return 0
    hits = pd.DataFrame(rows).drop_duplicates("ts_code")
    hits[["tier_n", "tier"]] = hits.apply(lambda r: pd.Series(tier(r)), axis=1)
    hits = hits.sort_values(["tier_n", "amp60"], ascending=[True, False])
    shown = hits if args.all else hits[hits.tier_n <= 1]
    if args.csv:
        hits.rename(columns=dict(
            ts_code="代码", name="名称", industry="行业", tier="位置档位", signal_date="信号日",
            close="收盘价", pct_chg="当日涨幅%", rsi=f"RSI{IGNITION_RSI_PERIOD}",
            position_ok="位置达标", candle_ok="形态干净", dd60="距60日高点%",
            off_low="距60日低点%", amp60="60日日均振幅%", vol_x="量/昨量",
            to_upper="距金牛上沿%", bear="熊市通道中", stop="止损价", stop_pct="距止损%",
            cross="当日上穿", first_cross="首次上穿")).to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"\n已导出全部 {len(hits)} 行 → {args.csv}")

    print(f"\n扫描日 {day}（回看 {args.days} 个交易日）｜全市场非ST已上市满一年：{len(uni)} 只，"
          f"有行情且指标可用：{done} 只扫完")
    print(f"命中上穿信号 {len(hits)} 只：A 档 {(hits.tier=='A').sum()}、B 档 {(hits.tier=='B').sum()}、"
          f"C 档 {(hits.tier=='C').sum()}")
    print("（A=深跌≥40%且已离底≥5%，历史单票胜率约73%；档内按波动率降序）\n")
    cols = shown.head(args.limit).rename(columns=dict(
        ts_code="代码", name="名称", industry="行业", tier="档", close="收盘价", dd60="距60高%",
        off_low="距60低%", amp60="振幅%", vol_x="量/昨", to_upper="距上沿%", rsi="RSI6",
        stop="止损价", stop_pct="距止损%", pct_chg="涨幅%", signal_date="信号日",
        position_ok="位置", candle_ok="形态", bear="熊道"))
    cols = cols[["代码", "名称", "行业", "档", "收盘价", "止损价", "距止损%", "涨幅%", "RSI6",
                 "距60高%", "距60低%", "振幅%", "量/昨", "距上沿%", "形态", "熊道"]]
    pd.set_option("display.unicode.east_asian_width", True)
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(cols.to_string(index=False))
    print("\n口径：买入价=信号日收盘（当日涨幅>5% 则次日再买，不追）；「止损价」= max(买价×0.90, "
          "起爆点及其前4根最低价) 收盘价触发；浮盈曾≥20% 后改按「最高点−总涨幅×25%」移动止盈；"
          "盘中摸到金牛上沿但收盘收回下方则减半（熊道=True 时清仓）。「距上沿%」是到那个减价位还有多少空间。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
