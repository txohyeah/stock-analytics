#!/usr/bin/env python3
"""大盘手绘线台账 —— 用户画线、agent 记账监控（2026-10-22 定稿）。

定位（用户拍板，勿越权）：
  - 线由用户手绘/口述，agent 只做台账 + 每日监控；失效**只提醒**，重画与否由用户定
    （假失效常见：第二天收回线内侧）。
  - 失效判定 = **收盘穿越**（压力线收盘站上 / 支撑线收盘跌破），盘中触碰不算数。
  - 线登记带 created_date（point-in-time 存档）：手画线本身含未来信息、不可回测，
    但"当天登记的线"从登记日起是干净的——台账日后自然积累失效案例。
  - 结构参考复用 pa-crypto-4h/pa_engine.py 的 mtr_anchor / _swing_*（零改动 import，
    判据改动必须改源模块，此处禁止复制代码防漂移）。
  - MACD 12/26/9 国内口径（EMA 递推，柱=2*(DIF-DEA)），只做状态标注不做提醒触发。
  - 纪律：只报状态、不给买卖建议；画线总量建议 ≤3 条（线越多"正好压住"越不值钱）。

表结构 market_lines（data/stock.db）：
  kind = pressure/support；line_type = slope/horizontal；
  斜线值 = p1_price + slope_per_day × 交易日序号差（序号取 000001.SH 日线）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# pa_engine（Brooks MTR 结构锚）零第三方依赖，直接 import 原实现
PA_ENGINE_DIR = "/root/.qwenpaw/workspaces/default/pa-crypto-4h"
if PA_ENGINE_DIR not in sys.path:
    sys.path.insert(0, PA_ENGINE_DIR)
from pa_engine import mtr_anchor, _swing_highs, _swing_lows  # noqa: E402

MACD_CODE = "000001.SH"          # 结构/MACD 都以上证指数日线为准
MACD_TOUCH_TOL = 0.005           # 盘中触线容差：高点距线 0.5% 内算"触线"


# ---------------- 表 + 种子线 ----------------

DDL = """
CREATE TABLE IF NOT EXISTS market_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,              -- pressure / support
  line_type TEXT NOT NULL,         -- slope / horizontal
  p1_date TEXT NOT NULL,           -- 锚点1 交易日（YYYYMMDD）
  p1_price REAL NOT NULL,
  p2_date TEXT,                    -- 锚点2（斜线用；水平线为 NULL）
  p2_price REAL,
  slope_per_day REAL NOT NULL,     -- 每交易日线值变化；水平线 = 0
  note TEXT DEFAULT '',
  status TEXT DEFAULT 'active',    -- active / archived
  created_date TEXT NOT NULL,      -- 登记日（point-in-time）
  archived_date TEXT
);
"""

# 2026-10-22 首批登记（用户口述 + 截图像素还原，勿改数值）
SEED_LINES = [
    {
        "name": "压力线A（5/14高点下行）",
        "kind": "pressure", "line_type": "slope",
        "p1_date": "20260514", "p1_price": 4258.86,
        "p2_date": "20261021", "p2_price": 3893.7,
        "slope_per_day": -3.4134,
        "note": "自 5/14 高点 4258.86 手绘下行；斜率由截图像素还原（≈-3.4 点/交易日）",
    },
    {
        "name": "支撑线B（3674政策位）",
        "kind": "support", "line_type": "horizontal",
        "p1_date": "20241008", "p1_price": 3674.4,
        "p2_date": None, "p2_price": None,
        "slope_per_day": 0.0,
        "note": "924 行情 V 反后第一高点（2024-10-08），2025 年突破后转支撑，政策性强支撑",
    },
]


def ensure_table(con: sqlite3.Connection) -> None:
    con.execute(DDL)
    for ln in SEED_LINES:
        con.execute(
            "INSERT OR IGNORE INTO market_lines "
            "(name, kind, line_type, p1_date, p1_price, p2_date, p2_price, slope_per_day, note, status, created_date) "
            "VALUES (:name,:kind,:line_type,:p1_date,:p1_price,:p2_date,:p2_price,:slope_per_day,:note,'active','20261022')",
            ln)
    con.commit()


# ---------------- 线值计算 ----------------

def _tmap(con: sqlite3.Connection) -> dict[str, int]:
    """上证指数全部交易日的序号映射（等距交易日坐标）。"""
    rows = con.execute("SELECT trade_date FROM index_daily WHERE ts_code='000001.SH' ORDER BY trade_date").fetchall()
    return {r[0]: i for i, r in enumerate(rows)}


def line_value(line: sqlite3.Row, date: str, tmap: dict[str, int]) -> float | None:
    """线在 date 的值；date 或锚点不在交易日序列时返回 None。"""
    i_d = tmap.get(date)
    i_1 = tmap.get(line["p1_date"])
    if i_d is None or i_1 is None:
        return None
    return line["p1_price"] + line["slope_per_day"] * (i_d - i_1)


# ---------------- ⑧ 手绘线监控 ----------------

def monitor_lines(con: sqlite3.Connection, date: str) -> list[str]:
    """对每条 active 线输出一行状态；穿越事件（首日/连续/回归）另起一行。"""
    ensure_table(con)
    tmap = _tmap(con)
    # 显式列名 zip：对裸 tuple 连接和 Row 连接都工作，不依赖 row_factory
    _cols = ["id", "name", "kind", "line_type", "p1_date", "p1_price", "p2_date", "p2_price",
             "slope_per_day", "note", "status", "created_date", "archived_date"]
    lines = [dict(zip(_cols, r)) for r in con.execute(
        "SELECT * FROM market_lines WHERE status='active' ORDER BY id").fetchall()]
    if not lines:
        return ["⑧画线台账：（无登记线）"]

    dates = sorted(d for d in tmap if d <= date)
    idx_date = {d: i for i, d in enumerate(dates)}
    # 监控窗口：最近 45 个交易日（覆盖"连续 N 天"展示足够）
    win = dates[max(0, len(dates) - 45):]
    px_rows = con.execute(
        "SELECT trade_date, close, high FROM index_daily WHERE ts_code='000001.SH' "
        "AND trade_date >= ? AND trade_date <= ?", (win[0], date)).fetchall()
    closes = {r[0]: r[1] for r in px_rows}
    highs = {r[0]: r[2] for r in px_rows}

    out = ["⑧画线台账（手绘，失效=收盘穿越，只提醒不催改）："]
    lr = con.execute("SELECT low FROM index_daily WHERE ts_code='000001.SH' AND trade_date=?",
                     (date,)).fetchone()
    for ln in lines:
        name = ln["name"]
        kind = ln["kind"]
        start = max(win[0], ln["p1_date"])   # 几何起点=p1；created_date 仅台账审计
        path = [d for d in win if d >= start]
        if date not in path or date not in closes:
            continue
        vals = {d: line_value(ln, d, tmap) for d in path}
        if vals.get(date) is None:
            continue

        def outside(d: str) -> bool:
            """收盘是否在线的失效侧（压力=上方；支撑=下方）。"""
            if vals[d] is None or d not in closes:
                return False
            return closes[d] > vals[d] if kind == "pressure" else closes[d] < vals[d]

        today_out = outside(date)
        prev_d = path[path.index(date) - 1] if path.index(date) > 0 else None
        prev_out = outside(prev_d) if prev_d else False

        v0, c0 = vals[date], closes[date]
        dist = (c0 / v0 - 1) * 100
        if today_out:
            n_out = 0
            for d in reversed(path[: path.index(date) + 1]):
                if outside(d):
                    n_out += 1
                else:
                    break
            if not prev_out:
                out.append(f"　⚠️ {name} 疑似失效：收盘 {c0:.1f} 首次{'站上' if kind == 'pressure' else '跌破'}"
                           f"线值 {v0:.1f}（{dist:+.1f}%）——可能假失效，重画与否你定"
                           + (f"｜{ln['note']}" if ln["note"] else ""))
            else:
                out.append(f"　⚠️ {name} 已连续 {n_out} 天收在线{'上' if kind == 'pressure' else '下'}方"
                           f"（今 {c0:.1f} vs 线 {v0:.1f}，{dist:+.1f}%）")
        else:
            # 盘中触线未破：压力看 high、支撑看 low（距线 0.5% 内且收盘收回内侧）
            touch = ""
            if lr and lr[0] is not None and kind == "support" and abs(lr[0] / v0 - 1) <= MACD_TOUCH_TOL:
                touch = f"，今日盘中触线未破（最低 {lr[0]:.1f}）"
            elif highs.get(date) is not None and kind == "pressure" and abs(highs[date] / v0 - 1) <= MACD_TOUCH_TOL:
                touch = f"，今日盘中触线未破（最高 {highs[date]:.1f}）"
            out.append(f"　{name}：线值 {v0:.1f}｜收盘 {c0:.1f}（{dist:+.1f}%，内侧）{touch}")
            if prev_out:
                out.append(f"　✅ {name} 收回线{'下' if kind == 'pressure' else '上'}方"
                           f"（今 {c0:.1f} vs 线 {v0:.1f}），疑似假失效解除，线继续有效")
    return out


# ---------------- ⑨ 结构参考：MTR 结构锚 + MACD ----------------

def structure_lines(con: sqlite3.Connection, date: str) -> list[str]:
    rows = con.execute(
        "SELECT trade_date, high, low, close FROM index_daily WHERE ts_code='000001.SH' "
        "AND trade_date <= ? ORDER BY trade_date DESC LIMIT 260", (date,)).fetchall()
    rows = rows[::-1]
    if len(rows) < 90:
        return ["⑨结构参考：（指数日线样本不足）"]
    h = [r[1] for r in rows]
    l = [r[2] for r in rows]
    c = [r[3] for r in rows]
    t = len(c) - 1
    if rows[t][0] != date:
        return ["⑨结构参考：（指数行情缺失）"]

    anchor = mtr_anchor({"high": h, "low": l, "close": c})
    hi = _swing_highs(h, t)
    lo = _swing_lows(l, t)
    dstr = lambda i: f"{rows[i][0][4:6]}/{rows[i][0][6:]}"  # noqa: E731

    segs = [f"结构锚：{anchor}"]
    if len(hi) >= 2:
        a, b = hi[-2], hi[-1]
        tag = "LH（高点降低）" if h[b] < h[a] else "HH（高点抬高）"
        segs.append(f"高点 {h[a]:.1f}({dstr(a)})→{h[b]:.1f}({dstr(b)})={tag}")
    if len(lo) >= 2:
        a, b = lo[-2], lo[-1]
        tag = "LL（低点降低）" if l[b] < l[a] else "HL（低点抬高）"
        segs.append(f"低点 {l[a]:.1f}({dstr(a)})→{l[b]:.1f}({dstr(b)})={tag}")
    # 方向反转提示：最近一段与上一段方向相反（PA 里结构动能衰减的最早信号）
    if len(lo) >= 3:
        dirs = ["LL" if l[lo[i]] < l[lo[i - 1]] else "HL" for i in range(len(lo) - 1, max(1, len(lo) - 3) - 1, -1)]
        dirs = dirs[::-1]
        if len(dirs) >= 2 and dirs[-1] != dirs[-2]:
            zh = "低点首次抬升" if dirs[-1] == "HL" else "低点首次降低"
            segs.append(f"⚠️ {zh}（{dirs[-2]}→{dirs[-1]}，收敛/反转候选）")
    if len(hi) >= 3:
        dirs = ["LH" if h[hi[i]] < h[hi[i - 1]] else "HH" for i in range(len(hi) - 1, max(1, len(hi) - 3) - 1, -1)]
        dirs = dirs[::-1]
        if len(dirs) >= 2 and dirs[-1] != dirs[-2]:
            zh = "高点首次降低" if dirs[-1] == "LH" else "高点首次抬高"
            segs.append(f"⚠️ {zh}（{dirs[-2]}→{dirs[-1]}，收敛/反转候选）")

    out = ["⑨结构参考（MTR+MACD，只报状态）：", f"　" + "；".join(segs[:1] + segs[1:2] + segs[2:])]
    macd = macd_state(c)
    out.append(f"　MACD(12,26,9)：{macd}")
    return out


def macd_state(closes: list[float]) -> str:
    """国内口径：DIF=EMA12-EMA26，DEA=EMA9(DIF)，柱=2*(DIF-DEA)；报零轴/叉/柱变化。"""
    if len(closes) < 60:
        return "样本不足"

    def ema(vals: list[float], n: int) -> list[float]:
        a = 2.0 / (n + 1)
        out = [vals[0]]
        for v in vals[1:]:
            out.append(a * v + (1 - a) * out[-1])
        return out

    e12, e26 = ema(closes, 12), ema(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema(dif, 9)
    bar = [2 * (d - e) for d, e in zip(dif, dea)]
    diff = [d - e for d, e in zip(dif, dea)]
    # 最近一次变号 = 最近金叉/死叉
    cross_i = None
    for i in range(len(diff) - 1, 0, -1):
        if diff[i - 1] <= 0 < diff[i]:
            cross_i, cross_zh = i, "金叉"
            break
        if diff[i - 1] >= 0 > diff[i]:
            cross_i, cross_zh = i, "死叉"
            break
    t = len(closes) - 1
    zero = "零轴上" if dif[t] > 0 else "零轴下"
    if cross_i is None:
        cross_txt = "无交叉记录"
    else:
        n_days = t - cross_i
        bar_zh = ""
        if n_days >= 1:
            growing = abs(bar[t]) > abs(bar[t - 1])
            polarity = "红柱" if bar[t] > 0 else "绿柱"
            bar_zh = f"、{polarity}{'扩张' if growing else '收缩'}"
        cross_txt = f"{cross_zh}第 {n_days} 天{bar_zh}"
    return f"DIF {dif[t]:.1f} / DEA {dea[t]:.1f}（{zero}），{cross_txt}"


def lines_section(con: sqlite3.Connection, date: str) -> list[str]:
    """market_review 挂载入口：⑧ 手绘线台账 + ⑨ 结构参考。"""
    try:
        part1 = monitor_lines(con, date)
    except Exception as e:  # 台账出错不影响复盘其余部分
        part1 = [f"⑧画线台账：（出错 {type(e).__name__}: {e}）"]
    try:
        part2 = structure_lines(con, date)
    except Exception as e:
        part2 = [f"⑨结构参考：（出错 {type(e).__name__}: {e}）"]
    return part1 + part2
