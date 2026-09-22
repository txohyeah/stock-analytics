"""龙虎榜资金性质分析：游资主导 vs 机构主导（top_inst 席位口径）。

数据源：sqlite top_inst（营业部/机构逐条席位明细，由 app.cli sync top_inst 同步）。
辅助：stock_basic（名称/行业）、top_list（当日涨跌幅）。

席位分类（按 exalter 名称匹配）：
  机构 = 含"机构专用"（同一榜单可多行，是不同机构）
  北向 = 含"沪股通" / "深股通"
  游资/营业部 = 其余（口径：非机构非北向的营业部席位）

占比口径（每股每榜单）：
  total_buy = 该榜 side=0（买入榜）席位 buy 合计
  xx_buy_ratio = 该类席位买入合计 / total_buy
  xx_net = 该类席位买入合计(side=0 行 buy) - 卖出合计(side=1 行 sell)

主导判定（阈值环境变量可调，LHB_DOM_*）：
  北向占比 >= 50%                       -> 北向主导
  机构占比 >= 40%                       -> 机构主导
  游资占比 >= 60% 且机构 < 20%          -> 游资主导
  游资占比 >= 40% 且机构 >= 20%         -> 机构游资共振
  其余                                  -> 分散
分歧备注：主导类当日净卖出（如机构净卖）标注 ⚠。

多榜单口径：同一股票一天可能同时上当日榜（如"日涨幅偏离值达到7%"）与多日榜
（如"连续三个交易日偏离20%"），多日榜金额是多日累计，不与当日榜混算。
每股取买入额最大的当日榜做主判定；只有多日榜时用多日榜并标注[3日榜]。

特殊口径排除：深交所"严重异常波动"榜（涨幅偏离100%/跌幅偏离70%）披露的是
投资者分类（自然人/中小投资者/机构投资者等）的区间累计总成交，非营业部买卖
五席，不参与主导判定，仅在报告中列出。

用法：
  ./venv/bin/python -m app.cli lhb-dominant --trade-date 20260918
  ./venv/bin/python -m app.cli lhb-dominant    # 默认最近有 top_inst 数据的交易日
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .errors import DataInsufficientError
from .lhb import _connect, _has_table, load_industry_map

# 判定阈值（环境变量可调）
NORTH_RATIO = float(os.environ.get("LHB_DOM_NORTH", "0.50"))      # 北向主导线
INST_RATIO = float(os.environ.get("LHB_DOM_INST", "0.40"))        # 机构主导线
HOT_RATIO = float(os.environ.get("LHB_DOM_HOT", "0.60"))          # 游资主导线
HOT_INST_LOW = float(os.environ.get("LHB_DOM_HOT_INST_LOW", "0.20"))  # 游资主导要求机构占比上限
CO_HOT = float(os.environ.get("LHB_DOM_CO_HOT", "0.40"))          # 共振：游资占比线
CO_INST = float(os.environ.get("LHB_DOM_CO_INST", "0.20"))        # 共振：机构占比线

DOM_SYNC_HINT = (
    "先同步龙虎榜席位明细：./venv/bin/python -m app.cli sync top_inst --start YYYYMMDD --end YYYYMMDD"
    "（2026-09-22 起已纳入每日 market 组自动同步）"
)

CLS_INST = "机构"
CLS_NORTH = "北向"
CLS_HOT = "游资/营业部"


def classify_seat(exalter: str) -> str:
    if "机构专用" in exalter:
        return CLS_INST
    if "沪股通" in exalter or "深股通" in exalter:
        return CLS_NORTH
    return CLS_HOT


def _short_seat(name: str) -> str:
    for pat in ("股份有限公司", "有限责任公司", "证券营业部", "证券有限公司", "营业部"):
        name = name.replace(pat, "")
    return name


def _resolve_date(conn, trade_date: str | None) -> str:
    """指定日期或最近有 top_inst 数据的交易日。"""
    if not _has_table(conn, "trade_cal"):
        raise DataInsufficientError("trade_cal 未同步，无法定位交易日", hint=DOM_SYNC_HINT)
    if trade_date:
        d = trade_date
    else:
        row = conn.execute("SELECT cal_date FROM trade_cal WHERE is_open='1' ORDER BY cal_date DESC LIMIT 1").fetchone()
        d = str(row["cal_date"]) if row else ""
    row = conn.execute("SELECT 1 FROM top_inst WHERE trade_date=? LIMIT 1", (d,)).fetchone()
    if row:
        return d
    dates = sorted(
        str(r["cal_date"])
        for r in conn.execute(
            "SELECT cal_date FROM trade_cal WHERE is_open='1' AND cal_date<=? ORDER BY cal_date DESC LIMIT 30",
            (d,),
        ).fetchall()
    )
    for day in reversed(dates):
        row = conn.execute("SELECT 1 FROM top_inst WHERE trade_date=? LIMIT 1", (day,)).fetchone()
        if row:
            return day
    raise DataInsufficientError(f"{d} 及往前 30 个交易日无 top_inst 席位数据", hint=DOM_SYNC_HINT)


def _judge(inst_r: float, hot_r: float, north_r: float) -> str:
    if north_r >= NORTH_RATIO:
        return "北向主导"
    if inst_r >= INST_RATIO:
        return "机构主导"
    if hot_r >= HOT_RATIO and inst_r < HOT_INST_LOW:
        return "游资主导"
    if hot_r >= CO_HOT and inst_r >= CO_INST:
        return "机构游资共振"
    return "分散"


def _empty_cls_stat() -> dict[str, float]:
    return {CLS_INST: 0.0, CLS_NORTH: 0.0, CLS_HOT: 0.0}


def _aggregate_by_reason(rows: list) -> dict[str, dict[str, Any]]:
    """按 (ts_code, reason) 聚合席位金额。返回 code_reason -> 统计 dict。"""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        code = str(r["ts_code"])
        reason = (r["reason"] or "").strip()
        g = groups.setdefault(
            (code, reason),
            {
                "ts_code": code,
                "reason": reason,
                "multi_day": "连续" in reason,
                "total_buy": 0.0,
                "buy_by_cls": _empty_cls_stat(),
                "sell_by_cls": _empty_cls_stat(),
                "seats_buy": [],   # (exalter, buy, sell, net_buy)
                "seats_sell": [],  # (exalter, sell, buy, -net_buy)
            },
        )
        cls = classify_seat(r["exalter"] or "")
        side = str(r["side"])
        if side == "0":
            buy = r["buy"] or 0.0
            g["total_buy"] += buy
            g["buy_by_cls"][cls] += buy
            g["seats_buy"].append((r["exalter"], buy, r["sell"] or 0.0, r["net_buy"] or 0.0))
        else:
            sell = r["sell"] or 0.0
            g["sell_by_cls"][cls] += sell
            g["seats_sell"].append((r["exalter"], sell, r["buy"] or 0.0, -(r["net_buy"] or 0.0)))
    result = {}
    for (code, reason), g in groups.items():
        tb = g["total_buy"]
        ratios = {k: (v / tb if tb else 0.0) for k, v in g["buy_by_cls"].items()}
        nets = {k: g["buy_by_cls"][k] - g["sell_by_cls"][k] for k in g["buy_by_cls"]}
        result[f"{code}|{reason}"] = {
            **g,
            "ratios": ratios,
            "nets": nets,
            "dominant": _judge(ratios[CLS_INST], ratios[CLS_HOT], ratios[CLS_NORTH]) if tb > 0 else "分散",
        }
    return result


def run_lhb_dominant(trade_date: str | None = None, db_path: str | Path | None = None) -> dict[str, object]:
    """LHB dominant CLI 逻辑入口（由 app.cli 分发）。"""
    conn = _connect(db_path)
    try:
        if not _has_table(conn, "top_inst"):
            raise DataInsufficientError("top_inst 未同步（龙虎榜席位明细）", hint=DOM_SYNC_HINT)
        day = _resolve_date(conn, trade_date)
        rows = conn.execute("SELECT * FROM top_inst WHERE trade_date=?", (day,)).fetchall()
        if not rows:
            raise DataInsufficientError(f"{day} 无 top_inst 席位数据", hint=DOM_SYNC_HINT)

        # "严重异常波动"榜是投资者分类（自然人/中小投资者/机构投资者等）的
        # 区间累计总成交口径，非营业部买卖五席，金额可为几十亿级总成交，
        # 参与游资/机构判定会严重失真 —— 排除并注明（2026-09-22 闽东电力案例）
        abnormal = [r for r in rows if "严重异常" in (r["reason"] or "")]
        rows = [r for r in rows if "严重异常" not in (r["reason"] or "")]
        abnormal_codes = sorted({str(r["ts_code"]) for r in abnormal})

        industry_map = load_industry_map(conn)
        name_map: dict[str, str] = {}
        pct_map: dict[str, float] = {}
        if _has_table(conn, "stock_basic"):
            for r in conn.execute("SELECT ts_code, name FROM stock_basic").fetchall():
                name_map[str(r["ts_code"])] = str(r["name"] or "")
        if _has_table(conn, "top_list"):
            for r in conn.execute("SELECT ts_code, pct_change FROM top_list WHERE trade_date=?", (day,)).fetchall():
                code = str(r["ts_code"])
                if r["pct_change"] is not None:
                    pct_map.setdefault(code, float(r["pct_change"]))

        by_reason = _aggregate_by_reason(rows)

        # 每股选主榜：当日榜（multi_day=False）中买入额最大；否则多日榜中最大
        per_code: dict[str, dict[str, Any]] = {}
        for g in by_reason.values():
            cur = per_code.get(g["ts_code"])
            is_daily, cur_daily = (not g["multi_day"]), (cur is not None and not cur["multi_day"])
            if cur is None or (is_daily and not cur_daily) or (is_daily == cur_daily and g["total_buy"] > cur["total_buy"]):
                per_code[g["ts_code"]] = g
        stocks = sorted(per_code.values(), key=lambda g: -g["total_buy"])

        buckets: dict[str, list[dict[str, Any]]] = {}
        for g in stocks:
            buckets.setdefault(g["dominant"], []).append(g)

        order = ["机构主导", "游资主导", "机构游资共振", "北向主导", "分散"]

        def fmt_wan(v: float) -> str:
            return f"{v / 1e4:,.0f}"

        lines: list[str] = []
        lines.append(f"{day} 龙虎榜资金性质（席位口径 top_inst，共 {len(stocks)} 只上榜）")
        summary = {k: len(buckets.get(k, [])) for k in order if buckets.get(k)}
        lines.append(
            "主导分布：" + "、".join(f"{k} {v}只" for k, v in summary.items())
            + "（口径：游资=非机构非北向营业部席位；占比分母=该榜买入五席合计）"
        )
        if abnormal_codes:
            lines.append(f"已排除严重异常波动榜 {len(abnormal_codes)} 只（投资者分类累计成交口径，非营业部席位）：{'、'.join(abnormal_codes[:8])}")
        lines.append("")

        def append_bucket(title: str, gs: list[dict[str, Any]]) -> None:
            if not gs:
                return
            lines.append(f"== {title}（{len(gs)} 只）==")
            lines.append(f"{'代码':<10}{'名称':<10}{'涨跌%':>7}{'买入(万)':>10}{'机构%':>6}{'游资%':>6}{'北向%':>6}{'机构净(万)':>11}{'游资净(万)':>11}  备注")
            for g in gs:
                notes = []
                if g["multi_day"]:
                    notes.append("[3日榜口径]")
                if g["dominant"] in ("机构主导", "机构游资共振") and g["nets"][CLS_INST] < 0:
                    notes.append("⚠机构净卖")
                if g["dominant"] in ("游资主导", "机构游资共振") and g["nets"][CLS_HOT] < 0:
                    notes.append("⚠游资净卖")
                r = g["ratios"]
                lines.append(
                    f"{g['ts_code']:<10}{name_map.get(g['ts_code'], '-'):<10}"
                    f"{pct_map.get(g['ts_code'], 0.0):>7.2f}{fmt_wan(g['total_buy']):>10}"
                    f"{r[CLS_INST] * 100:>6.0f}{r[CLS_HOT] * 100:>6.0f}{r[CLS_NORTH] * 100:>6.0f}"
                    f"{fmt_wan(g['nets'][CLS_INST]):>11}{fmt_wan(g['nets'][CLS_HOT]):>11}  "
                    + ("；".join(notes) if notes else "")
                )
            lines.append("")

        for k in order:
            append_bucket(k, buckets.get(k, []))

        # 席位明细：主导类（机构/游资/共振）展开买五卖五
        detail_stocks = [g for k in ("机构主导", "游资主导", "机构游资共振", "北向主导") for g in buckets.get(k, [])]
        if detail_stocks:
            lines.append("== 席位明细（主导类，买/卖各按金额前5）==")
            for g in detail_stocks:
                tag = "[3日榜口径] " if g["multi_day"] else ""
                lines.append(f"◆ {g['ts_code']} {name_map.get(g['ts_code'], '-')} {tag}{g['reason']}")
                for exalter, buy, sell, net in sorted(g["seats_buy"], key=lambda x: -x[1])[:5]:
                    cls = classify_seat(exalter)
                    lines.append(f"   买 {cls} {_short_seat(exalter or '-')} {fmt_wan(buy):>9}万 净买 {fmt_wan(net):>9}万")
                for exalter, sell, buy, net in sorted(g["seats_sell"], key=lambda x: -x[1])[:5]:
                    cls = classify_seat(exalter)
                    lines.append(f"   卖 {cls} {_short_seat(exalter or '-')} {fmt_wan(sell):>9}万 净卖 {fmt_wan(-net):>9}万")
                lines.append("")

        text = "\n".join(lines)
        print(text)

        def seat_json(seats: list[tuple], limit: int = 5) -> list[dict[str, Any]]:
            return [
                {
                    "exalter": exalter,
                    "class": classify_seat(exalter),
                    "buy": buy if buy else None,
                    "sell": sell if sell else None,
                    "net": net,
                }
                for exalter, buy, sell, net in sorted(seats, key=lambda x: -(x[1] + x[2]))[:limit]
            ]

        detail_json = []
        for g in stocks:
            r = g["ratios"]
            detail_json.append(
                {
                    "ts_code": g["ts_code"],
                    "name": name_map.get(g["ts_code"], ""),
                    "industry": industry_map.get(g["ts_code"], "未知"),
                    "pct_change": pct_map.get(g["ts_code"]),
                    "reason": g["reason"],
                    "multi_day": g["multi_day"],
                    "dominant": g["dominant"],
                    "total_buy": g["total_buy"],
                    "inst_buy_ratio": round(r[CLS_INST], 4),
                    "hot_buy_ratio": round(r[CLS_HOT], 4),
                    "north_buy_ratio": round(r[CLS_NORTH], 4),
                    "inst_net": g["nets"][CLS_INST],
                    "hot_net": g["nets"][CLS_HOT],
                    "north_net": g["nets"][CLS_NORTH],
                    "seats": seat_json(g["seats_buy"] + g["seats_sell"]),
                }
            )
        return {
            "ok": True,
            "trade_date": day,
            "stocks": len(stocks),
            "summary": summary,
            "abnormal_excluded": abnormal_codes,
            "detail": detail_json,
        }
    finally:
        conn.close()
