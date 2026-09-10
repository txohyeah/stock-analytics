#!/usr/bin/env python3
"""起爆模拟盘：信号池记录 + 每日推进 + 报表（2026-09-10 新建）

口径（与回测 backtest_ignition.py 完全一致）：
  - 建仓：信号日收盘买入；信号日涨幅 >5% 则次日收盘买（不追）；次日停牌/无行情则放弃
  - 复权：全程后复权（raw × adj_factor），基准不随最新价漂移，账本稳定；报表另显未复权价
  - 卖出：唯一实现在库 IgnitionPosition.step（C2：撞金牛上沿全清 + max(硬10%, 滚动30根) 止损）
  - 费用：买入 0.1% 佣金；卖出 0.1% 佣金 + 0.05% 印花税
  - 额度：单票上限 10 万，按强度分档（用户 2026-09-10 拍板）：
      回踩：★重手(距上沿≥8%) → 10万；量能确认(量托/OBV) → 8万；普通 → 6万
      超跌：跌幅≥40% 且量能确认 → 10万；二选一 → 8万；普通 → 6万
  - 不设总资金/槽位上限：出信号就建仓（用户拍板）

用法：
    ./venv/bin/python scripts/paper_sim.py ingest --pullback-csv X --ignition-csv Y
    ./venv/bin/python scripts/paper_sim.py step --date 20260910
    ./venv/bin/python scripts/paper_sim.py report --date 20260910
    ./venv/bin/python scripts/paper_sim.py run --date 20260910   # ingest+step+report 一体
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
    IgnitionPosition,
    golden_channel_state,
)

DB = ROOT / "data" / "stock.db"
SIM_DB = ROOT / "data" / "paper_sim.db"
SIGNALS_DIR = ROOT / "data" / "signals"

FEE = 0.001          # 买入佣金（与回测一致）
STAMP = 0.0005       # 卖出印花税（与回测一致）
REASON_ZH = {"stop_loss": "止损", "trailing_take_profit": "移动止盈",
             "upper_pressure_exit": "上沿压制·全清", "upper_pressure_half": "上沿压制·减半"}
REPLAY_BARS = 500    # 重放窗口：持仓最长约 2 年，含止损滚动窗与通道预热余量

SIZE_HEAVY = 100_000.0   # 重手/强信号
SIZE_CONFIRM = 80_000.0  # 量能确认
SIZE_BASE = 60_000.0     # 普通

# 扫描器 CSV 中文列 → 内部字段
PULLBACK_COLS = {
    "代码": "ts_code", "名称": "name", "分类": "category", "信号日": "signal_date",
    "收盘价": "close_raw", "参考止损": "stop_raw", "当日涨幅%": "pct_chg",
    "距60日高点%": "dd60", "距上沿%": "to_upper", "重手": "heavy",
    "量托确认": "liangtuo_ok", "OBV金叉确认": "obv_ok", "量能确认": "vol_confirm",
}
IGNITION_COLS = {
    "代码": "ts_code", "名称": "name", "行业": "category", "位置档位": "tier",
    "信号日": "signal_date", "收盘价": "close_raw", "当日涨幅%": "pct_chg",
    "距60日高点%": "dd60", "距金牛上沿%": "to_upper", "60日日均振幅%": "amp60",
    "量托确认": "liangtuo_ok", "OBV金叉确认": "obv_ok", "量能确认": "vol_confirm",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date TEXT NOT NULL,          -- 信号日 YYYYMMDD
    strategy TEXT NOT NULL,             -- pullback / ignition
    ts_code TEXT NOT NULL,
    name TEXT,
    category TEXT,                      -- 回踩分类 / 超跌档位
    close_raw REAL,                     -- 信号日收盘（未复权，报表用）
    pct_chg REAL,                       -- 信号日涨幅%
    stop_raw REAL,                      -- 参考止损（未复权）
    heavy INTEGER DEFAULT 0,            -- ★重手
    vol_confirm INTEGER DEFAULT 0,      -- 量能确认（量托或OBV）
    liangtuo_ok INTEGER DEFAULT 0,
    obv_ok INTEGER DEFAULT 0,
    dd60 REAL,                          -- 距60日高点%（负值）
    to_upper REAL,                      -- 距上沿%
    amp60 REAL,                         -- 60日日均振幅%（超跌）
    size_plan REAL,                     -- 计划买入金额
    entry_date TEXT,                    -- 实际建仓日 YYYYMMDD
    entry_price_adj REAL,               -- 建仓价（后复权）
    entry_price_raw REAL,               -- 建仓价（未复权）
    shares REAL,
    stop_line_adj REAL,                 -- 当前止损线（后复权，每日更新）
    status TEXT DEFAULT 'pending',      -- pending/opened/closed/skipped
    skip_reason TEXT,
    UNIQUE(signal_date, strategy, ts_code)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL UNIQUE,
    ts_code TEXT, name TEXT, strategy TEXT,
    entry_date TEXT, exit_date TEXT,
    entry_price_adj REAL, exit_price_adj REAL,
    entry_price_raw REAL, exit_price_raw REAL,
    shares REAL, cost REAL, proceeds REAL,
    pnl REAL, ret REAL, days INTEGER, reason TEXT
);
CREATE TABLE IF NOT EXISTS daily_snapshot (
    trade_date TEXT PRIMARY KEY,
    n_signals INTEGER, n_opened INTEGER, n_closed INTEGER,
    realized_pnl REAL, open_value REAL, open_cost REAL, n_open INTEGER
);
"""


def norm_date(s) -> str:
    return str(s).strip().replace("-", "")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(SIM_DB)
    con.executescript(SCHEMA)
    return con


def stock_con() -> sqlite3.Connection:
    return sqlite3.connect(DB)


def load_bars_adj(con: sqlite3.Connection, code: str, end_date: str,
                  n: int = REPLAY_BARS) -> pd.DataFrame | None:
    """读最近 n 根（含 end_date）日线并后复权，附因果金牛上沿。"""
    rows = pd.read_sql_query(
        "SELECT d.trade_date, d.open, d.high, d.low, d.close, d.pct_chg, a.adj_factor "
        "FROM daily d JOIN adj_factor a ON a.ts_code = d.ts_code AND a.trade_date = d.trade_date "
        "WHERE d.ts_code = ? AND d.trade_date <= ? ORDER BY d.trade_date DESC LIMIT ?",
        con, params=(code, end_date, n),
    )
    if rows.empty:
        return None
    rows = rows.iloc[::-1].reset_index(drop=True)
    rows["trade_date"] = rows["trade_date"].astype(str)
    for col in ("open", "high", "low", "close"):
        rows[col] = rows[col] * rows["adj_factor"]
    ch = golden_channel_state(rows, causal=True)
    rows["upper"] = ch["upper"].values
    return rows


def next_trade_date(con: sqlite3.Connection, date: str) -> str | None:
    row = con.execute("SELECT MIN(trade_date) FROM daily WHERE trade_date > ?",
                      (date,)).fetchone()
    return str(row[0]) if row and row[0] else None


def merge_duplicates(con: sqlite3.Connection) -> int:
    """同日同票多策略信号：保留最高档建仓，其余标记 skipped（每只票最多 10 万）。

    用户口径（2026-09-10）：每个股最多买入 10 万；同一只票同日被两个策略同时选中时，
    按强度取最高档（如回踩★重手 10 万 + 超跌深跌 8 万 → 买 10 万），信号池两条都留档。
    """
    rows = con.execute(
        "SELECT id, signal_date, ts_code, size_plan FROM signal_pool "
        "WHERE status='pending' ORDER BY size_plan DESC, id").fetchall()
    seen: set[tuple[str, str]] = set()
    n = 0
    for rid, sdate, code, _size in rows:
        key = (sdate, code)
        if key in seen:
            con.execute("UPDATE signal_pool SET status='skipped', skip_reason='同票多策略合并' "
                        "WHERE id=?", (rid,))
            n += 1
        else:
            seen.add(key)
    con.commit()
    return n


def plan_size(strategy: str, r: dict) -> float:
    """额度分档（用户 2026-09-10 拍板）。"""
    if strategy == "pullback":
        if r.get("heavy"):
            return SIZE_HEAVY
        if r.get("vol_confirm"):
            return SIZE_CONFIRM
        return SIZE_BASE
    # ignition 超跌起爆
    deep40 = r.get("dd60") is not None and r["dd60"] <= -40.0
    if deep40 and r.get("vol_confirm"):
        return SIZE_HEAVY
    if deep40 or r.get("vol_confirm"):
        return SIZE_CONFIRM
    return SIZE_BASE


def ingest(csv_path: str, strategy: str, con: sqlite3.Connection) -> int:
    """把扫描器 CSV 写入信号池（幂等：UNIQUE 约束 + INSERT OR IGNORE）。"""
    if not Path(csv_path).exists():
        print(f"⚠️ 找不到 {csv_path}，跳过 {strategy} 导入")
        return 0
    frame = pd.read_csv(csv_path, dtype=str).fillna("")
    cols = PULLBACK_COLS if strategy == "pullback" else IGNITION_COLS
    if not set(cols).issubset(frame.columns):
        print(f"⚠️ {csv_path} 列不匹配（缺 {set(cols) - set(frame.columns)}），跳过")
        return 0
    frame = frame.rename(columns=cols)
    if strategy == "ignition":
        frame = frame[frame["tier"] == "超跌起爆"]      # 只收超跌起爆档
    n = 0
    for _, r in frame.iterrows():
        rec = {
            "signal_date": norm_date(r["signal_date"]),
            "strategy": strategy,
            "ts_code": r["ts_code"],
            "name": r.get("name", ""),
            "category": r.get("category", ""),
            "close_raw": float(r["close_raw"]),
            "pct_chg": float(r["pct_chg"]),
            "stop_raw": float(r["stop_raw"]) if r.get("stop_raw") not in (None, "", "nan") else None,
            "heavy": int(r["heavy"] in ("True", "true", "1", "★")) if strategy == "pullback" else 0,
            "vol_confirm": int(r["vol_confirm"] in ("True", "true", "1", "✓")),
            "liangtuo_ok": int(r["liangtuo_ok"] in ("True", "true", "1", "✓")),
            "obv_ok": int(r["obv_ok"] in ("True", "true", "1", "✓")),
            "dd60": float(r["dd60"]) if r.get("dd60") not in (None, "", "nan") else None,
            "to_upper": float(r["to_upper"]) if r.get("to_upper") not in (None, "", "nan") else None,
            "amp60": float(r["amp60"]) if r.get("amp60") not in (None, "", "nan") else None,
        }
        rec["size_plan"] = plan_size(strategy, rec)
        cur = con.execute(
            "INSERT OR IGNORE INTO signal_pool (signal_date, strategy, ts_code, name, category, "
            "close_raw, pct_chg, stop_raw, heavy, vol_confirm, liangtuo_ok, obv_ok, dd60, "
            "to_upper, amp60, size_plan) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["signal_date"], rec["strategy"], rec["ts_code"], rec["name"], rec["category"],
             rec["close_raw"], rec["pct_chg"], rec["stop_raw"], rec["heavy"], rec["vol_confirm"],
             rec["liangtuo_ok"], rec["obv_ok"], rec["dd60"], rec["to_upper"], rec["amp60"],
             rec["size_plan"]),
        )
        n += cur.rowcount
    con.commit()
    return n


def try_open(con: sqlite3.Connection, scon: sqlite3.Connection, sig: dict, entry_date: str) -> bool:
    """按 entry_date 收盘建仓；停牌/无行情返回 False。con=账本，scon=行情。"""
    bars = load_bars_adj(scon, sig["ts_code"], entry_date, n=REPLAY_BARS)
    if bars is None:
        return False
    row = bars[bars["trade_date"] == entry_date]
    if row.empty or not np.isfinite(row["close"].iloc[0]) or row["close"].iloc[0] <= 0:
        return False
    price_adj = float(row["close"].iloc[0])
    price_raw = price_adj / float(row["adj_factor"].iloc[0])
    shares = sig["size_plan"] / (price_adj * (1 + FEE))
    con.execute(
        "UPDATE signal_pool SET status='opened', entry_date=?, entry_price_adj=?, "
        "entry_price_raw=?, shares=?, stop_line_adj=? WHERE id=?",
        (entry_date, price_adj, price_raw, shares, price_adj * 0.9, sig["id"]),
    )
    return True


def replay(con: sqlite3.Connection, sig: dict, date: str):
    """从建仓日重放状态机到 date。返回 (动作, 触发日, 触发收盘复权价, algo) 或 (None,None,None,algo)。"""
    bars = load_bars_adj(con, sig["ts_code"], date, n=REPLAY_BARS)
    if bars is None or len(bars) < 2:
        return None, None, None, None
    idx = bars.index[bars["trade_date"] == sig["entry_date"]]
    if len(idx) == 0:
        return None, None, None, None
    i0 = int(idx[0])
    algo = IgnitionPosition.open_at(sig["entry_price_adj"], i0,
                                    float(bars["high"].iloc[i0]), bars["low"].values)
    for i in range(i0 + 1, len(bars)):
        act = algo.step(i, float(bars["open"].iloc[i]), float(bars["high"].iloc[i]),
                        float(bars["low"].iloc[i]), float(bars["close"].iloc[i]),
                        float(bars["upper"].iloc[i]), bars["low"].values)
        if act:
            return act, str(bars["trade_date"].iloc[i]), float(bars["close"].iloc[i]), algo
    return None, None, None, algo


def trading_days(con: sqlite3.Connection, a: str, b: str) -> int:
    row = con.execute("SELECT COUNT(DISTINCT trade_date) FROM daily "
                      "WHERE trade_date BETWEEN ? AND ?", (a, b)).fetchone()
    return int(row[0]) if row and row[0] else 0


def adj_factor_at(con: sqlite3.Connection, code: str, date: str) -> float:
    row = con.execute("SELECT adj_factor FROM adj_factor WHERE ts_code=? AND trade_date=?",
                      (code, date)).fetchone()
    return float(row[0]) if row else 1.0


def record_trade(con: sqlite3.Connection, scon: sqlite3.Connection, sig: dict, exit_date: str,
                 exit_price_adj: float, act) -> None:
    kind, reason = act
    exit_price_raw = exit_price_adj / adj_factor_at(scon, sig["ts_code"], exit_date)
    cost = sig["shares"] * sig["entry_price_adj"] * (1 + FEE)
    proceeds = sig["shares"] * exit_price_adj * (1 - FEE - STAMP)
    pnl = proceeds - cost
    ret = exit_price_adj / sig["entry_price_adj"] - 1.0
    days = trading_days(scon, sig["entry_date"], exit_date)
    con.execute(
        "INSERT OR IGNORE INTO trades (signal_id, ts_code, name, strategy, entry_date, exit_date, "
        "entry_price_adj, exit_price_adj, entry_price_raw, exit_price_raw, shares, cost, proceeds, "
        "pnl, ret, days, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sig["id"], sig["ts_code"], sig["name"], sig["strategy"], sig["entry_date"], exit_date,
         sig["entry_price_adj"], exit_price_adj, sig["entry_price_raw"], exit_price_raw,
         sig["shares"], cost, proceeds, pnl, ret, days, REASON_ZH.get(reason, reason)),
    )
    con.execute("UPDATE signal_pool SET status='closed' WHERE id=?", (sig["id"],))


def step(date: str) -> None:
    con = connect()
    scon = stock_con()
    # 1. pending 信号 → 决定建仓日并尝试建仓
    for sig in con.execute("SELECT * FROM signal_pool WHERE status='pending'").fetchall():
        s = dict(zip([d[0] for d in con.execute("SELECT * FROM signal_pool LIMIT 0").description], sig))
        entry_date = s["entry_date"]
        if entry_date is None:
            if s["pct_chg"] is not None and s["pct_chg"] > 5.0:
                entry_date = next_trade_date(scon, s["signal_date"])
                if entry_date is None or entry_date > date:
                    continue                      # 次日还没到，继续等
            else:
                entry_date = s["signal_date"]
            con.execute("UPDATE signal_pool SET entry_date=? WHERE id=?", (entry_date, s["id"]))
        if entry_date > date:
            continue
        if not try_open(con, scon, s, entry_date):
            con.execute("UPDATE signal_pool SET status='skipped', skip_reason='次日停牌/无行情' "
                        "WHERE id=?", (s["id"],))
    con.commit()

    # 2. 推进持仓
    for sig in con.execute("SELECT * FROM signal_pool WHERE status='opened'").fetchall():
        s = dict(zip([d[0] for d in con.execute("SELECT * FROM signal_pool LIMIT 0").description], sig))
        act, exit_date, exit_px, algo = replay(scon, s, date)
        if act:
            record_trade(con, scon, s, exit_date, exit_px, act)
        elif algo is not None:
            con.execute("UPDATE signal_pool SET stop_line_adj=? WHERE id=?",
                        (algo.stop_line, s["id"]))
    con.commit()

    # 3. 快照
    n_open = con.execute("SELECT COUNT(*) FROM signal_pool WHERE status='opened'").fetchone()[0]
    n_closed = con.execute("SELECT COUNT(*) FROM trades WHERE exit_date=?", (date,)).fetchone()[0]
    n_signals = con.execute("SELECT COUNT(*) FROM signal_pool WHERE signal_date=?", (date,)).fetchone()[0]
    realized = con.execute("SELECT COALESCE(SUM(pnl),0) FROM trades").fetchone()[0]
    open_cost = con.execute("SELECT COALESCE(SUM(shares*entry_price_adj*(1+?)),0) "
                            "FROM signal_pool WHERE status='opened'", (FEE,)).fetchone()[0]
    open_value = 0.0
    for sig in con.execute("SELECT * FROM signal_pool WHERE status='opened'").fetchall():
        s = dict(zip([d[0] for d in con.execute("SELECT * FROM signal_pool LIMIT 0").description], sig))
        bars = load_bars_adj(scon, s["ts_code"], date, n=10)
        if bars is not None and len(bars):
            open_value += s["shares"] * float(bars["close"].iloc[-1])
    con.execute("INSERT OR REPLACE INTO daily_snapshot (trade_date, n_signals, n_opened, n_closed, "
                "realized_pnl, open_value, open_cost, n_open) VALUES (?,?,?,?,?,?,?,?)",
                (date, n_signals, n_closed, n_closed, realized, open_value, open_cost, n_open))
    con.commit()
    scon.close()
    con.close()


def fmt_pct(x: float | None, signed: bool = True) -> str:
    if x is None or not np.isfinite(x):
        return "-"
    return f"{x:+.2f}%" if signed else f"{x:.2f}%"


def report(date: str) -> str:
    con = connect()
    scon = stock_con()
    out: list[str] = []
    d = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    out.append(f"📈 起爆模拟盘 {d}")

    def sig_dict(row):
        cols = [c[0] for c in con.execute("SELECT * FROM signal_pool LIMIT 0").description]
        return dict(zip(cols, row))

    def trade_dict(row):
        cols = [c[0] for c in con.execute("SELECT * FROM trades LIMIT 0").description]
        return dict(zip(cols, row))

    # 今日新信号
    sigs = con.execute("SELECT * FROM signal_pool WHERE signal_date=?", (date,)).fetchall()
    if sigs:
        n_pb = sum(1 for s in sigs if s[2] == "pullback")
        n_ig = sum(1 for s in sigs if s[2] == "ignition")
        out.append(f"\n【今日新信号】回踩 {n_pb} 笔 / 超跌 {n_ig} 笔")
    else:
        out.append("\n【今日新信号】无")

    # 今日建仓
    opened = con.execute("SELECT * FROM signal_pool WHERE entry_date=? AND status='opened'",
                         (date,)).fetchall()
    if opened:
        out.append(f"\n【今日建仓】{len(opened)} 笔")
        for row in opened:
            s = sig_dict(row)
            strat = "回踩" if s["strategy"] == "pullback" else "超跌"
            if s["strategy"] == "pullback":
                tag = "★重手" if s["heavy"] else ("量能确认" if s["vol_confirm"] else "普通")
            else:
                deep40 = s["dd60"] is not None and s["dd60"] <= -40.0
                if deep40 and s["vol_confirm"]:
                    tag = "深跌40%+量能"
                elif deep40:
                    tag = "深跌40%"
                elif s["vol_confirm"]:
                    tag = "量能确认"
                else:
                    tag = "普通"
            out.append(f"● {s['name']} {s['ts_code']} [{strat}] 买 {s['entry_price_raw']:.2f} 元 "
                       f"× {s['size_plan']/10000:.0f} 万（{tag}）")
    else:
        out.append("\n【今日建仓】无")

    # 待次日/合并说明
    pend = con.execute("SELECT COUNT(*) FROM signal_pool WHERE signal_date=? AND status='pending'",
                       (date,)).fetchone()[0]
    merged = con.execute("SELECT COUNT(*) FROM signal_pool WHERE signal_date=? AND "
                         "status='skipped' AND skip_reason='同票多策略合并'", (date,)).fetchone()[0]
    notes = []
    if pend:
        notes.append(f"{pend} 笔涨幅>5% 待次日建仓")
    if merged:
        notes.append(f"{merged} 笔同票多策略合并（取最高档）")
    if notes:
        out.append(f"（{'；'.join(notes)}）")

    # 今日平仓
    closed = con.execute("SELECT * FROM trades WHERE exit_date=?", (date,)).fetchall()
    if closed:
        out.append(f"\n【今日平仓】{len(closed)} 笔")
        for row in closed:
            t = trade_dict(row)
            strat = "回踩" if t["strategy"] == "pullback" else "超跌"
            out.append(f"● {t['name']} {t['ts_code']} [{strat}] "
                       f"{t['entry_date'][4:6]}/{t['entry_date'][6:8]}→{t['exit_date'][4:6]}/{t['exit_date'][6:8]} "
                       f"{fmt_pct(t['ret']*100)} 盈亏 {t['pnl']:+,.0f} 元（{t['reason']}，持 {t['days']} 天）")
    else:
        out.append("\n【今日平仓】无")

    # 当前持仓
    holds = con.execute("SELECT * FROM signal_pool WHERE status='opened'").fetchall()
    if holds:
        out.append(f"\n【当前持仓】{len(holds)} 笔")
        for row in holds:
            s = sig_dict(row)
            strat = "回踩" if s["strategy"] == "pullback" else "超跌"
            bars = load_bars_adj(scon, s["ts_code"], date, n=10)
            if bars is None or not len(bars):
                out.append(f"● {s['name']} {s['ts_code']} [{strat}] 无行情")
                continue
            px_adj = float(bars["close"].iloc[-1])
            px_raw = px_adj / float(bars["adj_factor"].iloc[-1])
            ret = px_adj / s["entry_price_adj"] - 1.0
            stop_raw = s["stop_line_adj"] / float(bars["adj_factor"].iloc[-1]) if s["stop_line_adj"] else None
            stop_pct = (px_adj / s["stop_line_adj"] - 1.0) * 100 if s["stop_line_adj"] else None
            out.append(f"● {s['name']} {s['ts_code']} [{strat}] {s['entry_date'][4:6]}/{s['entry_date'][6:8]}买 "
                       f"成本 {s['entry_price_raw']:.2f} 现价 {px_raw:.2f} 浮盈 {fmt_pct(ret*100)} "
                       f"止损 {stop_raw:.2f}（距 {fmt_pct(stop_pct)}）")
    else:
        out.append("\n【当前持仓】无")

    # 累计
    trades = con.execute("SELECT * FROM trades").fetchall()
    if trades:
        tds = [trade_dict(r) for r in trades]
        wins = sum(1 for t in tds if t["ret"] > 0)
        total_pnl = sum(t["pnl"] for t in tds)
        avg_ret = np.mean([t["ret"] for t in tds]) * 100
        out.append(f"\n【累计】已平仓 {len(tds)} 笔 胜率 {wins/len(tds)*100:.0f}% "
                   f"累计盈亏 {total_pnl:+,.0f} 元 平均每笔 {avg_ret:+.2f}%")
    else:
        out.append("\n【累计】暂无平仓记录")
    scon.close()
    con.close()
    return "\n".join(out)


def cmd_run(date: str) -> int:
    con = connect()
    pb = SIGNALS_DIR / f"pullback_{date}.csv"
    ig = SIGNALS_DIR / f"ignition_{date}.csv"
    n1 = ingest(str(pb), "pullback", con)
    n2 = ingest(str(ig), "ignition", con)
    n3 = merge_duplicates(con)
    con.close()
    print(f"信号池导入：回踩 {n1} 笔 / 超跌 {n2} 笔（同票合并跳过 {n3} 笔）")
    step(date)
    print()
    print(report(date))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="起爆模拟盘")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_ingest = sub.add_parser("ingest", help="导入扫描 CSV 到信号池")
    p_ingest.add_argument("--pullback-csv")
    p_ingest.add_argument("--ignition-csv")
    p_step = sub.add_parser("step", help="推进模拟盘一天")
    p_step.add_argument("--date", required=True)
    p_rep = sub.add_parser("report", help="输出报表")
    p_rep.add_argument("--date", required=True)
    p_run = sub.add_parser("run", help="ingest+step+report 一体")
    p_run.add_argument("--date", required=True)
    args = ap.parse_args()

    if args.cmd == "ingest":
        con = connect()
        n1 = ingest(args.pullback_csv, "pullback", con) if args.pullback_csv else 0
        n2 = ingest(args.ignition_csv, "ignition", con) if args.ignition_csv else 0
        con.close()
        print(f"信号池导入：回踩 {n1} 笔 / 超跌 {n2} 笔")
    elif args.cmd == "step":
        step(args.date)
        print("推进完成")
    elif args.cmd == "report":
        print(report(args.date))
    elif args.cmd == "run":
        return cmd_run(args.date)
    return 0


if __name__ == "__main__":
    sys.exit(main())