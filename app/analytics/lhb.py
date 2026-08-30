"""龙虎榜超买/超卖信号筛选（含板块分析）——从 stock-research lhb_signal.py 迁移改造。

数据源：sqlite（top_list / stock_basic / stock_company），由 app.sync 同步（tushare）。
指标定义：
  超买 = 净买入 >= 买入额(l_buy) 的 40%（LHB_THRESHOLD）
  超卖 = 净卖出 >= 卖出额(l_sell) 的 40%
  大额放宽：净额绝对值 >= 2 亿元（LHB_BIG_NET，默认 2e8）时，阈值降至 30%（LHB_BIG_THRESHOLD）
          —— 大票上榜买卖金额大，40% 占比很难达到，用绝对净额（2 亿+）弥补占比不足
过滤规则（可调）：
  - 剔除北交所（.BJ）
  - 剔除可转债（代码 11/12 开头）
  - 剔除新股（上榜原因为"无价格涨跌幅限制"= 上市初期）
  - 信号净额绝对值下限：|净买入/净卖出| < 1 亿元（LHB_MIN_NET，默认 1e8）不输出

用法：
  ./venv/bin/python -m app.cli lhb --trade-date 20260828   # 指定交易日 YYYYMMDD
  ./venv/bin/python -m app.cli lhb                          # 默认最近有数据的交易日
"""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .errors import DataInsufficientError, UserInputError
from .repository import DEFAULT_DB_PATH

THRESHOLD = float(os.environ.get("LHB_THRESHOLD", "0.40"))           # 净买/净卖占比阈值
BIG_NET = float(os.environ.get("LHB_BIG_NET", "2e8"))                # 大额放宽净额线（元），默认 2 亿
BIG_THRESHOLD = float(os.environ.get("LHB_BIG_THRESHOLD", "0.30"))   # 大额放宽阈值，默认 30%
EXCLUDE_BJ = True
EXCLUDE_BOND = True
EXCLUDE_NEW = True
MIN_ABS_NET = float(os.environ.get("LHB_MIN_NET", "1e8"))            # 信号净额绝对值下限（元），默认 1 亿

SYNC_HINT = (
    "先同步龙虎榜数据：./venv/bin/python -m app.cli sync top_list --start YYYYMMDD --end YYYYMMDD"
    "；行业资料：./venv/bin/python -m app.cli sync stock_company"
)

# 细分链归类：关键词 → 子方向（基于 stock_company.main_business 匹配，优先命中靠前的规则）
# 解决东财行业粒度太粗的问题（如"元器件"把 PCB/覆铜板/连接器/被动元件全塞一起）
SUB_INDUSTRY_RULES = [
    ("PCB链", ["印制电路板", "覆铜板", "线路板", "印刷电路板"]),
    ("光模块/光通信", ["光模块", "光通信", "光器件", "光收发", "光传输", "光电子"]),
    ("半导体", ["半导体", "芯片", "晶圆", "集成电路", "封装测试", "分立器件", "存储"]),
    ("连接器", ["连接器", "高速线", "铜缆", "线束"]),
    ("被动元件", ["电容", "电感", "电阻", "被动元件"]),
    ("算力设备", ["服务器", "交换机", "算力", "数据中心", "液冷"]),
    ("消费电子", ["消费电子", "智能手机", "可穿戴", "显示"]),
    ("汽车电子", ["汽车电子", "车载", "智能驾驶"]),
    ("军工电子", ["军工", "雷达", "航天", "卫星"]),
    ("稀土永磁", ["稀土", "永磁", "磁材"]),
    ("生物医药", ["医药", "疫苗", "生物", "制药"]),
    ("电力/电网", ["电力", "发电", "电网", "变压器"]),
    ("软件/信创", ["软件", "网络安全", "信息安全", "信创", "操作系统"]),
]
# 手工覆盖表：tushare 主营描述滞后的转型公司（code -> 细分标签），优先于关键词匹配
SUB_OVERRIDES = {
    "001267.SZ": "光模块/光通信",  # 汇绿生态：2024 收购钧恒科技切入 AI 光模块，2026 拟全资控股
}


def _connect(db_path: str | Path | None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        from .errors import DatabaseConnectionError

        raise DatabaseConnectionError(str(exc)) from exc


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def latest_open_date(conn: sqlite3.Connection, before: str | None = None) -> str:
    """本地 trade_cal 最近交易日（<= before，默认今天之前）。"""
    if not _has_table(conn, "trade_cal"):
        raise DataInsufficientError("trade_cal 未同步，无法定位交易日", hint=SYNC_HINT)
    cond = "WHERE is_open='1'"
    params: tuple = ()
    if before:
        cond += " AND cal_date<=?"
        params = (before,)
    row = conn.execute(f"SELECT cal_date FROM trade_cal {cond} ORDER BY cal_date DESC LIMIT 1", params).fetchone()
    if not row:
        raise DataInsufficientError(f"trade_cal 无最近交易日（before={before}）", hint=SYNC_HINT)
    return str(row["cal_date"])


def resolve_trade_date(conn: sqlite3.Connection, trade_date: str | None) -> str:
    """指定日期或最近 open day；若该日无 data 则往前找有 top_list 数据的交易日。"""
    if not _has_table(conn, "top_list"):
        raise DataInsufficientError("top_list 未同步（龙虎榜数据）", hint=SYNC_HINT)
    if trade_date:
        d = trade_date
    else:
        d = latest_open_date(conn)
    # 当前日期有数据直接返回
    row = conn.execute("SELECT 1 FROM top_list WHERE trade_date=? LIMIT 1", (d,)).fetchone()
    if row:
        return d
    # 往前找最近有数据的交易日
    dates = sorted(
        str(r["cal_date"])
        for r in conn.execute(
            "SELECT cal_date FROM trade_cal WHERE is_open='1' AND cal_date<=? ORDER BY cal_date DESC LIMIT 30",
            (d,),
        ).fetchall()
    )
    for day in reversed(dates):
        row = conn.execute("SELECT 1 FROM top_list WHERE trade_date=? LIMIT 1", (day,)).fetchone()
        if row:
            return day
    raise DataInsufficientError(f"{d} 及往前 30 个交易日无龙虎榜数据", hint=SYNC_HINT)


def is_excluded(r: sqlite3.Row) -> tuple[bool, str | None]:
    code = r["ts_code"]
    if EXCLUDE_BJ and code.endswith(".BJ"):
        return True, "北交所"
    if EXCLUDE_BOND and (code.startswith("11") or code.startswith("12")):
        return True, "可转债"
    if EXCLUDE_NEW and "无价格涨跌幅限制" in (r["reason"] or ""):
        return True, "新股"
    return False, None


def load_industry_map(conn: sqlite3.Connection) -> dict[str, str]:
    if not _has_table(conn, "stock_basic"):
        return {}
    return {
        str(r["ts_code"]): (r["industry"] or "未知")
        for r in conn.execute("SELECT ts_code, industry FROM stock_basic").fetchall()
    }


def load_sub_industry(conn: sqlite3.Connection, codes: list[str]) -> dict[str, str]:
    """按主营关键词给个股打细分链标签；只处理命中信号的少量股票。"""
    result: dict[str, str] = {c: SUB_OVERRIDES[c] for c in codes if c in SUB_OVERRIDES}
    rest = [c for c in codes if c not in SUB_OVERRIDES]
    if rest and _has_table(conn, "stock_company"):
        rows = conn.execute(
            "SELECT ts_code, main_business FROM stock_company WHERE ts_code IN (%s)"
            % ",".join("?" * len(rest)),
            rest,
        ).fetchall()
        for r in rows:
            biz = r["main_business"] or ""
            tag = "其他"
            for name, kws in SUB_INDUSTRY_RULES:
                if any(k in biz for k in kws):
                    tag = name
                    break
            result[str(r["ts_code"])] = tag
        result = {c: result.get(c, "其他") for c in rest + [c for c in codes if c in SUB_OVERRIDES]}
    return result


def run_lhb(trade_date: str | None = None, db_path: str | Path | None = None) -> dict[str, object]:
    """LHB CLI 逻辑入口（由 app.cli 分发）。"""
    conn = _connect(db_path)
    try:
        day = resolve_trade_date(conn, trade_date)
        records = conn.execute("SELECT * FROM top_list WHERE trade_date=?", (day,)).fetchall()
        if not records:
            raise DataInsufficientError(f"{day} 无龙虎榜数据", hint=SYNC_HINT)

        industry_map = load_industry_map(conn)

        # 过滤 + 同股多次上榜汇总
        agg: dict[str, dict[str, Any]] = {}
        excluded: dict[str, list[str]] = {}
        for r in records:
            ex, why = is_excluded(r)
            if ex:
                excluded.setdefault(why, []).append(r["ts_code"])
                continue
            code = r["ts_code"]
            a = agg.setdefault(
                code,
                {
                    "ts_code": code,
                    "name": r["name"],
                    "pct_change": r["pct_change"],
                    "industry": industry_map.get(code, "未知"),
                    "amount": 0.0,
                    "l_buy": 0.0,
                    "l_sell": 0.0,
                    "net_amount": 0.0,
                    "reasons": [],
                },
            )
            a["amount"] += r["amount"] or 0
            a["l_buy"] += r["l_buy"] or 0
            a["l_sell"] += r["l_sell"] or 0
            a["net_amount"] += r["net_amount"] or 0
            if r["reason"] and r["reason"] not in a["reasons"]:
                a["reasons"].append(r["reason"])

        stocks = list(agg.values())
        total_excluded = sum(len(v) for v in excluded.values())

        overbuy: list[tuple[dict, float, bool]] = []
        oversell: list[tuple[dict, float, bool]] = []
        for a in stocks:
            buy_ratio = a["net_amount"] / a["l_buy"] if a["l_buy"] else 0
            sell_ratio = -a["net_amount"] / a["l_sell"] if a["l_sell"] else 0
            if abs(a["net_amount"]) < MIN_ABS_NET:
                continue
            big = abs(a["net_amount"]) >= BIG_NET
            th = BIG_THRESHOLD if big else THRESHOLD
            if a["net_amount"] > 0 and buy_ratio >= th:
                overbuy.append((a, buy_ratio, big and buy_ratio < THRESHOLD))
            elif a["net_amount"] < 0 and sell_ratio >= th:
                oversell.append((a, sell_ratio, big and sell_ratio < THRESHOLD))

        overbuy.sort(key=lambda x: x[1], reverse=True)
        oversell.sort(key=lambda x: x[1], reverse=True)

        # 细分链归类（只查命中信号的股票）
        sub_map = load_sub_industry(conn, [a["ts_code"] for a, _, _ in overbuy + oversell])
        for a, _, _ in overbuy + oversell:
            a["sub"] = sub_map.get(a["ts_code"], "其他")

        lines: list[str] = []
        lines.append(f"{day} 龙虎榜 {len(records)} 条记录 / {len(stocks) + total_excluded} 只股票，"
                     f"剔除 {total_excluded} 只（{'、'.join(f'{k}{len(v)}只' for k, v in excluded.items())}）")
        lines.append("")

        def append_list(title: str, pairs: list[tuple[dict, float, bool]], sign: int) -> None:
            lines.append(f"=== {title}：{len(pairs)} 只 ===")
            if not pairs:
                lines.append("  （无）\n")
                return
            lines.append(f"{'代码':<10}{'名称':<8}{'板块':<10}{'涨跌幅':>8}{'净额(万)':>11}{'占比':>7}  上榜原因")
            for a, r, big in pairs:
                net = sign * a["net_amount"] / 1e4
                pct = (sign * a["net_amount"]) / (a["l_buy"] if sign > 0 else a["l_sell"]) * 100
                mark = "*" if big else " "
                lines.append(f"{a['ts_code']:<10}{a['name']:<8}{a['industry']:<10}{a['pct_change']:>8.2f}{net:>11.0f}{pct:>6.1f}%{mark} {a['reasons'][0][:21]}")
            if any(big for _, _, big in pairs):
                lines.append(f"  （* = 仅靠放宽档命中：净额≥{BIG_NET / 1e8:g}亿且占比未达{int(THRESHOLD * 100)}%）")
            # 板块聚合（东财行业）
            lines.append("\n  -- 板块分布 --")
            cnt = Counter(a["industry"] for a, _, _ in pairs)
            for ind, n in cnt.most_common():
                names = "、".join(a["name"] for a, _, _ in pairs if a["industry"] == ind)
                lines.append(f"  {ind}（{n}只）：{names}")
            # 细分链分布
            sub_cnt = Counter(a.get("sub", "其他") for a, _, _ in pairs)
            lines.append("\n  -- 细分链分布 --")
            for s, n in sub_cnt.most_common():
                names = "、".join(a["name"] for a, _, _ in pairs if a.get("sub", "其他") == s)
                lines.append(f"  {s}（{n}只）：{names}")
            lines.append("")

        def rule_desc(side: str) -> str:
            key = "买入额" if side == "净买入" else "卖出额"
            base = f"{side} ≥ {key}{int(THRESHOLD * 100)}%"
            if BIG_THRESHOLD < THRESHOLD:
                base += f"；净额≥{BIG_NET / 1e8:g}亿时≥{int(BIG_THRESHOLD * 100)}%"
            return base

        append_list(f"超买（{rule_desc('净买入')}）", overbuy, 1)
        append_list(f"超卖（{rule_desc('净卖出')}）", oversell, -1)

        text = "\n".join(lines)
        print(text)
        return {
            "ok": True,
            "trade_date": day,
            "records": len(records),
            "stocks": len(stocks),
            "excluded": total_excluded,
            "overbuy": [
                {"ts_code": a["ts_code"], "name": a["name"], "industry": a["industry"], "sub": a.get("sub"), "pct_change": a["pct_change"], "net_amount": a["net_amount"], "ratio": round(r, 4), "big_relax": big}
                for a, r, big in overbuy
            ],
            "oversell": [
                {"ts_code": a["ts_code"], "name": a["name"], "industry": a["industry"], "sub": a.get("sub"), "pct_change": a["pct_change"], "net_amount": a["net_amount"], "ratio": round(r, 4), "big_relax": big}
                for a, r, big in oversell
            ],
        }
    finally:
        conn.close()