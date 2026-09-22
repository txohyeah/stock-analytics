"""龙虎榜信号推送版：超买/超卖 × 买卖席位成分（游资/机构/北向）交叉分析。

定位：每日晚间飞书推送（cron agent 任务驱动，`lhb-brief` 一次输出整份推送文本）。
区别于旧 u4 lhb-signal：在净额占比信号之外，用 top_inst 席位明细回答"谁在买/谁在卖"：
  超买股 -> 按买方五席成分判定主导（机构抢筹 / 游资打板 / 共振拉升 / 北向买入 / 分散）
  超卖股 -> 按卖方五席成分判定主导（机构撤退 / 游资撤退 / 共振出货 / 北向砸盘 / 分散）

复用：
  lhb.py        信号计算（阈值 40%、2 亿放宽 30%、1 亿下限、剔除北交所/转债/新股）
  lhb_dominant  席位分类与主导判定（_judge 同阈值，复用于卖方成分）
  注：lhb_dominant 命令保持"买方成分判定全部上榜股"的原逻辑不变。

多榜单口径与严重异常榜排除同 lhb_dominant（每股取主榜：超买按买入额最大当日榜，
超卖按卖出额最大当日榜；多日榜标注 [3日榜]）。

用法：
  ./venv/bin/python -m app.analytics.cli lhb-brief [--trade-date YYYYMMDD]
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .errors import DataInsufficientError
from .lhb import (
    BIG_NET,
    BIG_THRESHOLD,
    MIN_ABS_NET,
    SYNC_HINT,
    THRESHOLD,
    _connect,
    _has_table,
    is_excluded,
    load_industry_map,
    load_sub_industry,
    resolve_trade_date,
)
from .lhb_dominant import (
    CLS_HOT,
    CLS_INST,
    CLS_NORTH,
    DOM_SYNC_HINT,
    _aggregate_by_reason,
    _judge,
    classify_seat,
)

# 主导 -> 分组标题（买方视角 = 超买；卖方视角 = 超卖）
BUY_BUCKET: dict[str, str] = {
    "机构主导": "🔴 机构抢筹（超买·机构席位买入占比达标）",
    "游资主导": "🔴 游资打板（超买·营业部资金主导）",
    "机构游资共振": "🔴 共振拉升（超买·机构+游资同买）",
    "北向主导": "🔴 北向买入（超买·沪深股通席位主导）",
    "分散": "🔴 其他超买（成分分散）",
    "未知": "🔴 成分未知（无席位明细）",
}
SELL_BUCKET: dict[str, str] = {
    "机构主导": "🟢 机构撤退（超卖·机构席位卖出占比达标）",
    "游资主导": "🟢 游资撤退（超卖·营业部资金出逃）",
    "机构游资共振": "🟢 共振出货（超卖·机构+游资同卖）",
    "北向主导": "🟢 北向砸盘（超卖·沪深股通席位主导）",
    "分散": "🟢 其他超卖（成分分散）",
    "未知": "🟢 成分未知（无席位明细）",
}
BUCKET_ORDER = ["机构主导", "游资主导", "机构游资共振", "北向主导", "分散", "未知"]

CLS_SHORT = {CLS_INST: "构", CLS_HOT: "游", CLS_NORTH: "北"}


def _select_primary(by_reason: dict[tuple[str, str], dict[str, Any]], side: str) -> dict[str, dict[str, Any]]:
    """每股选主榜：当日榜中关键金额最大，否则多日榜中最大（与 lhb_dominant 同逻辑）。

    side="buy" 按买入五席合计选（谁在拉），side="sell" 按卖出五席合计选（谁在砸）。
    """
    per_code: dict[str, dict[str, Any]] = {}
    for g in by_reason.values():
        cur = per_code.get(g["ts_code"])
        is_daily, cur_daily = (not g["multi_day"]), (cur is not None and not cur["multi_day"])
        amount = g["total_buy"] if side == "buy" else sum(g["sell_by_cls"].values())
        cur_amount = 0.0
        if cur is not None:
            cur_amount = cur["total_buy"] if side == "buy" else sum(cur["sell_by_cls"].values())
        if cur is None or (is_daily and not cur_daily) or (is_daily == cur_daily and amount > cur_amount):
            per_code[g["ts_code"]] = g
    return per_code


def _mix(stat: dict[str, float], total: float) -> str:
    """席位成分摘要：构/游/北 三类占比整数%。"""
    if total <= 0:
        return "成分未知"
    return f"{CLS_SHORT[CLS_INST]}{stat[CLS_INST] / total * 100:.0f} {CLS_SHORT[CLS_HOT]}{stat[CLS_HOT] / total * 100:.0f} {CLS_SHORT[CLS_NORTH]}{stat[CLS_NORTH] / total * 100:.0f}"


def _signals(conn, day: str, records: list, industry_map: dict[str, str]) -> tuple[list[dict], list[dict], dict[str, dict], int]:
    """超买/超卖信号计算（与 lhb.py 同规则）+ 同股多榜汇总。"""
    agg: dict[str, dict[str, Any]] = {}
    excluded_count = 0
    for r in records:
        ex, _why = is_excluded(r)
        if ex:
            excluded_count += 1
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

    overbuy: list[dict[str, Any]] = []
    oversell: list[dict[str, Any]] = []
    for a in agg.values():
        if abs(a["net_amount"]) < MIN_ABS_NET:
            continue
        big = abs(a["net_amount"]) >= BIG_NET
        th = BIG_THRESHOLD if big else THRESHOLD
        buy_ratio = a["net_amount"] / a["l_buy"] if a["l_buy"] else 0
        sell_ratio = -a["net_amount"] / a["l_sell"] if a["l_sell"] else 0
        if a["net_amount"] > 0 and buy_ratio >= th:
            a["ratio"] = buy_ratio
            a["big_relax"] = big and buy_ratio < THRESHOLD
            overbuy.append(a)
        elif a["net_amount"] < 0 and sell_ratio >= th:
            a["ratio"] = sell_ratio
            a["big_relax"] = big and sell_ratio < THRESHOLD
            oversell.append(a)
    overbuy.sort(key=lambda x: -x["ratio"])
    oversell.sort(key=lambda x: -x["ratio"])
    return overbuy, oversell, agg, excluded_count


def run_lhb_brief(trade_date: str | None = None, db_path: str | Path | None = None, top_per_bucket: int = 8) -> dict[str, object]:
    """LHB brief CLI 逻辑入口（由 app.cli 分发）。"""
    conn = _connect(db_path)
    try:
        if not _has_table(conn, "top_list"):
            raise DataInsufficientError("top_list 未同步", hint=SYNC_HINT)
        if not _has_table(conn, "top_inst"):
            raise DataInsufficientError("top_inst 未同步（龙虎榜席位明细）", hint=DOM_SYNC_HINT)
        day = resolve_trade_date(conn, trade_date)
        records = conn.execute("SELECT * FROM top_list WHERE trade_date=?", (day,)).fetchall()
        if not records:
            raise DataInsufficientError(f"{day} 无龙虎榜数据", hint=SYNC_HINT)
        inst_rows = conn.execute("SELECT * FROM top_inst WHERE trade_date=?", (day,)).fetchall()

        industry_map = load_industry_map(conn)
        overbuy, oversell, agg, excluded_count = _signals(conn, day, records, industry_map)
        sub_map = load_sub_industry(conn, [a["ts_code"] for a in overbuy + oversell])

        # 席位成分：严重异常榜（投资者分类累计成交口径）排除；主榜按买卖两侧分别选
        abnormal_codes: set[str] = set()
        usable = [r for r in inst_rows if "严重异常" not in (r["reason"] or "")]
        abnormal_codes |= {str(r["ts_code"]) for r in inst_rows if "严重异常" in (r["reason"] or "")}
        by_reason = _aggregate_by_reason(usable)
        buy_primary = _select_primary(by_reason, "buy")
        sell_primary = _select_primary(by_reason, "sell")

        # 附上主导判定：超买用买方成分，超卖用卖方成分
        for side, signals, primary in (("buy", overbuy, buy_primary), ("sell", oversell, sell_primary)):
            for a in signals:
                g = primary.get(a["ts_code"])
                a["sub"] = sub_map.get(a["ts_code"], "其他")
                if g is None:
                    a["dominant"] = None
                    a["mix"] = "成分未知"
                    a["seat_cls"] = {}
                    a["warn"] = "（无席位数据）"
                    continue
                if side == "buy":
                    cls_stat = g["buy_by_cls"]
                    total = g["total_buy"]
                else:
                    cls_stat = g["sell_by_cls"]
                    total = sum(g["sell_by_cls"].values())
                if total <= 0:
                    a["dominant"] = None
                    a["mix"] = "成分未知"
                    a["seat_cls"] = {}
                    a["warn"] = "[3日榜]" if g["multi_day"] else ""
                    continue
                ratios = {k: cls_stat[k] / total for k in cls_stat}
                a["dominant"] = _judge(ratios[CLS_INST], ratios[CLS_HOT], ratios[CLS_NORTH])
                a["mix"] = _mix(cls_stat, total)
                a["seat_cls"] = {k: round(v, 0) for k, v in cls_stat.items()}
                warn = "[3日榜]" if g["multi_day"] else ""
                # 分歧标注：主导类席位方向与该股信号方向相反 = 对倒/分歧
                dom_cls = {"机构主导": CLS_INST, "游资主导": CLS_HOT, "北向主导": CLS_NORTH}.get(a["dominant"])
                if dom_cls is not None:
                    dom_net = g["nets"][dom_cls]
                    if side == "buy" and dom_net < 0:
                        warn += f"⚠{CLS_SHORT[dom_cls]}净卖"
                    elif side == "sell" and dom_net > 0:
                        warn += f"⚠{CLS_SHORT[dom_cls]}净买"
                a["warn"] = warn.strip()

        # 分组（dominant 为 None 归入"未知"桶）
        def bucketize(signals: list[dict], buckets: dict[str, str]) -> dict[str, list[dict]]:
            out: dict[str, list[dict]] = {}
            for a in signals:
                out.setdefault(buckets[a.get("dominant") or "未知"], []).append(a)
            return out

        buy_groups = bucketize(overbuy, BUY_BUCKET)
        sell_groups = bucketize(oversell, SELL_BUCKET)

        # ---- 文本输出 ----
        lines: list[str] = []
        lines.append(f"🐉 龙虎榜信号 · {day}")
        lines.append(
            f"上榜 {len(agg)} 只（剔除 {excluded_count}）｜超买 {len(overbuy)} · 超卖 {len(oversell)}"
            + (f"｜已排除严重异常榜 {len(abnormal_codes)} 只" if abnormal_codes else "")
        )
        lines.append(f"规则：净买≥买额{int(THRESHOLD * 100)}%（净额≥{BIG_NET / 1e8:g}亿放宽{int(BIG_THRESHOLD * 100)}%），净额下限{MIN_ABS_NET / 1e8:g}亿")
        lines.append("")

        def emit_group(title: str, gs: list[dict]) -> None:
            lines.append(f"== {title}：{len(gs)} 只 ==")
            if not gs:
                lines.append("  （无）\n")
                return
            lines.append(f"{'代码':<10}{'名称':<9}{'涨跌%':>7}{'净额(万)':>10}  {'成分':<12}备注 上榜原因")
            for a in gs[:top_per_bucket]:
                warn = f"{a['warn']} " if a.get("warn") else ""
                lines.append(
                    f"{a['ts_code']:<10}{a['name']:<9}{a['pct_change']:>7.2f}{a['net_amount'] / 1e4:>10,.0f}"
                    f"  {a['mix']:<12}{warn}{a['reasons'][0][:16]}"
                )
            if len(gs) > top_per_bucket:
                lines.append(f"  …另有 {len(gs) - top_per_bucket} 只略（按净额占比排序）")
            lines.append("")

        for key in BUCKET_ORDER:
            title = BUY_BUCKET.get(key)
            if title and buy_groups.get(title):
                emit_group(title, buy_groups[title])
            title = SELL_BUCKET.get(key)
            if title and sell_groups.get(title):
                emit_group(title, sell_groups[title])

        # 板块分布（东财行业，超买/超卖各 top6）
        for label, signals in (("超买", overbuy), ("超卖", oversell)):
            if not signals:
                continue
            cnt = Counter(a["industry"] for a in signals)
            parts = [f"{ind}({n}只)：{'、'.join(a['name'] for a in signals if a['industry'] == ind)}" for ind, n in cnt.most_common(6)]
            lines.append(f"-- {label}板块分布 --")
            lines.extend(f"  {p}" for p in parts)
            lines.append("")

        lines.append("口径：成分=买五/卖五席位分类占比（构=机构专用，游=营业部游资，北=沪深股通）；超买按买方成分、超卖按卖方成分判主导；游资=非机构非北向营业部席位")

        text = "\n".join(lines)
        print(text)

        def sig_json(a: dict) -> dict[str, Any]:
            return {
                "ts_code": a["ts_code"],
                "name": a["name"],
                "industry": a["industry"],
                "sub": a.get("sub"),
                "pct_change": a["pct_change"],
                "net_amount": a["net_amount"],
                "ratio": round(a["ratio"], 4),
                "big_relax": a["big_relax"],
                "dominant": a.get("dominant"),
                "mix": a.get("mix"),
                "seat_cls": a.get("seat_cls", {}),
                "warn": a.get("warn", ""),
                "reasons": a["reasons"][:2],
            }

        return {
            "ok": True,
            "trade_date": day,
            "stocks": len(agg),
            "overbuy": [sig_json(a) for a in overbuy],
            "oversell": [sig_json(a) for a in oversell],
            "buy_groups": {t: [a["ts_code"] for a in gs] for t, gs in buy_groups.items()},
            "sell_groups": {t: [a["ts_code"] for a in gs] for t, gs in sell_groups.items()},
        }
    finally:
        conn.close()
