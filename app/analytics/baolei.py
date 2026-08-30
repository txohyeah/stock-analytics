"""财报排雷批量扫描器（公司暴雷检查）——基于《财报排雷手册》三雷区逻辑。

从 stock-research baolei_check.py 迁移并改造：
- 数据源：MySQL(stock_fina_indicator) → **sqlite fina_indicator（tushare 原生表名）**
- 摆脱 SQLAlchemy/MySQL，仅依赖标准库 sqlite3
- fina_indicator 未同步（无表/空表）时抛 DataInsufficientError 并给出按需同步命令

评级映射（文章综合判定）：
  任一红 -> 高（排雷未通过）；有黄无红 -> 中（需深挖）；全绿 -> 低（通过排雷关）。
  ⚠️ 雷区三（商誉）数据源缺失，本评级是总暴雷风险的【下界】。
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
SYNC_HINT = "先同步 fina_indicator：./venv/bin/python -m app.cli sync fina_indicator --ts-codes <codes>（全市场：--mode history）"


@dataclass
class StockResult:
    ts_code: str
    name: str
    industry: str
    annual: list  # list of dict rows sorted desc by end_date
    r2: str = GREEN          # 雷区二 现金流
    r2_detail: str = ""
    r1_aux: str = GREEN      # 雷区一辅助 扣非同比趋势
    r1_detail: str = ""
    r3: str = "不可用"        # 雷区三 商誉（数据缺）
    rating: str = GREEN      # 综合 暴雷可能性
    reasons: list = field(default_factory=list)


def bulk_fetch(db_path: str | Path | None = None) -> tuple[dict[str, list], dict[str, dict]]:
    """一次性拉全市场年报（end_date 为 12-31）的所需字段 + 股票名。

    sqlite 中 fina_indicator.end_date 为 YYYYMMDD 字符串；年报判定 substr(end_date,5,4)='1231'。
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fina_indicator'"
        ).fetchone()
        if not exists:
            raise DataInsufficientError("fina_indicator 表未同步（sqlite 无此表）", hint=SYNC_HINT)
        rows = conn.execute(
            """
            SELECT ts_code, end_date, ocfps, ocf_to_profit, profit_dedt, dt_netprofit_yoy
            FROM fina_indicator
            WHERE substr(end_date, 5, 4) = '1231'
            ORDER BY ts_code, end_date DESC
            """
        ).fetchall()
        basic = conn.execute("SELECT ts_code, name, industry FROM stock_basic").fetchall()
    except sqlite3.Error as exc:
        raise DatabaseConnectionError(str(exc)) from exc
    finally:
        conn.close()

    if not rows:
        raise DataInsufficientError("fina_indicator 表为空（无年报数据）", hint=SYNC_HINT)

    basic_map = {r["ts_code"]: dict(r) for r in basic}
    by_code: dict[str, list] = defaultdict(list)
    for r in rows:
        by_code[r["ts_code"]].append(dict(r))
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

    # ---- 雷区二：现金流质量 ----
    # ocfps<0 等价于 经营现金流净额<0；ocf_to_profit = 经营现金流/净利润
    neg_years = [_end_year(r["end_date"]) for r in annual if r["ocfps"] is not None and r["ocfps"] < 0]
    # 连续为负（从最新往回数）的年数
    cons_neg = 0
    for r in annual:  # annual 已按 end_date desc
        if r["ocfps"] is not None and r["ocfps"] < 0:
            cons_neg += 1
        else:
            break
    latest = annual[0] if annual else None
    latest_ocf_ratio = latest["ocf_to_profit"] if latest else None
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
        res.r2 = GREEN
        res.r2_detail = "经营现金流持续为正且质量健康"

    # ---- 雷区一辅助：扣非同比趋势（缺归母净利润，仅作辅助）----
    dedt_yoy = [r["dt_netprofit_yoy"] for r in annual if r["dt_netprofit_yoy"] is not None]
    cons_dedt_neg = 0
    for y in dedt_yoy:  # annual desc => dedt_yoy 同序
        if y < 0:
            cons_dedt_neg += 1
        else:
            break
    if cons_dedt_neg >= 2:
        res.r1_aux = RED
        res.r1_detail = f"扣非净利润同比连续 {cons_dedt_neg} 年为负（主业盈利能力持续恶化）"
    elif cons_dedt_neg == 1 or (dedt_yoy and dedt_yoy[0] < -10):
        res.r1_aux = YELLOW
        res.r1_detail = "扣非同比下滑/单年为负（利润含金量偏弱）"
    else:
        res.r1_aux = GREEN
        res.r1_detail = "扣非同比为正（利润含金量好）"

    # ---- 综合判定 ----
    flags = [res.r2, res.r1_aux]
    if RED in flags:
        res.rating = "高"
    elif YELLOW in flags:
        res.rating = "中"
    else:
        res.rating = "低"
    if res.r2 == RED:
        res.reasons.append(f"雷区二·{res.r2_detail}")
    elif res.r2 == YELLOW:
        res.reasons.append(f"雷区二·{res.r2_detail}")
    if res.r1_aux == RED:
        res.reasons.append(f"雷区一(辅助)·{res.r1_detail}")
    elif res.r1_aux == YELLOW:
        res.reasons.append(f"雷区一(辅助)·{res.r1_detail}")
    if not res.reasons:
        res.reasons.append("现金流与扣非趋势均未触发预警")
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


def summarize(results) -> tuple[Counter, Counter, Counter]:
    c = Counter(r.rating for r in results)
    r2c = Counter(r.r2 for r in results)
    r1c = Counter(r.r1_aux for r in results)
    return c, r2c, r1c


def generate_markdown(results, path: str) -> None:
    c, r2c, r1c = summarize(results)
    red_r2 = [r for r in results if r.r2 == RED]
    red_r1 = [r for r in results if r.r1_aux == RED and r.r2 != RED]
    yel = [r for r in results if r.rating == "中"]
    low = [r for r in results if r.rating == "低"]
    red_r2.sort(key=lambda x: (len([y for y in x.annual if y["ocfps"] is not None and y["ocfps"] < 0]), x.name), reverse=True)
    red_r1.sort(key=lambda x: x.name)
    yel.sort(key=lambda x: x.name)
    lines = []
    lines.append(f"# 全市场财报排雷扫描（基于《财报排雷手册》三雷区）\n")
    lines.append(f"> 生成日期：{date.today()} ｜ 数据源：fina_indicator（tushare 同步 → sqlite）｜ 覆盖有年报数据股票 {len(results)} 只\n")
    lines.append("\n## 一、方法与数据口径（重要）\n")
    lines.append("- 文章三雷区：**雷区一 利润结构（扣非/归母）｜雷区二 现金流质量｜雷区三 商誉**。")
    lines.append("- **雷区二（现金流质量）已完整复算**：用 `ocfps`（每股经营现金流，符号≡经营现金流净额符号）+ `ocf_to_profit`（经营现金流/净利润，即文章第二个指标）。")
    lines.append("- **雷区一辅助（扣非同比趋势）**：文章精确口径需「归母净利润」算 扣非/归母 比率，但 `income` 尚未全市场同步，归母净利润不可得；故仅以 `dt_netprofit_yoy`（扣非净利润同比）连续为负作**辅助信号**，非文章精确比率。")
    lines.append("- **雷区三（商誉）不可用**：`balancesheet` 需按 ts_code 逐股同步，商誉/归母净资产暂不可得，无法评估。")
    lines.append("- ⚠️ **因此本评级是总暴雷风险的「下界」**：标「低」的票仍可能带商誉雷；标「高」主要来自现金流持续失血（硬信号）或扣非同比持续为负（软信号）。\n")
    lines.append("## 二、评级定义（文章综合判定）\n")
    lines.append("- 任一红 → **高（排雷未通过）**；有黄无红 → **中（需深挖）**；全绿 → **低（通过排雷关）**。\n")
    lines.append("## 三、概览统计\n")
    lines.append(f"- 综合暴雷可能性：**高 {c.get('高',0)} ｜ 中 {c.get('中',0)} ｜ 低 {c.get('低',0)}**")
    lines.append(f"- 雷区二 现金流：红 {r2c.get('红',0)} ｜ 黄 {r2c.get('黄',0)} ｜ 绿 {r2c.get('绿',0)}")
    lines.append(f"- 雷区一辅助 扣非同比：红 {r1c.get('红',0)} ｜ 黄 {r1c.get('黄',0)} ｜ 绿 {r1c.get('绿',0)}\n")
    lines.append("## 四、核心高风险：雷区二红（现金流连续 2 年及以上为负）— 共 %d 只\n" % len(red_r2))
    lines.append("> 最硬信号，与文章案例 *ST仕净（连续6年为负）一致。\n")
    lines.append("| 代码 | 名称 | 行业 | 连续为负年数 | 说明 |")
    lines.append("|---|---|---|---|---|")
    for r in red_r2:
        cons = len([y for y in r.annual if y["ocfps"] is not None and y["ocfps"] < 0])
        lines.append(f"| {r.ts_code} | {r.name} | {r.industry} | {cons} | {r.r2_detail} |")
    lines.append("")
    lines.append("## 五、辅助高风险：雷区一aux红（扣非同比连续 2 年为负）— 共 %d 只\n" % len(red_r1))
    lines.append("> 软信号（缺归母净利润，非文章精确比率）。仅列名称与行业，供交叉参考。\n")
    lines.append("| 代码 | 名称 | 行业 | 说明 |")
    lines.append("|---|---|---|---|")
    for r in red_r1:
        lines.append(f"| {r.ts_code} | {r.name} | {r.industry} | {r.r1_detail} |")
    lines.append("")
    lines.append("## 六、中风险（黄）— 共 %d 只（样例前 50）\n" % len(yel))
    lines.append("| 代码 | 名称 | 行业 | 触发 |")
    lines.append("|---|---|---|---|")
    for r in yel[:50]:
        lines.append(f"| {r.ts_code} | {r.name} | {r.industry} | {'; '.join(r.reasons)} |")
    lines.append("")
    lines.append("## 七、低风险（绿）— 共 %d 只\n" % len(low))
    lines.append("> 现金流持续为正且扣非同比为正，通过本次可计算的排雷关（不含商誉雷评估）。\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"report written: {path} ({len(red_r2)} core-red, {len(red_r1)} aux-red, {len(yel)} yellow, {len(low)} green)")


def run_baolei(*, all_mode: bool = False, codes: str = "", self_test: bool = False, report: str = "", db_path: str | None = None, repository=None) -> dict[str, object]:
    """baolei CLI 逻辑入口（由 app.cli 分发）。"""
    if self_test:
        test_codes = ["000572.SH", "301030.SZ", "002343.SZ"]
        by_code, basic_map = bulk_fetch(db_path)
        payload = {"ok": True, "self_test": True, "results": []}
        for code in test_codes:
            if code in by_code:
                r = evaluate(code, basic_map.get(code, {}), by_code[code])
                print(f"{code} {r.name}: 评级={r.rating} 雷区二={r.r2}({r.r2_detail}) 雷区一aux={r.r1_aux}({r.r1_detail})")
                payload["results"].append({"ts_code": code, "rating": r.rating, "r2": r.r2, "r1_aux": r.r1_aux})
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
                print(f"{code} {r.name}: 评级={r.rating} | 雷区二={r.r2} {r.r2_detail} | 雷区一aux={r.r1_aux} {r.r1_detail}")
                payload["results"].append({"ts_code": code, "name": r.name, "rating": r.rating, "r2": r.r2, "r1_aux": r.r1_aux, "reasons": r.reasons})
            else:
                print(f"{code}: 无年报数据")
                payload["results"].append({"ts_code": code, "rating": None})
        return payload

    if all_mode:
        results = run_all(db_path)
        if report:
            generate_markdown(results, report)
            return {"ok": True, "mode": "all", "report_path": str(Path(report).resolve()), "count": len(results)}
        c, r2c, r1c = summarize(results)
        print(f"扫描股票数: {len(results)}")
        print("综合暴雷可能性:", dict(c))
        print("雷区二(现金流):", dict(r2c))
        print("雷区一辅助(扣非同比):", dict(r1c))
        red = [r for r in results if r.rating == "高"]
        yel = [r for r in results if r.rating == "中"]
        print(f"\n=== 高暴雷风险(红) 共 {len(red)} 只 ===")
        for r in sorted(red, key=lambda x: (x.r2 != RED, x.name)):
            print(f"{r.ts_code} {r.name} [{r.industry}] {r.reasons}")
        print(f"\n=== 中风险(黄) 共 {len(yel)} 只 ===")
        for r in sorted(yel, key=lambda x: x.name):
            print(f"{r.ts_code} {r.name} [{r.industry}] {r.reasons}")
        return {"ok": True, "mode": "all", "count": len(results), "rating_counts": dict(c), "r2_counts": dict(r2c), "r1_aux_counts": dict(r1c)}

    raise DataInsufficientError("baolei 需要 --self-test / --codes / --all 之一", hint="示例：--codes 600519.SH,000001.SZ")