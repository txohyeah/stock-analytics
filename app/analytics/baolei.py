"""财报排雷批量扫描器（公司暴雷检查）——基于《财报排雷手册》三雷区逻辑。

从 stock-research baolei_check.py 迁移并改造：
- 数据源：MySQL(stock_fina_indicator) → **sqlite 三报表 + fina_indicator（tushare 原生表名）**
- 摆脱 SQLAlchemy/MySQL，仅依赖标准库 sqlite3
- 2026-08-30 升级：income/balancesheet/cashflow 全市场同步后，
  雷区一（利润结构）用 income 归母净利润 + fina_indicator 扣非净利润算精确比率；
  雷区二（现金流）改由 cashflow.n_cashflow_act 直接判定连续为负 + 经营现金流/净利润比率；
  雷区三（商誉）用 balancesheet.goodwill / 归母净资产 评估。三雷区完整可用。
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
SYNC_HINT = (
    "先同步 finance 组：./venv/bin/python -m app.cli sync finance --ts-codes <codes>"
    "（全市场：--mode history，脚本 scripts/backfill_finance.sh）"
)
REQUIRED_TABLES = ("income", "balancesheet", "cashflow", "fina_indicator")


@dataclass
class StockResult:
    ts_code: str
    name: str
    industry: str
    annual: list  # list of dict rows sorted desc by end_date
    r1: str = GREEN          # 雷区一 利润结构（扣非/归母 + 扣非同比）
    r1_detail: str = ""
    r2: str = GREEN          # 雷区二 现金流质量（经营现金流净额连续为负 + 现金/利润比率）
    r2_detail: str = ""
    r3: str = GREEN          # 雷区三 商誉（商誉/归母净资产）
    r3_detail: str = ""
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


def bulk_fetch(db_path: str | Path | None = None) -> tuple[dict[str, list], dict[str, dict]]:
    """一次性拉全市场年报（end_date 为 12-31）的所需字段 + 股票名。

    四张表按 (ts_code, end_date) 合并，同一报告期的更正/追溯公告取 ann_date 最新一条。
    sqlite 中 end_date 为 YYYYMMDD 字符串；年报判定 substr(end_date,5,4)='1231'。
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        _check_tables(conn)

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
            "SELECT ts_code, end_date, ann_date, profit_dedt, dt_netprofit_yoy FROM fina_indicator "
            "WHERE substr(end_date,5,4)='1231'"
        )
        rows_income = conn.execute(q_income).fetchall()
        rows_cf = conn.execute(q_cashflow).fetchall()
        rows_bs = conn.execute(q_balancesheet).fetchall()
        rows_fina = conn.execute(q_fina).fetchall()
        basic = conn.execute("SELECT ts_code, name, industry FROM stock_basic").fetchall()
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
    for src, tags in ((rows_cf, ("n_cashflow_act",)), (rows_bs, ("goodwill", "total_hldr_eqy_exc_min_int")), (rows_fina, ("profit_dedt", "dt_netprofit_yoy"))):
        for r in src:
            key = (r["ts_code"], r["end_date"])
            d = merged.get(key)
            if d is None:
                # income 缺本期（个别股）时也保留
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
    return by_code, basic_map


def evaluate(code: str, basic: dict, annual: list) -> StockResult:
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

    # ---- 雷区一：利润结构（扣非/归母 比率 + 扣非同比趋势）----
    latest = annual[0] if annual else None
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
    if ratios:
        latest_ratio = ratios[0][1]
        if latest_ratio < 0.5:
            low_ratio_years = [y for y, rt in ratios if rt < 0.5]
            if len(low_ratio_years) >= 2:
                r1_flags.append(RED)
                r1_parts.append(f"扣非/归母 <0.5 连续 {len(low_ratio_years)} 年（{low_ratio_years[:4]}）")
            else:
                r1_flags.append(YELLOW)
                r1_parts.append(f"扣非/归母={latest_ratio:.2f}(<0.5，利润依赖非经常损益)")
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
    if r1_flags:
        res.r1 = RED if RED in r1_flags else YELLOW
        res.r1_detail = "；".join(r1_parts)
    else:
        res.r1_detail = "扣非/归母比率健康且扣非同比为正"

    # ---- 雷区二：现金流质量 ----
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

    # ---- 雷区三：商誉 ----
    if latest is not None:
        gw = latest.get("goodwill")
        eq = latest.get("total_hldr_eqy_exc_min_int")  # 归母股东权益
        if gw is not None and eq is not None:
            if eq <= 0:
                res.r3 = RED
                res.r3_detail = f"归母股东权益 {eq:.0f}（资不抵债）"
            elif gw > 0:
                gw_ratio = gw / eq
                if gw_ratio >= 0.5:
                    res.r3 = RED
                    res.r3_detail = f"商誉/归母净资产={gw_ratio:.0%}(>=50%)"
                elif gw_ratio >= 0.3:
                    res.r3 = YELLOW
                    res.r3_detail = f"商誉/归母净资产={gw_ratio:.0%}(30%~50%)"
                else:
                    res.r3_detail = f"商誉/归母净资产={gw_ratio:.1%}（健康）"
            else:
                res.r3_detail = "无商誉"

    # ---- 综合判定 ----
    flags = [res.r1, res.r2, res.r3]
    if RED in flags:
        res.rating = "高"
    elif YELLOW in flags:
        res.rating = "中"
    else:
        res.rating = "低"
    for tag, level, detail in (("一", res.r1, res.r1_detail), ("二", res.r2, res.r2_detail), ("三", res.r3, res.r3_detail)):
        if level in (RED, YELLOW):
            res.reasons.append(f"雷区{tag}·{detail}")
    if not res.reasons:
        res.reasons.append("三雷区均未触发预警")
    return res


def _end_year(end_date: Any) -> int:
    return int(str(end_date)[:4])


def run_all(db_path: str | Path | None = None) -> list[StockResult]:
    by_code, basic_map = bulk_fetch(db_path)
    results = []
    for code, annual in by_code.items():
        basic = basic_map.get(code) or {}
        results.append(evaluate(code, basic, annual))
    return results


def summarize(results) -> tuple[Counter, Counter, Counter, Counter]:
    c = Counter(r.rating for r in results)
    r1c = Counter(r.r1 for r in results)
    r2c = Counter(r.r2 for r in results)
    r3c = Counter(r.r3 for r in results)
    return c, r1c, r2c, r3c


def generate_markdown(results, path: str) -> None:
    c, r1c, r2c, r3c = summarize(results)
    red_r1 = [r for r in results if r.r1 == RED]
    red_r2 = [r for r in results if r.r2 == RED]
    red_r3 = [r for r in results if r.r3 == RED]
    yel = [r for r in results if r.rating == "中"]
    low = [r for r in results if r.rating == "低"]
    red_r1.sort(key=lambda x: x.name)
    red_r2.sort(key=lambda x: (len([y for y in x.annual if y.get("n_cashflow_act") is not None and y["n_cashflow_act"] < 0]), x.name), reverse=True)
    red_r3.sort(key=lambda x: x.name)
    yel.sort(key=lambda x: x.name)
    lines = []
    lines.append(f"# 全市场财报排雷扫描（基于《财报排雷手册》三雷区）\n")
    lines.append(f"> 生成日期：{date.today()} ｜ 数据源：income/balancesheet/cashflow/fina_indicator（tushare 同步 → sqlite）｜ 覆盖有年报数据股票 {len(results)} 只\n")
    lines.append("\n## 一、方法与数据口径\n")
    lines.append("- 文章三雷区：**雷区一 利润结构（扣非/归母）｜雷区二 现金流质量｜雷区三 商誉**（三雷区已完整复算）。")
    lines.append("- **雷区一**：扣非/归母 比率（fina_indicator.profit_dedt / income.n_income_attr_p）<0.5 或连续两年低；叠加归母净利润连续为负、扣非同比连续为负信号。")
    lines.append("- **雷区二**：cashflow.n_cashflow_act 连续为负（硬信号）+ 经营现金流/净利润<0.5（软信号）。")
    lines.append("- **雷区三**：balancesheet.goodwill / 归母净资产，>=50% 红、30%~50% 黄。\n")
    lines.append("## 二、评级定义（文章综合判定）\n")
    lines.append("- 任一红 → **高（排雷未通过）**；有黄无红 → **中（需深挖）**；全绿 → **低（通过排雷关）**。\n")
    lines.append("## 三、概览统计\n")
    lines.append(f"- 综合暴雷可能性：**高 {c.get('高',0)} ｜ 中 {c.get('中',0)} ｜ 低 {c.get('低',0)}**")
    lines.append(f"- 雷区一 利润结构：红 {r1c.get('红',0)} ｜ 黄 {r1c.get('黄',0)} ｜ 绿 {r1c.get('绿',0)}")
    lines.append(f"- 雷区二 现金流：红 {r2c.get('红',0)} ｜ 黄 {r2c.get('黄',0)} ｜ 绿 {r2c.get('绿',0)}")
    lines.append(f"- 雷区三 商誉：红 {r3c.get('红',0)} ｜ 黄 {r3c.get('黄',0)} ｜ 绿 {r3c.get('绿',0)}\n")
    lines.append("## 四、雷区一红（利润结构恶化）— 共 %d 只\n" % len(red_r1))
    lines.append("| 代码 | 名称 | 行业 | 说明 |")
    lines.append("|---|---|---|---|")
    for r in red_r1:
        lines.append(f"| {r.ts_code} | {r.name} | {r.industry} | {r.r1_detail} |")
    lines.append("")
    lines.append("## 五、雷区二红（现金流连续为负）— 共 %d 只\n" % len(red_r2))
    lines.append("> 最硬信号，与文章案例 *ST仕净（连续6年为负）一致。\n")
    lines.append("| 代码 | 名称 | 行业 | 连续为负年数 | 说明 |")
    lines.append("|---|---|---|---|---|")
    for r in red_r2:
        cons = len([y for y in r.annual if y.get("n_cashflow_act") is not None and y["n_cashflow_act"] < 0])
        lines.append(f"| {r.ts_code} | {r.name} | {r.industry} | {cons} | {r.r2_detail} |")
    lines.append("")
    lines.append("## 六、雷区三红（商誉/归母净资产>=50%）— 共 %d 只\n" % len(red_r3))
    lines.append("| 代码 | 名称 | 行业 | 说明 |")
    lines.append("|---|---|---|---|")
    for r in red_r3:
        lines.append(f"| {r.ts_code} | {r.name} | {r.industry} | {r.r3_detail} |")
    lines.append("")
    lines.append("## 七、中风险（黄）— 共 %d 只（样例前 50）\n" % len(yel))
    lines.append("| 代码 | 名称 | 行业 | 触发 |")
    lines.append("|---|---|---|---|")
    for r in yel[:50]:
        lines.append(f"| {r.ts_code} | {r.name} | {r.industry} | {'; '.join(r.reasons)} |")
    lines.append("")
    lines.append("## 八、低风险（绿）— 共 %d 只\n" % len(low))
    lines.append("> 三雷区均通过。\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"report written: {path} ({len(red_r1)} r1-red, {len(red_r2)} r2-red, {len(red_r3)} r3-red, {len(yel)} yellow, {len(low)} green)")


def run_baolei(*, all_mode: bool = False, codes: str = "", self_test: bool = False, report: str = "", db_path: str | None = None, repository=None) -> dict[str, object]:
    """baolei CLI 逻辑入口（由 app.cli 分发）。"""
    if self_test:
        test_codes = ["000001.SZ", "000002.SZ", "000008.SZ"]
        by_code, basic_map = bulk_fetch(db_path)
        payload = {"ok": True, "self_test": True, "results": []}
        for code in test_codes:
            if code in by_code:
                r = evaluate(code, basic_map.get(code, {}), by_code[code])
                print(f"{code} {r.name}: 评级={r.rating} 雷区一={r.r1}({r.r1_detail}) 雷区二={r.r2}({r.r2_detail}) 雷区三={r.r3}({r.r3_detail})")
                payload["results"].append({"ts_code": code, "rating": r.rating, "r1": r.r1, "r2": r.r2, "r3": r.r3})
            else:
                print(f"{code}: 无年报数据")
                payload["results"].append({"ts_code": code, "rating": None})
        return payload

    if codes:
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
        by_code, basic_map = bulk_fetch(db_path)
        payload = {"ok": True, "mode": "codes", "results": []}
        for code in code_list:
            if code in by_code:
                r = evaluate(code, basic_map.get(code, {}), by_code[code])
                print(f"{code} {r.name}: 评级={r.rating} | 雷区一={r.r1} {r.r1_detail} | 雷区二={r.r2} {r.r2_detail} | 雷区三={r.r3} {r.r3_detail}")
                payload["results"].append({"ts_code": code, "name": r.name, "rating": r.rating, "r1": r.r1, "r2": r.r2, "r3": r.r3, "reasons": r.reasons})
            else:
                print(f"{code}: 无年报数据")
                payload["results"].append({"ts_code": code, "rating": None})
        return payload

    if all_mode:
        results = run_all(db_path)
        if report:
            generate_markdown(results, report)
            return {"ok": True, "mode": "all", "report_path": str(Path(report).resolve()), "count": len(results)}
        c, r1c, r2c, r3c = summarize(results)
        print(f"扫描股票数: {len(results)}")
        print("综合暴雷可能性:", dict(c))
        print("雷区一(利润结构):", dict(r1c))
        print("雷区二(现金流):", dict(r2c))
        print("雷区三(商誉):", dict(r3c))
        red = [r for r in results if r.rating == "高"]
        yel = [r for r in results if r.rating == "中"]
        print(f"\n=== 高暴雷风险(红) 共 {len(red)} 只 ===")
        for r in sorted(red, key=lambda x: (x.r2 != RED, x.r1 != RED, x.r3 != RED, x.name)):
            print(f"{r.ts_code} {r.name} [{r.industry}] {r.reasons}")
        print(f"\n=== 中风险(黄) 共 {len(yel)} 只 ===")
        for r in sorted(yel, key=lambda x: x.name):
            print(f"{r.ts_code} {r.name} [{r.industry}] {r.reasons}")
        return {"ok": True, "mode": "all", "count": len(results), "rating_counts": dict(c), "r1_counts": dict(r1c), "r2_counts": dict(r2c), "r3_counts": dict(r3c)}

    raise DataInsufficientError("baolei 需要 --self-test / --codes / --all 之一", hint="示例：--codes 600519.SH,000001.SZ")