"""财报排雷批量扫描器（公司暴雷检查）——基于《财报排雷手册》五雷区逻辑。

从 stock-research baolei_check.py 迁移并改造：
- 数据源：MySQL(stock_fina_indicator) → **sqlite 三报表 + fina_indicator + fina_audit（tushare 原生表名）**
- 2026-08-30 升级为五雷区：
  - 雷区零 审计意见（fina_audit，非标前置闸门）：标准无保留绿；带强调事项/解释性说明/持续经营黄；保留/无法表示/否定红
  - 雷区一：扣非/归母 >=70% 绿、50%~70% 黄、<50% 红；归母>0 扣非<0 直接红；归母涨扣非跌升一档
  - 雷区二：cashflow.n_cashflow_act 连续为负红 / 单年为负黄 / 现金流-归母比<0.5 黄
  - 雷区三：商誉/归母净资产 >30% 红、15%~30% 黄、<=15% 绿（原 50% 红线收紧至 30%，对齐手册）
  - 雷区四 业绩拐点：最新报告期营收或归母同比转负 = 黄（增长引擎熄火预警）
- 数据库未同步（无表/空表）时抛 DataInsufficientError 并给出按需同步命令

评级映射（文章综合判定）：
  任一红 -> 高（排雷未通过）；有黄无红 -> 中（需深挖）；全绿 -> 低（通过排雷关）。
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .errors import DataInsufficientError, DatabaseConnectionError
from .repository import DEFAULT_DB_PATH

RED, YELLOW, GREEN = "红", "黄", "绿"
SKIP = "sk"
SYNC_HINT = (
    "先同步 finance 组：./venv/bin/python -m app.cli sync finance --ts-codes <codes>"
    "（全市场：--mode history，脚本 scripts/backfill_finance.sh）"
    "；审计意见单独同步：./venv/bin/python -m app.cli sync fina_audit --ts-codes <codes>"
)
REQUIRED_TABLES = ("income", "balancesheet", "cashflow", "fina_indicator")
OPTIONAL_TABLES = ("fina_audit",)


@dataclass
class StockResult:
    ts_code: str
    name: str
    industry: str
    annual: list  # list of dict rows sorted desc by end_date
    r0: str = GREEN          # 雷区零 审计意见（非标前置闸门）
    r0_detail: str = ""
    r1: str = GREEN          # 雷区一 利润结构（扣非/归母 + 扣非同比）
    r1_detail: str = ""
    r2: str = GREEN          # 雷区二 现金流质量（经营现金流净额连续为负 + 现金/利润比率）
    r2_detail: str = ""
    r3: str = GREEN          # 雷区三 商誉（商誉/归母净资产）
    r3_detail: str = ""
    r4: str = GREEN          # 雷区四 业绩拐点（最新报告期营收/归母同比转负）
    r4_detail: str = ""
    rating: str = GREEN      # 综合 暴雷可能性
    reasons: list = field(default_factory=list)


def _check_tables(conn: sqlite3.Connection) -> None:
    exist = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('income','balancesheet','cashflow','fina_indicator')"
        ).fetchall()
    }
    missing = [t for t in REQUIRED_TABLES if t not in exist]
    if missing:
        raise DataInsufficientError(
            f"排雷所需表未同步: {', '.join(missing)}", hint=SYNC_HINT
        )


def bulk_fetch(db_path: str | Path | None = None) -> tuple[dict[str, list], dict[str, dict], dict[str, dict], dict[str, dict]]:
    """一次性拉全市场年报（end_date 为 12-31）的所需字段 + 股票名 + 审计意见 + 最新报告期趋势。

    四张主表按 (ts_code, end_date) 合并，同一报告期的更正/追溯公告取 ann_date 最新一条。
    fina_audit 按 (ts_code, end_date) 单独返回（仅年报，缺表则空）。
    trend 返回每只股票最新报告期 vs 去年同期的营收/归母（用 income，不限年报，供雷区四）。
    sqlite 中 end_date 为 YYYYMMDD 字符串；年报判定 substr(end_date,5,4)='1231'。
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        _check_tables(conn)
        has_fina_audit = bool(
            conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fina_audit'").fetchone()
        )

        q_income = (
            "SELECT ts_code, end_date, ann_date, n_income, n_income_attr_p FROM income "
            "WHERE substr(end_date,5,4)='1231'"
        )
        q_cashflow = (
            "SELECT ts_code, end_date, ann_date, n_cashflow_act FROM cashflow "
            "WHERE substr(end_date,5,4)='1231'"
        )
        q_balancesheet = (
            "SELECT ts_code, end_date, ann_date, goodwill, total_hldr_eqy_exc_min_int "
            "FROM balancesheet WHERE substr(end_date,5,4)='1231'"
        )
        q_fina = (
            "SELECT ts_code, end_date, ann_date, profit_dedt, dt_netprofit_yoy, netprofit_yoy FROM fina_indicator "
            "WHERE substr(end_date,5,4)='1231'"
        )
        rows_income = conn.execute(q_income).fetchall()
        rows_cf = conn.execute(q_cashflow).fetchall()
        rows_bs = conn.execute(q_balancesheet).fetchall()
        rows_fina = conn.execute(q_fina).fetchall()
        basic = conn.execute("SELECT ts_code, name, industry FROM stock_basic").fetchall()

        # 审计意见（可选表）
        audit_map: dict[str, dict[str, str]] = {}
        if has_fina_audit:
            for r in conn.execute(
                "SELECT ts_code, end_date, audit_result FROM fina_audit WHERE substr(end_date,5,4)='1231'"
            ).fetchall():
                audit_map.setdefault(r["ts_code"], {})[str(r["end_date"])[:8]] = (r["audit_result"] or "").replace(" ", "")

        # 最新报告期 vs 去年同期（income 全部分期，取每 code 最新 end_date + 去年同口径）
        trend_map: dict[str, dict[str, Any]] = {}
        all_income = conn.execute(
            "SELECT ts_code, end_date, ann_date, revenue, n_income_attr_p FROM income"
        ).fetchall()
        by_period: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for r in all_income:
            by_period[(r["ts_code"], str(r["end_date"]))].append(r)
        for (code, end), rows in by_period.items():
            rows.sort(key=lambda x: str(x["ann_date"] or ""))
            latest = rows[-1]
            year = int(end[:4])
            prev_ed = f"{year - 1}{end[4:]}"
            prev = by_period.get((code, prev_ed))
            prev_row = sorted(prev, key=lambda x: str(x["ann_date"] or ""))[-1] if prev else None
            cur = trend_map.setdefault(
                code,
                {"latest_ed": end, "rev": None, "np": None, "prev_ed": prev_ed, "prev_rev": None, "prev_np": None},
            )
            if end >= str(cur["latest_ed"] or ""):
                cur["latest_ed"] = end
                cur["rev"] = latest["revenue"]
                cur["np"] = latest["n_income_attr_p"]
                cur["prev_ed"] = prev_ed
                cur["prev_rev"] = prev_row["revenue"] if prev_row else None
                cur["prev_np"] = prev_row["n_income_attr_p"] if prev_row else None
    except sqlite3.Error as exc:
        raise DatabaseConnectionError(str(exc)) from exc
    finally:
        conn.close()

    if not rows_income:
        raise DataInsufficientError("income 表为空（无年报数据）", hint=SYNC_HINT)

    # 合并表：以 income 行打底，按 (ts_code, end_date) 聚合
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows_income:
        key = (r["ts_code"], r["end_date"])
        merged.setdefault(key, {"ts_code": r["ts_code"], "end_date": r["end_date"], "ann_date": r["ann_date"]})
        d = merged[key]
        if r["ann_date"] and (not d.get("ann_date") or r["ann_date"] >= d["ann_date"]):
            d["ann_date"] = r["ann_date"]
        d["n_income"] = r["n_income"]
        d["n_income_attr_p"] = r["n_income_attr_p"]
    for src, tags in (
        (rows_cf, ("n_cashflow_act",)),
        (rows_bs, ("goodwill", "total_hldr_eqy_exc_min_int")),
        (rows_fina, ("profit_dedt", "dt_netprofit_yoy", "netprofit_yoy")),
    ):
        for r in src:
            key = (r["ts_code"], r["end_date"])
            d = merged.get(key)
            if d is None:
                d = merged.setdefault(key, {"ts_code": r["ts_code"], "end_date": r["end_date"], "ann_date": r["ann_date"]})
            if r["ann_date"] and (not d.get("ann_date") or r["ann_date"] >= d["ann_date"]):
                d["ann_date"] = r["ann_date"]
            for tag in tags:
                d[tag] = r[tag]

    basic_map = {r["ts_code"]: dict(r) for r in basic}
    by_code: dict[str, list] = defaultdict(list)
    for (code, _end), d in merged.items():
        by_code[code].append(d)
    for code in by_code:
        by_code[code].sort(key=lambda x: str(x["end_date"]), reverse=True)
    return by_code, basic_map, audit_map, trend_map


def evaluate(code: str, basic: dict, annual: list, audit_map: dict[str, dict[str, str]] | None = None, trend: dict[str, Any] | None = None) -> StockResult:
    audit_map = audit_map or {}
    trend = trend or {}
    name = basic.get("name") or code
    industry = basic.get("industry") or ""
    # 同一年可能有多条年报（更正/追溯），按年去重，避免连续年数被重复计数
    seen_year = set()
    ann_dedup = []
    for r in annual:
        y = _end_year(r["end_date"])
        if y in seen_year:
            continue
        seen_year.add(y)
        ann_dedup.append(r)
    annual = ann_dedup
    res = StockResult(ts_code=code, name=name, industry=industry, annual=annual)

    # ================= 雷区零：审计意见（非标前置闸门） =================
    latest = annual[0] if annual else None
    if latest is not None and audit_map.get(code):
        audit = audit_map[code].get(str(latest["end_date"])[:8])
        if audit:
            if "标准无保留" in audit:
                res.r0 = GREEN
                res.r0_detail = f"{latest['end_date'][:4]} 审计意见：{audit}"
            elif "无保留" in audit:
                # 带强调事项段/解释性说明/持续经营重大不确定性 的 无保留意见 → 黄
                res.r0 = YELLOW
                res.r0_detail = f"{latest['end_date'][:4]} 审计意见：{audit}（带强调事项段/非标事项，需关注）"
            elif any(k in audit for k in ("保留意见", "无法表示意见", "否定意见")):
                res.r0 = RED
                res.r0_detail = f"{latest['end_date'][:4]} 审计意见：{audit}（非标！利润真实性存疑）"
            else:
                res.r0 = YELLOW
                res.r0_detail = f"{latest['end_date'][:4]} 审计意见：{audit}（非标准无保留，需关注）"
        else:
            res.r0 = SKIP
            res.r0_detail = f"{latest['end_date'][:4]} 审计意见缺失（fina_audit 未同步或该期无数据）"
    else:
        res.r0 = SKIP
        res.r0_detail = "无年报数据 / fina_audit 表未同步（雷区零跳过）"

    # ================= 雷区一：利润结构（扣非/归母 比率 + 扣非同比趋势） =================
    ratios = []
    for r in annual:
        ni = r.get("n_income_attr_p")  # 归母净利润
        pd = r.get("profit_dedt")      # 扣非净利润
        if ni is not None and pd is not None and ni > 0 and pd >= 0:
            ratios.append((_end_year(r["end_date"]), pd / ni))
    r1_flags = []
    r1_parts = []
    loss_years = [_end_year(r["end_date"]) for r in annual if (r.get("n_income_attr_p") or 0) < 0]
    cons_loss = 0
    for r in annual:
        if (r.get("n_income_attr_p") or 0) < 0:
            cons_loss += 1
        else:
            break
    if cons_loss >= 2:
        r1_flags.append(RED)
        r1_parts.append(f"归母净利润连续 {cons_loss} 年为负（{loss_years[:4]}）")
    elif cons_loss == 1:
        r1_flags.append(YELLOW)
        r1_parts.append(f"最新年报归母净利润为负（{loss_years[0]}）")
    if latest is not None:
        ni_ly = latest.get("n_income_attr_p")
        pd_ly = latest.get("profit_dedt")
        if ni_ly is not None and pd_ly is not None and ni_ly > 0 and pd_ly < 0:
            r1_flags.append(RED)
            r1_parts.append(f"{latest['end_date'][:4]} 归母为正但扣非为负：利润全靠非经常性损益")
    if ratios:
        latest_ratio = ratios[0][1]
        if latest_ratio < 0.5:
            low_ratio_years = [y for y, rt in ratios if rt < 0.5]
            if len(low_ratio_years) >= 2:
                r1_flags.append(RED)
                r1_parts.append(f"扣非/归母 <0.5 连续 {len(low_ratio_years)} 年（{low_ratio_years[:4]}）")
            else:
                r1_flags.append(YELLOW)
                r1_parts.append(f"扣非/归母={latest_ratio:.2f}(<50%，利润依赖非经常损益)")
        elif latest_ratio < 0.7:
            r1_flags.append(YELLOW)
            r1_parts.append(f"扣非/归母={latest_ratio:.2f}(50%~70%，利润结构偏弱)")
        else:
            r1_parts.append(f"扣非/归母={latest_ratio:.2f}(>=70%，利润结构健康)")
    # 扣非同比趋势（辅助硬信号）
    dedt_yoy = [r["dt_netprofit_yoy"] for r in annual if r.get("dt_netprofit_yoy") is not None]
    cons_dedt_neg = 0
    for y in dedt_yoy:  # annual desc => dedt_yoy 同序
        if y < 0:
            cons_dedt_neg += 1
        else:
            break
    if cons_dedt_neg >= 2:
        r1_flags.append(RED)
        r1_parts.append(f"扣非净利润同比连续 {cons_dedt_neg} 年为负（主业持续恶化）")
    elif cons_dedt_neg == 1 or (dedt_yoy and dedt_yoy[0] < -10):
        r1_flags.append(YELLOW)
        r1_parts.append("扣非同比下滑/单年为负（利润含金量偏弱）")
    # 归母同比涨、扣非同比跌 → 升一档（利润靠非经常性损益撑，对齐原版 netprofit_yoy/dt_netprofit_yoy 背离）
    ny = latest.get("netprofit_yoy") if latest else None
    dy = latest.get("dt_netprofit_yoy") if latest else None
    if ny is not None and dy is not None and ny > 0 and dy < 0:
        if RED not in r1_flags:
            if r1_flags:  # 已有黄 → 升红
                r1_flags.append(RED)
                r1_parts.append(f"归母同比 {ny / 100:+.1%} 但扣非同比 {dy / 100:+.1%}：增长成色差（升档）")
            else:  # 全绿 → 黄
                r1_flags.append(YELLOW)
                r1_parts.append(f"归母同比 {ny / 100:+.1%} 但扣非同比 {dy / 100:+.1%}：增长成色差")
    if r1_flags:
        res.r1 = RED if RED in r1_flags else YELLOW
        res.r1_detail = "；".join(r1_parts)
    else:
        res.r1_detail = "扣非/归母比率健康且扣非同比为正"

    # ================= 雷区二：现金流质量 =================
    neg_years = [_end_year(r["end_date"]) for r in annual if r.get("n_cashflow_act") is not None and r["n_cashflow_act"] < 0]
    cons_neg = 0
    for r in annual:  # annual 已按 end_date desc
        if r.get("n_cashflow_act") is not None and r["n_cashflow_act"] < 0:
            cons_neg += 1
        else:
            break
    latest_ocf_ratio = None
    if latest is not None:
        ocf = latest.get("n_cashflow_act")
        ni = latest.get("n_income")
        if ocf is not None and ni is not None and ni > 0:
            latest_ocf_ratio = ocf / ni
    single_neg = (cons_neg == 1)
    if cons_neg >= 2:
        res.r2 = RED
        res.r2_detail = f"经营现金流连续 {cons_neg} 年为负（{neg_years[:4]}）"
    elif single_neg or (latest_ocf_ratio is not None and 0 <= latest_ocf_ratio < 0.5):
        res.r2 = YELLOW
        parts = []
        if single_neg:
            parts.append("单年经营现金流为负")
        if latest_ocf_ratio is not None and 0 <= latest_ocf_ratio < 0.5:
            parts.append(f"经营现金流/净利润={latest_ocf_ratio:.2f}(0~0.5)")
        res.r2_detail = "；".join(parts)
    else:
        res.r2_detail = "经营现金流持续为正且质量健康"

    # ================= 雷区三：商誉（对齐手册：>30% 红、15%~30% 黄、<=15% 绿） =================
    if latest is not None:
        gw = latest.get("goodwill")
        eq = latest.get("total_hldr_eqy_exc_min_int")  # 归母股东权益
        if gw is not None and eq is not None:
            if eq <= 0:
                res.r3 = RED
                res.r3_detail = f"归母股东权益 {eq:.0f}（资不抵债）"
            elif gw > 0:
                gw_ratio = gw / eq
                if gw_ratio > 0.3:
                    res.r3 = RED
                    res.r3_detail = f"商誉/归母净资产={gw_ratio:.1%}(>30%)"
                elif gw_ratio >= 0.15:
                    res.r3 = YELLOW
                    res.r3_detail = f"商誉/归母净资产={gw_ratio:.1%}(15%~30%)"
                else:
                    res.r3_detail = f"商誉/归母净资产={gw_ratio:.1%}（<=15%，健康）"
            else:
                res.r3_detail = "无商誉"

    # ================= 雷区四：业绩拐点（最新报告期营收/归母同比转负 = 黄） =================
    # trend 参数已是该股票的最新报告期字典（bulk_fetch 的 trend_map[code]）
    t = trend if trend else None
    if t and t.get("latest_ed"):
        cur_rev, prev_rev = t.get("rev"), t.get("prev_rev")
        cur_np, prev_np = t.get("np"), t.get("prev_np")
        if all(v is not None and v != 0 for v in (cur_rev, prev_rev, cur_np, prev_np)):
            r_yoy = (cur_rev - prev_rev) / abs(prev_rev)
            n_yoy = (cur_np - prev_np) / abs(prev_np)
            latest_ed = t["latest_ed"]
            detail = f"{latest_ed[:4]}-{latest_ed[4:6]} 营收同比 {r_yoy * 100:+.1f}%，归母同比 {n_yoy * 100:+.1f}%"
            if r_yoy < 0 or n_yoy < 0:
                res.r4 = YELLOW
                res.r4_detail = detail + "（最新报告期营收或归母同比转负：业绩拐点预警）"
            else:
                res.r4_detail = detail + "（最新报告期营收、归母同比均为正）"
        else:
            res.r4 = SKIP
            res.r4_detail = f"{t['latest_ed'][:4]}-{t['latest_ed'][4:6]} 同比数据缺失或上期基数为 0，无法判断"
    else:
        res.r4 = SKIP
        res.r4_detail = "无最新报告期数据（income 未同步），业绩拐点跳过"

    # ================= 综合判定（五档） =================
    flags = [res.r0, res.r1, res.r2, res.r3, res.r4]
    levels = [f for f in flags if f in (RED, YELLOW, GREEN)]
    if RED in levels:
        res.rating = "高"
    elif YELLOW in levels:
        res.rating = "中"
    else:
        res.rating = "低"
    for tag, level, detail in (
        ("零", res.r0, res.r0_detail),
        ("一", res.r1, res.r1_detail),
        ("二", res.r2, res.r2_detail),
        ("三", res.r3, res.r3_detail),
        ("四", res.r4, res.r4_detail),
    ):
        if level in (RED, YELLOW):
            res.reasons.append(f"雷区{tag}·{detail}")
    if not res.reasons:
        res.reasons.append("五雷区均未触发预警")
    return res


def _end_year(end_date: Any) -> int:
    return int(str(end_date)[:4])


def run_all(db_path: str | Path | None = None) -> list[StockResult]:
    by_code, basic_map, audit_map, trend_map = bulk_fetch(db_path)
    results = []
    for code, annual in by_code.items():
        basic = basic_map.get(code) or {}
        results.append(evaluate(code, basic, annual, audit_map, trend_map.get(code)))
    return results


def summarize(results) -> tuple[Counter, Counter, Counter, Counter, Counter, Counter]:
    c = Counter(r.rating for r in results)
    r0c = Counter(r.r0 for r in results)
    r1c = Counter(r.r1 for r in results)
    r2c = Counter(r.r2 for r in results)
    r3c = Counter(r.r3 for r in results)
    r4c = Counter(r.r4 for r in results)
    return c, r0c, r1c, r2c, r3c, r4c


def generate_markdown(results, path: str) -> None:
    c, r0c, r1c, r2c, r3c, r4c = summarize(results)
    red_r0 = [r for r in results if r.r0 == RED]
    red_r1 = [r for r in results if r.r1 == RED]
    red_r2 = [r for r in results if r.r2 == RED]
    red_r3 = [r for r in results if r.r3 == RED]
    yel = [r for r in results if r.rating == "中"]
    yel_r4 = [r for r in results if r.r4 == YELLOW]
    low = [r for r in results if r.rating == "低"]
    for lst in (red_r0, red_r1, red_r3, yel_r4, yel, low):
        lst.sort(key=lambda x: x.name)
    red_r2.sort(
        key=lambda x: (len([y for y in x.annual if y.get("n_cashflow_act") is not None and y["n_cashflow_act"] < 0]), x.name),
        reverse=True,
    )
    lines = []
    lines.append(f"# 全市场财报排雷扫描（基于《财报排雷手册》五雷区）\n")
    lines.append(f"> 生成日期：{date.today()} ｜ 数据源：income/balancesheet/cashflow/fina_indicator/fina_audit（tushare 同步 → sqlite）｜ 覆盖有年报数据股票 {len(results)} 只\n")
    lines.append("\n## 一、方法与数据口径\n")
    lines.append("- 文章五雷区：**雷区零 审计意见（非标前置闸门）｜雷区一 利润结构（扣非/归母）｜雷区二 现金流质量｜雷区三 商誉｜雷区四 业绩拐点**。")
    lines.append("- **雷区零**：fina_audit.audit_result，标准无保留=绿；带强调事项/解释性说明/持续经营=黄；保留/无法表示/否定=红（利润真实性存疑，前置闸门）。")
    lines.append("- **雷区一**：扣非/归母（fina_indicator.profit_dedt / income.n_income_attr_p）>=70% 绿、50%~70% 黄、<50% 红；归母为正扣非为负直接红；归母涨扣非跌升一档。")
    lines.append("- **雷区二**：cashflow.n_cashflow_act 连续为负（硬信号）+ 经营现金流/净利润<0.5（软信号）。")
    lines.append("- **雷区三**：balancesheet.goodwill / 归母净资产，>30% 红、15%~30% 黄、<=15% 绿（红线 30%）。")
    lines.append("- **雷区四**：income 最新报告期 vs 去年同期，营收或归母同比转负 = 黄（业绩拐点预警）。\n")
    lines.append("## 二、评级定义（文章综合判定）\n")
    lines.append("- 任一红 → **高（排雷未通过）**；有黄无红 → **中（需深挖）**；全绿 → **低（通过排雷关）**。\n")
    lines.append("## 三、概览统计\n")
    lines.append(f"- 综合暴雷可能性：**高 {c.get('高',0)} ｜ 中 {c.get('中',0)} ｜ 低 {c.get('低',0)}**")
    lines.append(f"- 雷区零 审计意见：红 {r0c.get('红',0)} ｜ 黄 {r0c.get('黄',0)} ｜ 绿 {r0c.get('绿',0)} ｜ 跳过 {r0c.get('sk',0)}")
    lines.append(f"- 雷区一 利润结构：红 {r1c.get('红',0)} ｜ 黄 {r1c.get('黄',0)} ｜ 绿 {r1c.get('绿',0)}")
    lines.append(f"- 雷区二 现金流：红 {r2c.get('红',0)} ｜ 黄 {r2c.get('黄',0)} ｜ 绿 {r2c.get('绿',0)}")
    lines.append(f"- 雷区三 商誉：红 {r3c.get('红',0)} ｜ 黄 {r3c.get('黄',0)} ｜ 绿 {r3c.get('绿',0)}")
    lines.append(f"- 雷区四 业绩拐点：黄 {r4c.get('黄',0)} ｜ 绿 {r4c.get('绿',0)} ｜ 跳过 {r4c.get('sk',0)}\n")
    lines.append("## 四、雷区零红（非标审计意见）— 共 %d 只\n" % len(red_r0))
    lines.append("| 代码 | 名称 | 行业 | 说明 |")
    lines.append("|---|---|---|---|")
    for r in red_r0:
        lines.append(f"| {r.ts_code} | {r.name} | {r.industry} | {r.r0_detail} |")
    lines.append("")
    lines.append("## 五、雷区一红（利润结构恶化）— 共 %d 只\n" % len(red_r1))
    lines.append("| 代码 | 名称 | 行业 | 说明 |")
    lines.append("|---|---|---|---|")
    for r in red_r1:
        lines.append(f"| {r.ts_code} | {r.name} | {r.industry} | {r.r1_detail} |")
    lines.append("")
    lines.append("## 六、雷区二红（现金流连续为负）— 共 %d 只\n" % len(red_r2))
    lines.append("> 最硬信号，与文章案例 *ST仕净（连续6年为负）一致。\n")
    lines.append("| 代码 | 名称 | 行业 | 连续为负年数 | 说明 |")
    lines.append("|---|---|---|---|---|")
    for r in red_r2:
        cons = len([y for y in r.annual if y.get("n_cashflow_act") is not None and y["n_cashflow_act"] < 0])
        lines.append(f"| {r.ts_code} | {r.name} | {r.industry} | {cons} | {r.r2_detail} |")
    lines.append("")
    lines.append("## 七、雷区三红（商誉/归母净资产>30%）— 共 %d 只\n" % len(red_r3))
    lines.append("| 代码 | 名称 | 行业 | 说明 |")
    lines.append("|---|---|---|---|")
    for r in red_r3:
        lines.append(f"| {r.ts_code} | {r.name} | {r.industry} | {r.r3_detail} |")
    lines.append("")
    lines.append("## 八、雷区四黄（业绩拐点预警）— 共 %d 只（样例前 50）\n" % len(yel_r4))
    lines.append("| 代码 | 名称 | 行业 | 说明 |")
    lines.append("|---|---|---|---|")
    for r in yel_r4[:50]:
        lines.append(f"| {r.ts_code} | {r.name} | {r.industry} | {r.r4_detail} |")
    lines.append("")
    lines.append("## 九、中风险（黄）— 共 %d 只（样例前 50）\n" % len(yel))
    lines.append("| 代码 | 名称 | 行业 | 触发 |")
    lines.append("|---|---|---|---|")
    for r in yel[:50]:
        lines.append(f"| {r.ts_code} | {r.name} | {r.industry} | {'; '.join(r.reasons)} |")
    lines.append("")
    lines.append("## 十、低风险（绿）— 共 %d 只\n" % len(low))
    lines.append("> 五雷区均通过。\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(
        f"report written: {path} ({len(red_r0)} r0-red, {len(red_r1)} r1-red, {len(red_r2)} r2-red, "
        f"{len(red_r3)} r3-red, {len(yel_r4)} r4-yellow, {len(yel)} yellow, {len(low)} green)"
    )


def run_baolei(*, all_mode: bool = False, codes: str = "", self_test: bool = False, report: str = "", db_path: str | None = None, repository=None) -> dict[str, object]:
    """baolei CLI 逻辑入口（由 app.cli 分发）。"""
    if self_test:
        test_codes = ["000001.SZ", "000002.SZ", "000008.SZ"]
        by_code, basic_map, audit_map, trend_map = bulk_fetch(db_path)
        payload = {"ok": True, "self_test": True, "results": []}
        for code in test_codes:
            if code in by_code:
                r = evaluate(code, basic_map.get(code, {}), by_code[code], audit_map, trend_map.get(code))
                print(f"{code} {r.name}: 评级={r.rating} 零={r.r0}({r.r0_detail}) 一={r.r1}({r.r1_detail}) 二={r.r2}({r.r2_detail}) 三={r.r3}({r.r3_detail}) 四={r.r4}({r.r4_detail})")
                payload["results"].append({"ts_code": code, "rating": r.rating, "r0": r.r0, "r1": r.r1, "r2": r.r2, "r3": r.r3, "r4": r.r4})
            else:
                print(f"{code}: 无年报数据")
                payload["results"].append({"ts_code": code, "rating": None})
        return payload

    if codes:
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
        by_code, basic_map, audit_map, trend_map = bulk_fetch(db_path)
        payload = {"ok": True, "mode": "codes", "results": []}
        for code in code_list:
            if code in by_code:
                r = evaluate(code, basic_map.get(code, {}), by_code[code], audit_map, trend_map.get(code))
                print(f"{code} {r.name}: 评级={r.rating} | 零={r.r0} {r.r0_detail} | 一={r.r1} {r.r1_detail} | 二={r.r2} {r.r2_detail} | 三={r.r3} {r.r3_detail} | 四={r.r4} {r.r4_detail}")
                payload["results"].append({"ts_code": code, "name": r.name, "rating": r.rating, "r0": r.r0, "r1": r.r1, "r2": r.r2, "r3": r.r3, "r4": r.r4, "reasons": r.reasons})
            else:
                print(f"{code}: 无年报数据")
                payload["results"].append({"ts_code": code, "rating": None})
        return payload

    if all_mode:
        results = run_all(db_path)
        if report:
            generate_markdown(results, report)
            return {"ok": True, "mode": "all", "report_path": str(Path(report).resolve()), "count": len(results)}
        c, r0c, r1c, r2c, r3c, r4c = summarize(results)
        print(f"扫描股票数: {len(results)}")
        print("综合暴雷可能性:", dict(c))
        print("雷区零(审计意见):", dict(r0c))
        print("雷区一(利润结构):", dict(r1c))
        print("雷区二(现金流):", dict(r2c))
        print("雷区三(商誉):", dict(r3c))
        print("雷区四(业绩拐点):", dict(r4c))
        red = [r for r in results if r.rating == "高"]
        yel = [r for r in results if r.rating == "中"]
        print(f"\n=== 高暴雷风险(红) 共 {len(red)} 只 ===")
        for r in sorted(red, key=lambda x: (x.r0 != RED, x.r2 != RED, x.r1 != RED, x.r3 != RED, x.name)):
            print(f"{r.ts_code} {r.name} [{r.industry}] {r.reasons}")
        print(f"\n=== 中风险(黄) 共 {len(yel)} 只 ===")
        for r in sorted(yel, key=lambda x: x.name):
            print(f"{r.ts_code} {r.name} [{r.industry}] {r.reasons}")
        return {"ok": True, "mode": "all", "count": len(results), "rating_counts": dict(c), "r0_counts": dict(r0c), "r1_counts": dict(r1c), "r2_counts": dict(r2c), "r3_counts": dict(r3c), "r4_counts": dict(r4c)}

    raise DataInsufficientError("baolei 需要 --self-test / --codes / --all 之一", hint="示例：--codes 600519.SH,000001.SZ")