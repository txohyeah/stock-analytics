#!/usr/bin/env python3
"""大盘（市场层）状态表 —— 从研究版 `research/pa-state-label/code/market_state.py` 正式化（2026-10-08）。

口径：**全市场等权代理**，不依赖 index_daily（后者有数据缺口：000300/000852 缺 2019~2022）。
    et w_idx = 全市场个股当日涨跌幅**截尾 ±21% 后**的横截面**均值**逐日累乘
    方向   = 等权指数 vs 自身 MA60（≥ 判 above，否则 below）
    宽度   = 20 日上涨家数占比 > 50% 判「宽」，否则「窄」
    状态   = 方向 × 宽度 四桶（above_宽/above_窄/below_宽/below_窄）

三条纪律（都是 2026-10-08 踩过的坑，勿改）：
  ① **必须按年分块读 daily**：1000 万+ 行一次性 select 会把 pandas 撑爆（实测 exit 137 OOM）。
  ② **等权指数用截尾后的横截面均值累乘**，不能用中位数累乘：中位数本身没错，错在
     「把中位数当日收益累乘」——那不是任何组合的收益（2024 年算出 −40.4%，中证1000 实际 +1.8%）。
     同时必须截尾 ±21%：daily.pct_chg 有垃圾极值（全表 max = +200400%，每年 360~1250 行 |涨跌|>21%），
     一行就能给当日均值贡献 +0.4pp。
  ③ **只用当日及以前信息**；宽度阈值写死 50%，不用全样本分位（避免隐性前视）。

与信号层的关系（弱耦合）：市场层只算一份、信评层与展示层共用；**只给状态判断，不当过滤器**。
研究结论：该口径下大盘 MA60 下方时起爆信号 f20 +5.55%、上方 −0.01%（7/9 年成立，样本 2018-2026 内）。

用法：
    ./venv/bin/python scripts/market_state.py update        # 增量追加（默认，日常用）
    ./venv/bin/python scripts/market_state.py build         # 全量重建（首次 / 回补）
    ./venv/bin/python scripts/market_state.py show          # 打印最近 10 天
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "stock.db"

START_YEAR = 2017
BREADTH_TH = 0.50          # 20 日上涨家数占比的固定阈值
CLIP_PCT = 21.0            # 日收益截尾（%）：覆盖主板 ±10%、双创 ±20%，砍新股首日与脏数据

COLS = ["trade_date", "ret_mean", "ret_med", "bad_rows", "up_ratio", "ew_idx",
        "ew_ma60", "ew_slope60", "breadth20", "mkt_dir", "mkt_breadth", "mkt_state"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_state_daily (
    trade_date TEXT PRIMARY KEY,        -- YYYYMMDD
    ret_mean REAL,                      -- 当日横截面均值收益（截尾后，小数）
    ret_med REAL,                       -- 当日横截面中位收益（小数，展示用）
    bad_rows INTEGER,                   -- 当日被截尾的脏数据行数
    up_ratio REAL,                      -- 当日上涨家数占比
    ew_idx REAL,                        -- 等权指数（2017 起累乘）
    ew_ma60 REAL,                       -- 等权指数 60 日均线
    ew_slope60 REAL,                    -- MA60 十日斜率
    breadth20 REAL,                     -- 20 日上涨家数占比（宽度）
    mkt_dir TEXT,                       -- above / below（等权指数 vs MA60）
    mkt_breadth TEXT,                   -- 宽 / 窄
    mkt_state TEXT,                     -- 方向_宽度 四桶
    updated_at TEXT
);
"""


def connect(path: Path = DB) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def _daily_agg(con: sqlite3.Connection, lo: str, hi: str) -> pd.DataFrame:
    """读 [lo, hi] 区间 daily 并算当日横截面统计（按年分块，防 OOM）。"""
    parts = []
    for yr in range(int(lo[:4]), int(hi[:4]) + 1):
        a, b = max(lo, f"{yr}0101"), min(hi, f"{yr}1231")
        if a > b:
            continue
        px = pd.read_sql_query(
            "SELECT trade_date, pct_chg FROM daily WHERE trade_date >= ? AND trade_date <= ? "
            "AND pct_chg IS NOT NULL", con, params=(a, b))
        if px.empty:
            continue
        clip = px["pct_chg"].clip(-CLIP_PCT, CLIP_PCT)
        g = px.groupby("trade_date")["pct_chg"]
        parts.append(pd.DataFrame({
            "ret_mean": clip.groupby(px["trade_date"]).mean() / 100.0,
            "ret_med": g.median() / 100.0,
            "bad_rows": (px["pct_chg"].abs() > CLIP_PCT).groupby(px["trade_date"]).sum(),
            "up_ratio": g.apply(lambda s: (s > 0).mean()),
        }))
        del px, clip
    if not parts:
        return pd.DataFrame(columns=["ret_mean", "ret_med", "bad_rows", "up_ratio"])
    return pd.concat(parts).sort_index()


def _derive(m: pd.DataFrame, prev_idx: float | None = None) -> pd.DataFrame:
    """由日频统计推出指数/均线/宽度/状态。prev_idx 给增量续接用（None = 从 1.0 起）。"""
    m = m.sort_index().copy()
    # 等权指数：截尾均值逐日累乘；增量模式从上一交易日已存的指数值续接（保持同一条曲线）
    base = 1.0 + float(m["ret_mean"].iloc[0]) if prev_idx is None else float(prev_idx)
    growth = (1.0 + m["ret_mean"]).cumprod()
    m["ew_idx"] = growth / growth.iloc[0] * base
    m["ew_ma60"] = m["ew_idx"].rolling(60).mean()
    m["ew_slope60"] = m["ew_ma60"] / m["ew_ma60"].shift(10) - 1.0
    m["breadth20"] = m["up_ratio"].rolling(20).mean()
    m["mkt_dir"] = np.where(m["ew_idx"] >= m["ew_ma60"], "above", "below")
    m["mkt_breadth"] = np.where(m["breadth20"] > BREADTH_TH, "宽", "窄")
    m["mkt_state"] = m["mkt_dir"] + "_" + m["mkt_breadth"]
    m.loc[m["ew_ma60"].isna() | m["breadth20"].isna(), "mkt_state"] = "na"
    return m


def _upsert(con: sqlite3.Connection, df: pd.DataFrame) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [tuple(r) + (now,) for r in df[COLS].itertuples(index=False, name=None)]
    con.executemany(
        f"INSERT OR REPLACE INTO market_state_daily ({','.join(COLS)}, updated_at) "
        f"VALUES ({','.join('?' * (len(COLS) + 1))})", rows)
    con.commit()
    return len(rows)


def build(con: sqlite3.Connection, start: str = str(START_YEAR)) -> int:
    """全量重建：从 start 年起重算整条曲线（幂等）。"""
    raw = _daily_agg(con, f"{start}0101", "20991231")
    if raw.empty:
        return 0
    out = _derive(raw).reset_index().rename(columns={"index": "trade_date"})
    out["trade_date"] = out["trade_date"].astype(str)
    return _upsert(con, out)


def update(con: sqlite3.Connection, tail: int = 90) -> int:
    """增量追加：新交易日从已存曲线续接，只需重算尾部窗口（日常 20:15 用）。"""
    last = con.execute("SELECT MAX(trade_date) FROM market_state_daily").fetchone()[0]
    newest = con.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
    if newest is None:
        return 0
    if last is None:                      # 空表 → 走全量
        return build(con)
    if newest <= last:
        return 0
    keep = pd.read_sql_query(
        "SELECT trade_date, ret_mean, ret_med, bad_rows, up_ratio, ew_idx FROM market_state_daily "
        "ORDER BY trade_date DESC LIMIT ?", con, params=(tail,)).iloc[::-1]
    fresh = _daily_agg(con, str(last), "20991231")   # 新日期的日频统计（含 last 那一天，稍后剔除）
    fresh = fresh[fresh.index > str(last)]
    if fresh.empty:
        return 0
    merged = pd.concat([keep.set_index("trade_date")[["ret_mean", "ret_med", "bad_rows", "up_ratio"]],
                        fresh]).sort_index()
    # 续接基准取 keep 的**第一行**（_derive 把 merged 首行的指数值钉在 base 上再往后累乘）
    base_idx = float(keep["ew_idx"].iloc[0])
    out = _derive(merged, prev_idx=base_idx).reset_index().rename(columns={"index": "trade_date"})
    out["trade_date"] = out["trade_date"].astype(str)
    out = out[out["trade_date"] > last]
    return _upsert(con, out)


def main() -> int:
    ap = argparse.ArgumentParser(description="大盘（市场层）状态表")
    ap.add_argument("cmd", choices=["build", "update", "show"], nargs="?", default="update")
    ap.add_argument("--start", default=str(START_YEAR), help="build 起始年")
    ap.add_argument("--tail", type=int, default=90, help="update 重算尾部窗口天数")
    ap.add_argument("--date", help="show 指定日期")
    args = ap.parse_args()

    con = connect()
    if args.cmd == "build":
        print(f"全量重建完成：{build(con, args.start)} 行")
    elif args.cmd == "update":
        n = update(con, args.tail)
        print(f"增量追加 {n} 行（{'无新交易日' if n == 0 else 'OK'}）")
    show = pd.read_sql_query(
        "SELECT * FROM market_state_daily ORDER BY trade_date DESC LIMIT 10", con)
    print(show[["trade_date", "ret_mean", "up_ratio", "ew_idx", "ew_ma60", "breadth20",
                "mkt_state"]].iloc[::-1].to_string(index=False))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
