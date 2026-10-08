#!/usr/bin/env python3
"""手动模拟盘（2026-10-08 新建）：100 万初始资金、用户指令驱动。

与机械模拟盘（paper_sim.py，20:40 那个）的关系——用户 2026-10-08 拍板：
  - 同一个账本库 data/paper_sim.db，只加 manual_* 新表，不新建库（D1 紧俏，本地同理）
  - 卖出体系完全同一套：唯一实现在库 IgnitionPosition.step（C2：撞金牛上沿全清 +
    max(硬10%, 滚动30根低点)），本脚本只喂数据，绝不抄第二份规则
  - 建仓由用户指令驱动（从韩立推送的票中选，来源打标签但不设硬性白名单）
  - 成交价限定：买入 = 当日收盘（--exec close）或次日开盘（--exec next_open）；
    卖出 = 收盘价。其他价格一律不收
  - 现金硬约束：100 万封顶，买入挂单即预留额度，现金不足拒绝
  - 股数按 A 股整手（100 股）向下取整（与机械盘按金额除尽的差异，有意为之，贴近实盘）
  - 手动卖出遵守 T+1：当日买入的股票当日不可卖
  - 停牌：次日开盘单顺延至复牌日开盘；收盘价单当日无行情则作废（rejected）
  - 【2026-10-08 二改】规则触发（止损/移动止盈/上沿出场）**不再自动平仓**，只在日报挂
    "⚠️ 待确认"，等用户「卖出 XX」确认或「继续持有 XX」（hold）忽略；
    同一原因连续触发归并为一个 episode，忽略后该 episode 不再报警，
    条件解除后再次成立（新 episode）才重新报警。规则本体仍在库内唯一实现。

用法：
    manual_sim.py buy  --code 300353.SZ --amount 8 --exec close --source 回马枪 [--note ...] [--date YYYYMMDD]
    manual_sim.py sell --code 300353.SZ [--position-id N] [--qty N] [--reason "..."] [--date YYYYMMDD]
    manual_sim.py cancel --order-id N
    manual_sim.py orders
    manual_sim.py step --date YYYYMMDD
    manual_sim.py report [--date YYYYMMDD]
    manual_sim.py run --date YYYYMMDD    # step + report（cron 用）

amount 口径：小于 1000 视为「万」（8 → 8 万），否则为元。
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import paper_sim as ps  # noqa: E402  复用：后复权取数/C2重放/交易日天数/费用口径（卖出规则唯一实现仍在库内）

DB = ps.DB  # stock.db
SIM_DB = Path(os.environ.get("MANUAL_SIM_DB") or (ROOT / "data" / "paper_sim.db"))
INITIAL_CAPITAL = 1_000_000.0

EXEC_ALIASES = {"close": "close", "t_close": "close", "收盘": "close",
                "next_open": "next_open", "次日开盘": "next_open", "开盘": "next_open"}

MANUAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS manual_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital REAL NOT NULL,
    cash REAL NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS manual_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_date TEXT NOT NULL,           -- 指令日 YYYYMMDD（next_open 的成交日为其后首个交易日）
    side TEXT NOT NULL,                 -- buy / sell
    ts_code TEXT NOT NULL,
    name TEXT,
    exec_type TEXT NOT NULL,            -- close / next_open
    amount REAL,                        -- buy：预算金额（元，含费上限）
    qty INTEGER,                        -- sell：股数（空 = 全部）
    position_id INTEGER,                -- sell：指定平掉的持仓（多笔同票时必填）
    reason TEXT,                        -- sell：手动卖出原因
    source TEXT,                        -- buy：来源标签
    note TEXT,
    status TEXT DEFAULT 'pending',      -- pending / filled / cancelled / rejected
    fill_date TEXT,
    fill_price_adj REAL,
    fill_price_raw REAL,
    fill_shares INTEGER,
    fill_amount REAL,
    fee REAL,
    reject_reason TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS manual_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    ts_code TEXT NOT NULL,
    name TEXT,
    source TEXT,
    exec_type TEXT,
    entry_date TEXT NOT NULL,
    entry_price_adj REAL NOT NULL,
    entry_price_raw REAL,
    shares INTEGER NOT NULL,
    cost REAL NOT NULL,                 -- 含买入佣金（未复权真金白银）
    stop_line_adj REAL,
    status TEXT DEFAULT 'open',         -- open / closed
    exit_date TEXT,
    exit_price_adj REAL,
    exit_price_raw REAL,
    exit_reason TEXT,                   -- 手动（...）——唯一平仓途径是用户指令
    proceeds REAL,
    pnl REAL,
    ret REAL,
    exit_pending INTEGER DEFAULT 0,     -- 1=规则触发待人工确认（2026-10-08 用户拍板：不自动平仓）
    trigger_date TEXT,                  -- 触发 episode 首日
    trigger_price_adj REAL,
    trigger_price_raw REAL,
    trigger_reason TEXT,                -- 止损/移动止盈/上沿压制·全清/上沿压制·减半
    last_dismissed_date TEXT            -- 已忽略 episode 的首日；≤该日开始的 episode 不再报警
);
CREATE TABLE IF NOT EXISTS manual_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    ts_code TEXT, name TEXT, source TEXT,
    entry_date TEXT, exit_date TEXT,
    entry_price_adj REAL, exit_price_adj REAL,
    entry_price_raw REAL, exit_price_raw REAL,
    shares INTEGER, cost REAL, proceeds REAL,
    pnl REAL, ret REAL, days INTEGER, reason TEXT
);
CREATE TABLE IF NOT EXISTS manual_snapshot (
    trade_date TEXT PRIMARY KEY,
    cash REAL, market_value REAL, net_value REAL,
    realized_pnl REAL, n_open INTEGER,
    bought_today INTEGER, sold_today INTEGER
);
"""


def now_str() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(SIM_DB)
    con.executescript(MANUAL_SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """旧库补列（2026-10-08 二改：触发待确认字段）。幂等。"""
    cols = {r[1] for r in con.execute("PRAGMA table_info(manual_positions)")}
    if not cols:
        return
    for col, ddl in [("exit_pending", "INTEGER DEFAULT 0"), ("trigger_date", "TEXT"),
                     ("trigger_price_adj", "REAL"), ("trigger_price_raw", "REAL"),
                     ("trigger_reason", "TEXT"), ("last_dismissed_date", "TEXT")]:
        if col not in cols:
            con.execute(f"ALTER TABLE manual_positions ADD COLUMN {col} {ddl}")
    con.commit()


def stock_con() -> sqlite3.Connection:
    return sqlite3.connect(DB)


def rows_as_dicts(con: sqlite3.Connection, cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_name(scon: sqlite3.Connection, code: str) -> str:
    row = scon.execute("SELECT name FROM stock_basic WHERE ts_code=?", (code,)).fetchone()
    return (row[0] if row and row[0] else code)


def is_trade_date(scon: sqlite3.Connection, date: str) -> bool:
    row = scon.execute("SELECT is_open FROM trade_cal WHERE cal_date=?", (date,)).fetchone()
    if row:
        return row[0] == 1
    # trade_cal 只同步到当天：超出范围按周末判定（weekday 假定为交易日；
    # 若实际是节假日，成交时会以"当日无行情"如实作废）
    try:
        return datetime.strptime(date, "%Y%m%d").weekday() < 5
    except ValueError:
        return False


def account(con: sqlite3.Connection) -> dict:
    row = con.execute("SELECT initial_capital, cash FROM manual_account WHERE id=1").fetchone()
    if row is None:
        con.execute("INSERT INTO manual_account (id, initial_capital, cash, created_at) VALUES (1,?,?,?)",
                    (INITIAL_CAPITAL, INITIAL_CAPITAL, now_str()))
        con.commit()
        return {"initial_capital": INITIAL_CAPITAL, "cash": INITIAL_CAPITAL}
    return {"initial_capital": row[0], "cash": row[1]}


def pending_buy_total(con: sqlite3.Connection) -> float:
    return float(con.execute(
        "SELECT COALESCE(SUM(amount),0) FROM manual_orders WHERE side='buy' AND status='pending'"
    ).fetchone()[0])


def available_cash(con: sqlite3.Connection) -> float:
    return account(con)["cash"] - pending_buy_total(con)


# ---------------------------------------------------------------- 建仓/平仓执行

def close_px(scon: sqlite3.Connection, code: str, date: str) -> float | None:
    """后复权收盘价；无行情返回 None。"""
    row = scon.execute(
        "SELECT d.close * a.adj_factor FROM daily d JOIN adj_factor a "
        "ON a.ts_code=d.ts_code AND a.trade_date=d.trade_date "
        "WHERE d.ts_code=? AND d.trade_date=?", (code, date)).fetchone()
    return float(row[0]) if row and row[0] else None


def open_px(scon: sqlite3.Connection, code: str, date: str) -> float | None:
    row = scon.execute(
        "SELECT d.open * a.adj_factor FROM daily d JOIN adj_factor a "
        "ON a.ts_code=d.ts_code AND a.trade_date=d.trade_date "
        "WHERE d.ts_code=? AND d.trade_date=?", (code, date)).fetchone()
    return float(row[0]) if row and row[0] else None


def last_px_af(scon: sqlite3.Connection, code: str, date: str) -> tuple[float | None, float | None]:
    """date（含）前最后可见收盘（后复权）及其复权因子；停牌时自然回看，无数据返回 (None, None)。"""
    row = scon.execute(
        "SELECT d.close * a.adj_factor, a.adj_factor FROM daily d JOIN adj_factor a "
        "ON a.ts_code=d.ts_code AND a.trade_date=d.trade_date "
        "WHERE d.ts_code=? AND d.trade_date<=? ORDER BY d.trade_date DESC LIMIT 1",
        (code, date)).fetchone()
    return (float(row[0]), float(row[1])) if row and row[0] else (None, None)


def adj_at(scon: sqlite3.Connection, code: str, date: str) -> float:
    return ps.adj_factor_at(scon, code, date)


def fill_buy(con: sqlite3.Connection, scon: sqlite3.Connection, o: dict,
             fill_date: str, price_adj: float) -> None:
    af = adj_at(scon, o["ts_code"], fill_date)
    price_raw = price_adj / af
    # 真金白银一律按未复权价记账（除权因子只用于收益率与 C2 内核，不进现金）
    shares = int(o["amount"] / (price_raw * (1 + ps.FEE)) // 100) * 100
    if shares <= 0:
        con.execute("UPDATE manual_orders SET status='rejected', reject_reason=? WHERE id=?",
                    (f"金额不足一手（成交价 {price_raw:.2f} 元）", o["id"]))
        return
    cost = shares * price_raw
    fee = cost * ps.FEE
    con.execute("UPDATE manual_account SET cash = cash - ? WHERE id=1", (cost + fee,))
    cur = con.execute(
        "INSERT INTO manual_positions (order_id, ts_code, name, source, exec_type, entry_date, "
        "entry_price_adj, entry_price_raw, shares, cost, stop_line_adj) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (o["id"], o["ts_code"], o["name"], o.get("source") or "用户自选", o["exec_type"],
         fill_date, price_adj, price_raw, shares, cost + fee, price_adj * 0.9))
    con.execute(
        "UPDATE manual_orders SET status='filled', fill_date=?, fill_price_adj=?, fill_price_raw=?, "
        "fill_shares=?, fill_amount=?, fee=? WHERE id=?",
        (fill_date, price_adj, price_raw, shares, cost, fee, o["id"]))
    print(f"  ✅ 买入成交 {o['name']} {o['ts_code']} {shares}股 × {price_raw:.2f} 元 "
          f"= {cost:,.0f} 元 + 佣金 {fee:,.0f} 元（{fill_date} {'收盘' if o['exec_type']=='close' else '开盘'}）"
          f"｜来源：{o.get('source') or '用户自选'}｜持仓id={cur.lastrowid}")


def fill_sell(con: sqlite3.Connection, scon: sqlite3.Connection, o: dict,
              fill_date: str, price_adj: float) -> None:
    if o.get("position_id"):
        pos_row = con.execute("SELECT * FROM manual_positions WHERE id=?", (o["position_id"],)).fetchone()
    else:
        lots = con.execute("SELECT * FROM manual_positions WHERE ts_code=? AND status='open' ORDER BY id",
                           (o["ts_code"],)).fetchall()
        pos_row = lots[0] if len(lots) == 1 else None
        if pos_row is None:
            con.execute("UPDATE manual_orders SET status='rejected', reject_reason=? WHERE id=?",
                        (f"该票有 {len(lots)} 笔在持仓，需指定 --position-id（ids="
                         + ",".join(str(r[0]) for r in lots) + "）", o["id"]))
            return
    if pos_row is None:
        con.execute("UPDATE manual_orders SET status='rejected', reject_reason='持仓不存在或已平仓' WHERE id=?",
                    (o["id"],))
        return
    cols = [d[0] for d in con.execute("SELECT * FROM manual_positions LIMIT 0").description]
    pos = dict(zip(cols, pos_row))
    if pos["status"] != "open":
        con.execute("UPDATE manual_orders SET status='rejected', reject_reason='该持仓已平仓' WHERE id=?",
                    (o["id"],))
        return
    if fill_date <= pos["entry_date"]:
        con.execute("UPDATE manual_orders SET status='rejected', reject_reason='T+1：当日买入不可当日卖' WHERE id=?",
                    (o["id"],))
        return
    qty = o["qty"] or pos["shares"]
    if qty <= 0 or qty > pos["shares"] or qty % 100 != 0:
        con.execute("UPDATE manual_orders SET status='rejected', reject_reason=? WHERE id=?",
                    (f"卖出股数非法（在持 {pos['shares']}，须 100 整数倍）", o["id"]))
        return
    af = adj_at(scon, o["ts_code"], fill_date)
    price_raw = price_adj / af
    proceeds = qty * price_raw              # 真金白银（未复权）
    fee = proceeds * (ps.FEE + ps.STAMP)
    cost_part = pos["cost"] * qty / pos["shares"]
    pnl = proceeds - fee - cost_part
    ret = price_adj / pos["entry_price_adj"] - 1.0   # 收益率按后复权口径（含分红除权）
    days = ps.trading_days(scon, pos["entry_date"], fill_date)
    left = pos["shares"] - qty
    if left == 0:
        con.execute(
            "UPDATE manual_positions SET status='closed', exit_date=?, exit_price_adj=?, "
            "exit_price_raw=?, exit_reason=?, proceeds=?, pnl=?, ret=?, shares=0, exit_pending=0 WHERE id=?",
            (fill_date, price_adj, price_raw, f"手动（{o.get('reason') or '指令卖出'}）",
             proceeds - fee, pnl, ret, pos["id"]))
    else:
        con.execute("UPDATE manual_positions SET shares=? WHERE id=?", (left, pos["id"]))
    con.execute(
        "INSERT INTO manual_trades (position_id, ts_code, name, source, entry_date, exit_date, "
        "entry_price_adj, exit_price_adj, entry_price_raw, exit_price_raw, shares, cost, proceeds, "
        "pnl, ret, days, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pos["id"], pos["ts_code"], pos["name"], pos["source"], pos["entry_date"], fill_date,
         pos["entry_price_adj"], price_adj, pos["entry_price_raw"], price_raw,
         qty, cost_part, proceeds - fee, pnl, ret, days, f"手动（{o.get('reason') or '指令卖出'}）"))
    con.execute("UPDATE manual_account SET cash = cash + ? WHERE id=1", (proceeds - fee,))
    con.execute(
        "UPDATE manual_orders SET status='filled', fill_date=?, fill_price_adj=?, fill_price_raw=?, "
        "fill_shares=?, fill_amount=?, fee=? WHERE id=?",
        (fill_date, price_adj, price_raw, qty, proceeds, fee, o["id"]))
    print(f"  ✅ 卖出成交 {pos['name']} {pos['ts_code']} {qty}股 × {price_raw:.2f} 元（{fill_date} 收盘）"
          f"｜{ret*100:+.2f}% 盈亏 {pnl:+,.0f} 元（{o.get('reason') or '指令卖出'}，持 {days} 天）"
          + (f"｜剩余 {left} 股继续持有" if left else ""))


def replay_episodes(scon: sqlite3.Connection, pos: dict, date: str):
    """重放 C2 内核收集触发 episode（2026-10-08 二改：不再自动平仓）。

    状态全程逐根推进、不中断——规则仍唯一在库 IgnitionPosition.step，这里只喂数据。
    同一原因**连续**触发归并成一个 episode；返回 (algo, episodes)，
    episodes = [(episode首日, reason, 首日收盘复权价), ...] 按时间升序。
    """
    bars = ps.load_bars_adj(scon, pos["ts_code"], date, n=ps.REPLAY_BARS)
    if bars is None or len(bars) < 2:
        return None, []
    idx = bars.index[bars["trade_date"] == pos["entry_date"]]
    if len(idx) == 0:
        return None, []
    i0 = int(idx[0])
    algo = ps.IgnitionPosition.open_at(pos["entry_price_adj"], i0,
                                       float(bars["high"].iloc[i0]), bars["low"].values)
    episodes: list[tuple[str, str, float]] = []
    cur: tuple[str, str, float] | None = None
    for i in range(i0 + 1, len(bars)):
        act = algo.step(i, float(bars["open"].iloc[i]), float(bars["high"].iloc[i]),
                        float(bars["low"].iloc[i]), float(bars["close"].iloc[i]),
                        float(bars["upper"].iloc[i]), bars["low"].values)
        d = str(bars["trade_date"].iloc[i])
        if act:
            if cur is not None and cur[1] == act[1]:
                cur = (cur[0], act[1], cur[2])       # 同一 episode 延续，保留首日
            else:
                cur = (d, act[1], float(bars["close"].iloc[i]))
                episodes.append(cur)
        else:
            cur = None
    return algo, episodes


# ---------------------------------------------------------------- step

def try_fill_orders(con: sqlite3.Connection, scon: sqlite3.Connection, date: str) -> None:
    # 1) next_open 买单：order_date 之后的首个有行情交易日开盘成交（停牌顺延）
    for o in rows_as_dicts(con, con.execute(
            "SELECT * FROM manual_orders WHERE side='buy' AND status='pending' AND "
            "exec_type='next_open' AND order_date < ?", (date,))):
        px = open_px(scon, o["ts_code"], date)
        if px:
            fill_buy(con, scon, o, date, px)
        # 无行情（停牌）→ 继续挂起，顺延到下一交易日开盘
    # 2) close 单（买/卖）：order_date 当日收盘成交；数据未同步则等，隔日仍无行情则作废
    for o in rows_as_dicts(con, con.execute(
            "SELECT * FROM manual_orders WHERE status='pending' AND exec_type='close' AND "
            "order_date <= ?", (date,))):
        px = close_px(scon, o["ts_code"], o["order_date"])
        if px:
            if o["side"] == "buy":
                fill_buy(con, scon, o, o["order_date"], px)
            else:
                fill_sell(con, scon, o, o["order_date"], px)
        elif o["order_date"] < date:
            con.execute("UPDATE manual_orders SET status='rejected', reject_reason='当日无行情（停牌/节假日）' WHERE id=?",
                        (o["id"],))
            print(f"  ⚠️ 订单 {o['id']} 作废：{o['ts_code']} {o['order_date']} 无行情（停牌/节假日）")
    con.commit()


def step(date: str) -> None:
    con = connect()
    scon = stock_con()
    account(con)
    print(f"⚙️ 手动模拟盘推进 {date}")

    # 1. 成交挂单（手动收盘卖单在此优先成交——用户明确指令优先于规则触发；
    #    若同一持仓同日规则也要平，剩余股数由第 2 步内核按纪律处理，不会双重卖出）
    try_fill_orders(con, scon, date)

    # 2. C2 内核重放：只检测触发、标记待确认，**不自动平仓**（用户 2026-10-08 拍板）
    for pos in rows_as_dicts(con, con.execute("SELECT * FROM manual_positions WHERE status='open' ORDER BY id")):
        algo, episodes = replay_episodes(scon, pos, date)
        latest = episodes[-1] if episodes else None
        dismissed = pos.get("last_dismissed_date") or ""
        if latest and latest[0] > dismissed:
            # 新 episode（或尚无人理会的旧 episode）→ 挂"待确认"
            af_t = adj_at(scon, pos["ts_code"], latest[0])
            con.execute(
                "UPDATE manual_positions SET exit_pending=1, trigger_date=?, trigger_price_adj=?, "
                "trigger_price_raw=?, trigger_reason=? WHERE id=?",
                (latest[0], latest[2], latest[2] / af_t if af_t else None,
                 ps.REASON_ZH.get(latest[1], latest[1]), pos["id"]))
        elif pos.get("exit_pending"):
            # 已忽略（episode 首日 ≤ last_dismissed）→ 清除待确认标记
            con.execute("UPDATE manual_positions SET exit_pending=0 WHERE id=?", (pos["id"],))
        if algo is not None:
            con.execute("UPDATE manual_positions SET stop_line_adj=? WHERE id=?",
                        (algo.stop_line, pos["id"]))
    con.commit()

    # 3. 快照（市值按未复权真金白银口径）
    cash = account(con)["cash"]
    mv = 0.0
    for pos in rows_as_dicts(con, con.execute("SELECT * FROM manual_positions WHERE status='open'")):
        px, af = last_px_af(scon, pos["ts_code"], date)
        if px is None or af is None:
            px, af = pos["entry_price_adj"], 1.0
        mv += pos["shares"] * px / af
    realized = float(con.execute("SELECT COALESCE(SUM(pnl),0) FROM manual_trades").fetchone()[0])
    n_open = con.execute("SELECT COUNT(*) FROM manual_positions WHERE status='open'").fetchone()[0]
    bought = con.execute("SELECT COUNT(*) FROM manual_orders WHERE side='buy' AND status='filled' AND fill_date=?",
                         (date,)).fetchone()[0]
    sold = con.execute("SELECT COUNT(*) FROM manual_trades WHERE exit_date=?", (date,)).fetchone()[0]
    con.execute("INSERT OR REPLACE INTO manual_snapshot VALUES (?,?,?,?,?,?,?,?)",
                (date, cash, mv, cash + mv, realized, n_open, bought, sold))
    con.commit()
    scon.close()
    con.close()


# ---------------------------------------------------------------- report

def fmt_pct(x, signed=True) -> str:
    if x is None or not np.isfinite(x):
        return "-"
    return f"{x:+.2f}%" if signed else f"{x:.2f}%"


def report(date: str) -> str:
    con = connect()
    scon = stock_con()
    acc = account(con)
    out: list[str] = []
    d = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    out.append(f"💰 手动模拟盘（100万） {d}")

    # 挂单
    pends = rows_as_dicts(con, con.execute(
        "SELECT * FROM manual_orders WHERE status='pending' ORDER BY id"))
    if pends:
        out.append(f"\n【待成交挂单】{len(pends)} 笔")
        for o in pends:
            kind = "买入" if o["side"] == "buy" else "卖出"
            desc = f"{(o['amount'] or 0)/10000:.0f} 万" if o["side"] == "buy" else f"{o['qty'] or '全部'} 股"
            out.append(f"● {kind} {o['name']} {o['ts_code']} {desc}"
                       f"（{'次日开盘' if o['exec_type']=='next_open' else '收盘价'}，{o['order_date']} 下单）")

    # 今日成交（买入单；卖出单统一在下面【今日平仓】段展示盈亏明细）
    fills = rows_as_dicts(con, con.execute(
        "SELECT * FROM manual_orders WHERE status='filled' AND fill_date=? AND side='buy' ORDER BY id", (date,)))
    if fills:
        out.append(f"\n【今日买入】{len(fills)} 笔")
        for o in fills:
            out.append(f"● 买入 {o['name']} {o['ts_code']} {o['fill_shares']} 股 × "
                       f"{o['fill_price_raw']:.2f} 元 = {o['fill_amount']:,.0f} 元"
                       f"（{'收盘' if o['exec_type']=='close' else '次日开盘'}）｜来源：{o['source'] or '用户自选'}")

    # 今日平仓（规则触发 + 手动）
    closed = rows_as_dicts(con, con.execute(
        "SELECT * FROM manual_trades WHERE exit_date=? ORDER BY id", (date,)))
    if closed:
        out.append(f"\n【今日平仓】{len(closed)} 笔")
        for t in closed:
            out.append(f"● {t['name']} {t['ts_code']} {t['entry_date'][4:6]}/{t['entry_date'][6:8]}"
                       f"→{t['exit_date'][4:6]}/{t['exit_date'][6:8]} {fmt_pct(t['ret']*100)} "
                       f"盈亏 {t['pnl']:+,.0f} 元（{t['reason']}，持 {t['days']} 天）")
    if not fills and not closed:
        out.append("\n【今日无成交】")

    # 持仓
    holds = rows_as_dicts(con, con.execute("SELECT * FROM manual_positions WHERE status='open' ORDER BY id"))
    cash = acc["cash"]
    mv_total = 0.0
    mv_by_code: dict[str, float] = {}
    px_cache: dict[int, tuple[float, float]] = {}
    for pos in holds:
        px, af = last_px_af(scon, pos["ts_code"], date)
        if px is None or af is None:
            px, af = pos["entry_price_adj"] or 0.0, 1.0
            mv = pos["shares"] * (pos["entry_price_raw"] or px)   # 无行情兜底：按未复权成本价
        else:
            mv = pos["shares"] * px / af                          # 未复权真金白银
        mv_total += mv
        mv_by_code[pos["ts_code"]] = mv_by_code.get(pos["ts_code"], 0.0) + mv
        px_cache[pos["id"]] = (px, af)
    net = cash + mv_total
    out.append(f"\n【账户】现金 {cash:,.0f}｜持仓市值 {mv_total:,.0f}｜总净值 {net:,.0f}"
               f"（{fmt_pct(net/acc['initial_capital']*100-100)}，初始 {acc['initial_capital']:,.0f}）")

    # 规则触发待确认（2026-10-08 二改：不自动平仓，等人工决定）
    pendings = rows_as_dicts(con, con.execute(
        "SELECT * FROM manual_positions WHERE status='open' AND exit_pending=1 ORDER BY id"))
    if pendings:
        out.append(f"\n【⚠️ 规则触发·待确认】{len(pendings)} 笔（不会自动平仓，等你决定）")
        for pos in pendings:
            px, af = last_px_af(scon, pos["ts_code"], date)
            px_raw = px / af if px and af else None
            ret_now = px / pos["entry_price_adj"] - 1.0 if px else None
            out.append(f"● {pos['name']} {pos['ts_code']} {pos['trigger_reason']}"
                       f"（{pos['trigger_date'][4:6]}/{pos['trigger_date'][6:8]} 收盘 "
                       f"{pos['trigger_price_raw']:.2f} 触发，现价 {px_raw:.2f}，浮盈 {fmt_pct(ret_now*100)}）"
                       f"→ 回复「卖出 {pos['name']}」确认，或「继续持有 {pos['name']}」忽略")

    if holds:
        out.append(f"\n【当前持仓】{len(holds)} 笔｜总仓位 {fmt_pct(mv_total/net*100, signed=False)}")
        warn_stops = []
        for pos in holds:
            px, af = px_cache[pos["id"]]
            px_raw = px / af
            ret = px / pos["entry_price_adj"] - 1.0
            stop_raw = pos["stop_line_adj"] / af if pos["stop_line_adj"] else None
            stop_dist = (px / pos["stop_line_adj"] - 1.0) * 100 if pos["stop_line_adj"] else None
            weight = (pos["shares"] * px_raw) / net * 100
            pend_flag = " ⚠️待确认" if pos["exit_pending"] else ""
            out.append(f"● {pos['name']} {pos['ts_code']} {pos['entry_date'][4:6]}/{pos['entry_date'][6:8]}买"
                       f"（{'收盘' if pos['exec_type']=='close' else '开盘'}）成本 {pos['entry_price_raw']:.2f} "
                       f"现价 {px_raw:.2f} 浮盈 {fmt_pct(ret*100)} 止损 {stop_raw:.2f}（距 {fmt_pct(stop_dist)}）"
                       f"｜仓位 {weight:.1f}%｜{pos['source'] or '用户自选'}{pend_flag}")
            if stop_dist is not None and stop_dist < 3.0:
                warn_stops.append(f"● {pos['name']} {pos['ts_code']} 止损 {stop_raw:.2f}（距 {fmt_pct(stop_dist)}）⚠️")
        if warn_stops:
            out.append("\n【贴近止损预警】")
            out.extend(warn_stops)
        conc = [(c, w) for c, w in ((c, mv/net*100) for c, mv in mv_by_code.items()) if w > 20.0]
        if mv_total / net > 0.8:
            out.append("⚠️ 集中度提示：总仓位已超 80%")
        for c, w in conc:
            row = scon.execute("SELECT name FROM stock_basic WHERE ts_code=?", (c,)).fetchone()
            out.append(f"⚠️ 集中度提示：{(row[0] if row else c)} 单票 {w:.1f}%")
    else:
        out.append("\n【当前持仓】空仓")

    # 已平仓累计
    ts = rows_as_dicts(con, con.execute("SELECT * FROM manual_trades"))
    if ts:
        wins = sum(1 for t in ts if t["ret"] > 0)
        total_pnl = sum(t["pnl"] for t in ts)
        avg = float(np.mean([t["ret"] for t in ts])) * 100
        out.append(f"\n【已平仓累计】{len(ts)} 笔 胜率 {wins/len(ts)*100:.0f}% "
                   f"累计盈亏 {total_pnl:+,.0f} 元 平均每笔 {avg:+.2f}%")
    scon.close()
    con.close()
    return "\n".join(out)


# ---------------------------------------------------------------- 指令

def parse_amount(s: str) -> float:
    v = float(s)
    return v * 10000 if v < 1000 else v


def norm_code(s: str) -> str:
    s = s.strip().upper()
    if re.fullmatch(r"\d{6}", s):
        return s + (".SH" if s.startswith(("6", "9", "5")) else (".BJ" if s.startswith(("4", "8")) else ".SZ"))
    if not re.fullmatch(r"\d{6}\.(SZ|SH|BJ)", s):
        raise ValueError(f"代码格式不认识：{s}")
    return s


def cmd_buy(args) -> int:
    code = norm_code(args.code)
    amount = parse_amount(args.amount)
    exec_type = EXEC_ALIASES.get(args.exec.lower())
    if exec_type is None:
        print(f"❌ exec 只支持 close（当日收盘）/ next_open（次日开盘），收到：{args.exec}")
        return 2
    date = args.date or today()
    con = connect()
    scon = stock_con()
    if not scon.execute("SELECT 1 FROM stock_basic WHERE ts_code=?", (code,)).fetchone():
        print(f"❌ {code} 不在行情库 stock_basic 里，检查代码")
        return 2
    if exec_type == "close" and not is_trade_date(scon, date):
        print(f"❌ {date} 非交易日，收盘价单不收（要下一交易日开盘成交用 --exec next_open）")
        return 2
    avail = available_cash(con)
    if amount > avail + 1e-6:
        print(f"❌ 现金不足：预算 {amount:,.0f} 元 > 可用 {avail:,.0f} 元（已扣未成交挂单预留）")
        return 2
    name = get_name(scon, code)
    con.execute(
        "INSERT INTO manual_orders (order_date, side, ts_code, name, exec_type, amount, source, note, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (date, "buy", code, name, exec_type, amount, args.source or "用户自选", args.note, now_str()))
    con.commit()
    print(f"📝 挂单：买入 {name} {code} 预算 {amount/10000:.1f} 万"
          f"（{'当日收盘' if exec_type=='close' else '次日开盘'}，{date} 下单）｜来源：{args.source or '用户自选'}")
    # 收盘单且当日行情已在库 → 立即成交
    if exec_type == "close" and close_px(scon, code, date):
        o = rows_as_dicts(con, con.execute(
            "SELECT * FROM manual_orders WHERE side='buy' AND status='pending' AND ts_code=? "
            "AND order_date=? ORDER BY id DESC LIMIT 1", (code, date)))[0]
        fill_buy(con, scon, o, date, close_px(scon, code, date))
        con.commit()
    scon.close()
    con.close()
    return 0


def cmd_sell(args) -> int:
    code = norm_code(args.code)
    date = args.date or today()
    con = connect()
    scon = stock_con()
    name = get_name(scon, code)
    lots = rows_as_dicts(con, con.execute(
        "SELECT * FROM manual_positions WHERE ts_code=? AND status='open' ORDER BY id", (code,)))
    if not lots:
        print(f"❌ {name} {code} 无在持仓位")
        return 2
    if not is_trade_date(scon, date):
        print(f"❌ {date} 非交易日，卖出收盘价单不收")
        return 2
    pid = args.position_id
    if pid is None and len(lots) > 1:
        print(f"❌ {name} {code} 有 {len(lots)} 笔在持仓，需指定 --position-id：")
        for p in lots:
            print(f"   id={p['id']} {p['entry_date']}买 {p['shares']}股 成本 {p['entry_price_raw']:.2f}（{p['source']}）")
        return 2
    con.execute(
        "INSERT INTO manual_orders (order_date, side, ts_code, name, exec_type, qty, position_id, reason, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (date, "sell", code, name, "close", args.qty, pid, args.reason or "指令卖出", now_str()))
    con.commit()
    print(f"📝 挂单：卖出 {name} {code} {args.qty or '全部'} 股（收盘价，{date} 下单）")
    if close_px(scon, code, date):
        o = rows_as_dicts(con, con.execute(
            "SELECT * FROM manual_orders WHERE side='sell' AND status='pending' AND ts_code=? "
            "AND order_date=? ORDER BY id DESC LIMIT 1", (code, date)))[0]
        fill_sell(con, scon, o, date, close_px(scon, code, date))
        con.commit()
    scon.close()
    con.close()
    return 0


def cmd_hold(args) -> int:
    """忽略本次触发（继续持有）：记录被忽略 episode 首日，同 episode 不再报警。"""
    con = connect()
    scon = stock_con()
    code = norm_code(args.code)
    lots = rows_as_dicts(con, con.execute(
        "SELECT * FROM manual_positions WHERE ts_code=? AND status='open' AND exit_pending=1 ORDER BY id",
        (code,)))
    if not lots:
        print(f"❌ {get_name(scon, code)} {code} 没有「待确认」的规则触发")
        return 2
    if args.position_id:
        pos = next((p for p in lots if p["id"] == args.position_id), None)
        if pos is None:
            print(f"❌ position-id={args.position_id} 不在该票的待确认列表（ids="
                  + ",".join(str(p['id']) for p in lots) + "）")
            return 2
    else:
        if len(lots) > 1:
            print(f"❌ {code} 有 {len(lots)} 笔待确认，需指定 --position-id：")
            for p in lots:
                print(f"   id={p['id']} {p['trigger_date']} {p['trigger_reason']} "
                      f"触发价 {p['trigger_price_raw']:.2f}")
            return 2
        pos = lots[0]
    con.execute("UPDATE manual_positions SET exit_pending=0, last_dismissed_date=? WHERE id=?",
                (pos["trigger_date"], pos["id"]))
    con.commit()
    print(f"🙋 已忽略 {pos['name']} {pos['trigger_reason']}（{pos['trigger_date']} 触发）——继续持有；"
          f"条件若解除后再次成立会重新报警")
    scon.close()
    con.close()
    return 0


def cmd_cancel(args) -> int:
    con = connect()
    cur = con.execute("UPDATE manual_orders SET status='cancelled' WHERE id=? AND status='pending'",
                      (args.order_id,))
    con.commit()
    if cur.rowcount:
        print(f"🗑️ 订单 {args.order_id} 已撤销（预留额度释放）")
    else:
        print(f"❌ 订单 {args.order_id} 不存在或不在 pending 状态")
    con.close()
    return 0 if cur.rowcount else 2


def cmd_orders() -> int:
    con = connect()
    rows = rows_as_dicts(con, con.execute(
        "SELECT * FROM manual_orders ORDER BY id DESC LIMIT 20"))
    if not rows:
        print("（无订单记录）")
    for o in rows:
        desc = f"{(o['amount'] or 0)/10000:.1f}万" if o["side"] == "buy" else f"{o['qty'] or '全部'}股"
        print(f"#{o['id']} {o['order_date']} {o['side'].upper():4s} {o['name']} {o['ts_code']} {desc} "
              f"[{o['exec_type']}] {o['status']}"
              + (f" → {o['fill_date']} @{o['fill_price_raw']:.2f} × {o['fill_shares']}" if o['status'] == 'filled' else "")
              + (f"（{o['reject_reason']}）" if o['reject_reason'] else ""))
    con.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="手动模拟盘（100万）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("buy", help="挂买入单")
    p.add_argument("--code", required=True)
    p.add_argument("--amount", required=True, help="金额，<1000 视为万")
    p.add_argument("--exec", dest="exec", required=True, help="close=当日收盘 / next_open=次日开盘")
    p.add_argument("--source", default="用户自选")
    p.add_argument("--note")
    p.add_argument("--date")
    p = sub.add_parser("sell", help="挂卖出单（收盘价）")
    p.add_argument("--code", required=True)
    p.add_argument("--position-id", type=int)
    p.add_argument("--qty", type=int, help="股数，默认全部")
    p.add_argument("--reason", default="指令卖出")
    p.add_argument("--date")
    p = sub.add_parser("cancel", help="撤销挂单")
    p.add_argument("--order-id", type=int, required=True)
    p = sub.add_parser("hold", help="忽略规则触发（继续持有）")
    p.add_argument("--code", required=True)
    p.add_argument("--position-id", type=int)
    sub.add_parser("orders", help="最近订单")
    p = sub.add_parser("step", help="推进一天")
    p.add_argument("--date", required=True)
    p = sub.add_parser("report", help="报表")
    p.add_argument("--date")
    p = sub.add_parser("run", help="step + report（cron 用）")
    p.add_argument("--date", required=True)
    args = ap.parse_args()

    if args.cmd == "buy":
        return cmd_buy(args)
    if args.cmd == "sell":
        return cmd_sell(args)
    if args.cmd == "cancel":
        return cmd_cancel(args)
    if args.cmd == "hold":
        return cmd_hold(args)
    if args.cmd == "orders":
        return cmd_orders()
    if args.cmd == "step":
        step(args.date)
        print("推进完成")
        return 0
    if args.cmd == "report":
        print(report(args.date or today()))
        return 0
    if args.cmd == "run":
        step(args.date)
        print()
        print(report(args.date))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
