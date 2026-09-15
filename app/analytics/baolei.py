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
from collections import defaultdict
from dataclasses import dataclass, field
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
    deep_checks: list = field(default_factory=list)  # 深度排雷检查项（李神奇方法扩展）
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
            "SELECT ts_code, end_date, ann_date, n_income, n_income_attr_p, revenue, "
            "fv_value_chg_gain, invest_income, rd_exp, n_oth_income, assets_impair_loss FROM income "
            "WHERE substr(end_date,5,4)='1231'"
        )
        q_cashflow = (
            "SELECT ts_code, end_date, ann_date, n_cashflow_act, c_fr_sale_sg FROM cashflow "
            "WHERE substr(end_date,5,4)='1231'"
        )
        q_balancesheet = (
            "SELECT ts_code, end_date, ann_date, goodwill, total_hldr_eqy_exc_min_int, "
            "inventories, payroll_payable, money_cap, st_borr, lt_borr, bond_payable FROM balancesheet "
            "WHERE substr(end_date,5,4)='1231'"
        )
        q_fina = (
            "SELECT ts_code, end_date, ann_date, profit_dedt, dt_netprofit_yoy, netprofit_yoy, "
            "netprofit_margin, grossprofit_margin, turn_days, interestdebt, debt_to_assets FROM fina_indicator "
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
    # 合并表：按 (ts_code, end_date) 聚合四张表。
    # 同一报告期在 tushare 里可能有多个 ann_date（业绩快报、年报更正等），其中
    # 部分行只填主键、其余字段为空。原实现按 SQL 返回顺序无条件赋值，等价于
    # "最后一行赢"，而"最后一行"取决于查询计划（走索引还是全表扫），一旦取到空壳行
    # 就把真实数据读成 None。改为按公告日升序遍历、只接受有值的字段：每列取
    # "最新公告里非空的那个值"，空壳行再也盖不掉真实数据，且不再依赖返回顺序。
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for src, tags in (
        (
            rows_income,
            (
                "n_income",
                "n_income_attr_p",
                "revenue",
                "fv_value_chg_gain",
                "invest_income",
                "rd_exp",
                "n_oth_income",
                "assets_impair_loss",
            ),
        ),
        (rows_cf, ("n_cashflow_act", "c_fr_sale_sg")),
        (
            rows_bs,
            (
                "goodwill",
                "total_hldr_eqy_exc_min_int",
                "inventories",
                "payroll_payable",
                "money_cap",
                "st_borr",
                "lt_borr",
                "bond_payable",
            ),
        ),
        (
            rows_fina,
            (
                "profit_dedt",
                "dt_netprofit_yoy",
                "netprofit_yoy",
                "netprofit_margin",
                "grossprofit_margin",
                "turn_days",
                "interestdebt",
                "debt_to_assets",
            ),
        ),
    ):
        for r in sorted(src, key=lambda x: str(x["ann_date"] or "")):
            key = (r["ts_code"], r["end_date"])
            d = merged.get(key)
            if d is None:
                d = merged[key] = {
                    "ts_code": r["ts_code"],
                    "end_date": r["end_date"],
                    "ann_date": r["ann_date"],
                }
            if r["ann_date"] and (not d.get("ann_date") or r["ann_date"] >= d["ann_date"]):
                d["ann_date"] = r["ann_date"]
            for tag in tags:
                if _present(r[tag]):
                    d[tag] = r[tag]

    basic_map = {r["ts_code"]: dict(r) for r in basic}
    by_code: dict[str, list] = defaultdict(list)
    for (code, _end), d in merged.items():
        by_code[code].append(d)
    for code in by_code:
        by_code[code].sort(key=lambda x: str(x["end_date"]), reverse=True)
    return by_code, basic_map, audit_map, trend_map


def _present(v: Any) -> bool:
    """字段是否"有值"。None、空串、纯空白、NaN 都算缺失。

    用于同一报告期多公告日的归并：只有"有值"的字段才允许覆盖已有值，
    空壳行（tushare 对某些 ann_date 只填主键）不能把真实数据抹掉。
    """
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, float):
        return v == v  # 过滤 NaN
    return True


def _to_float(v: Any) -> float | None:
    """TEXT 字段安全转 float（空串/None/非法值 → None）。"""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _deep_checks(annual: list, trend: dict[str, Any] | None = None, industry: str = "") -> list[dict]:
    """深度排雷检查项（李神奇方法 + 财报排雷手册扩展）。

    返回 [{name, level, detail}]，level ∈ 红/黄/绿。数据不足时该项跳过（sk）。
    全部基于年报（annual desc by end_date）+ 最新报告期趋势（trend）。
    金融股（银行/保险/证券）投资收益/公允价值变动是主业，跳过相关检查。
    """
    checks: list[dict] = []
    latest = annual[0] if annual else None
    if latest is None:
        return checks

    is_fin = any(k in (industry or "") for k in ("银行", "保险", "证券", "多元金融"))

    ni = _to_float(latest.get("n_income_attr_p"))  # 归母净利润
    rev = _to_float(latest.get("revenue"))
    ocf = _to_float(latest.get("n_cashflow_act"))
    eq = _to_float(latest.get("total_hldr_eqy_exc_min_int"))

    # ---------- 利润质量 ----------
    # 1. 公允价值变动收益占比（昭衍新药猴子案例：利润全靠资产增值）
    fv = _to_float(latest.get("fv_value_chg_gain"))
    if fv is not None and ni is not None and ni > 0 and not is_fin:
        ratio = fv / ni
        if ratio > 0.5:
            checks.append({"name": "公允价值变动收益占比", "level": RED,
                           "detail": f"公允价值变动收益 {fv/1e8:.2f}亿 / 归母净利润 {ni/1e8:.2f}亿 = {ratio:.0%}（>50%，利润靠资产增值撑）"})
        elif ratio > 0.3:
            checks.append({"name": "公允价值变动收益占比", "level": YELLOW,
                           "detail": f"公允价值变动收益占归母净利润 {ratio:.0%}（30%~50%，利润含金量偏弱）"})
        else:
            checks.append({"name": "公允价值变动收益占比", "level": GREEN,
                           "detail": f"公允价值变动收益占归母净利润 {ratio:.0%}（<=30%，健康）"})

    # 2. 投资收益占比（药明康德案例：投资收益占利润一小半 = 业绩虚高）
    inv = _to_float(latest.get("invest_income"))
    if inv is not None and ni is not None and ni > 0 and not is_fin:
        ratio = inv / ni
        if ratio > 0.5:
            checks.append({"name": "投资收益占比", "level": RED,
                           "detail": f"投资收益 {inv/1e8:.2f}亿 / 归母净利润 {ni/1e8:.2f}亿 = {ratio:.0%}（>50%，主业成色差）"})
        elif ratio > 0.3:
            checks.append({"name": "投资收益占比", "level": YELLOW,
                           "detail": f"投资收益占归母净利润 {ratio:.0%}（30%~50%，利润依赖投资收益）"})
        else:
            checks.append({"name": "投资收益占比", "level": GREEN,
                           "detail": f"投资收益占归母净利润 {ratio:.0%}（<=30%，健康）"})

    # 3. 净利率偏离历史均值（德明利案例：44% vs 历史 10% = 异常）
    npm = _to_float(latest.get("netprofit_margin"))
    hist_npm = [_to_float(r.get("netprofit_margin")) for r in annual[1:]]
    hist_npm = [x for x in hist_npm if x is not None]
    if npm is not None and len(hist_npm) >= 3:
        avg = sum(hist_npm) / len(hist_npm)
        if avg > 0:
            dev = npm / avg
            if dev > 2.0:
                checks.append({"name": "净利率偏离历史", "level": RED,
                               "detail": f"最新净利率 {npm:.1f}% vs 历史均值 {avg:.1f}%（{dev:.1f}倍，异常偏离，警惕利润调节）"})
            elif dev > 1.5:
                checks.append({"name": "净利率偏离历史", "level": YELLOW,
                               "detail": f"最新净利率 {npm:.1f}% vs 历史均值 {avg:.1f}%（{dev:.1f}倍，偏离偏大）"})
            else:
                checks.append({"name": "净利率偏离历史", "level": GREEN,
                               "detail": f"最新净利率 {npm:.1f}% vs 历史均值 {avg:.1f}%（{dev:.1f}倍，正常范围）"})

    # 4. 毛利净利差过小（联创光电案例：毛利率=净利率15%，消费电子不可能）
    gpm = _to_float(latest.get("grossprofit_margin"))
    if gpm is not None and npm is not None and npm > 8 and not is_fin:
        gap = gpm - npm
        if gap < 3:
            checks.append({"name": "毛利净利差", "level": RED,
                           "detail": f"毛利率 {gpm:.1f}% - 净利率 {npm:.1f}% = {gap:.1f}pct（差 <3pct，四费几乎为零，必有非主营收益撑利润）"})
        elif gap < 8:
            checks.append({"name": "毛利净利差", "level": YELLOW,
                           "detail": f"毛利率 {gpm:.1f}% - 净利率 {npm:.1f}% = {gap:.1f}pct（差 <8pct，期间费用异常低）"})
        else:
            checks.append({"name": "毛利净利差", "level": GREEN,
                           "detail": f"毛利率 {gpm:.1f}% - 净利率 {npm:.1f}% = {gap:.1f}pct（正常）"})

    # ---------- 资产与现金流 ----------
    # 5. 存货增速 vs 营收增速背离（江波龙囤货 257 亿案例）
    cur_inv = _to_float(latest.get("inventories"))
    prev = annual[1] if len(annual) > 1 else None
    prev_inv = _to_float(prev.get("inventories")) if prev else None
    prev_rev = _to_float(prev.get("revenue")) if prev else None
    if cur_inv is not None and prev_inv and prev_inv > 0 and rev is not None and prev_rev and prev_rev > 0:
        inv_g = (cur_inv - prev_inv) / prev_inv
        rev_g = (rev - prev_rev) / prev_rev
        gap = inv_g - rev_g
        if gap > 0.3:
            checks.append({"name": "存货/营收背离", "level": RED,
                           "detail": f"存货增速 {inv_g:.0%} vs 营收增速 {rev_g:.0%}（存货快 {gap:.0%}，囤货/压货信号）"})
        elif gap > 0.15:
            checks.append({"name": "存货/营收背离", "level": YELLOW,
                           "detail": f"存货增速 {inv_g:.0%} vs 营收增速 {rev_g:.0%}（存货快 {gap:.0%}，需关注）"})
        else:
            checks.append({"name": "存货/营收背离", "level": GREEN,
                           "detail": f"存货增速 {inv_g:.0%} vs 营收增速 {rev_g:.0%}（匹配）"})

    # 6. 净资产侵蚀速度（卓翼科技案例：净资产÷年亏损=还能撑几年）
    if eq is not None and eq > 0 and ni is not None and ni < 0:
        years = eq / abs(ni)
        if years < 3:
            checks.append({"name": "净资产侵蚀", "level": RED,
                           "detail": f"净资产 {eq/1e8:.2f}亿 ÷ 年亏损 {abs(ni)/1e8:.2f}亿 = 还能撑 {years:.1f} 年（<3 年，资不抵债风险）"})
        elif years < 5:
            checks.append({"name": "净资产侵蚀", "level": YELLOW,
                           "detail": f"净资产 {eq/1e8:.2f}亿 ÷ 年亏损 {abs(ni)/1e8:.2f}亿 = 还能撑 {years:.1f} 年（<5 年）"})
        else:
            checks.append({"name": "净资产侵蚀", "level": GREEN,
                           "detail": f"净资产 {eq/1e8:.2f}亿 ÷ 年亏损 {abs(ni)/1e8:.2f}亿 = 还能撑 {years:.1f} 年"})

    # 7. 收现比（江波龙案例：收入 vs 实际收到现金）
    sale_cash = _to_float(latest.get("c_fr_sale_sg"))
    if sale_cash is not None and rev is not None and rev > 0 and not is_fin:
        ratio = sale_cash / rev
        if ratio < 0.6:
            checks.append({"name": "收现比", "level": RED,
                           "detail": f"销售收现 {sale_cash/1e8:.2f}亿 / 营收 {rev/1e8:.2f}亿 = {ratio:.2f}（<0.6，收入含金量差）"})
        elif ratio < 0.8:
            checks.append({"name": "收现比", "level": YELLOW,
                           "detail": f"销售收现 {sale_cash/1e8:.2f}亿 / 营收 {rev/1e8:.2f}亿 = {ratio:.2f}（0.6~0.8，需关注）"})
        else:
            checks.append({"name": "收现比", "level": GREEN,
                           "detail": f"销售收现 {sale_cash/1e8:.2f}亿 / 营收 {rev/1e8:.2f}亿 = {ratio:.2f}（>=0.8，健康）"})

    # 8. 应付薪酬 vs 现金（中公教育案例：应付薪酬 3.39亿 vs 现金 1亿 = 发不出工资风险）
    payroll = _to_float(latest.get("payroll_payable"))
    money = _to_float(latest.get("money_cap"))
    if payroll is not None and money is not None and money > 0:
        ratio = payroll / money
        if ratio > 3:
            checks.append({"name": "应付薪酬/现金", "level": RED,
                           "detail": f"应付职工薪酬 {payroll/1e8:.2f}亿 vs 货币资金 {money/1e8:.2f}亿（{ratio:.1f}倍，发不出工资风险）"})
        elif ratio > 1.5:
            checks.append({"name": "应付薪酬/现金", "level": YELLOW,
                           "detail": f"应付职工薪酬 {payroll/1e8:.2f}亿 vs 货币资金 {money/1e8:.2f}亿（{ratio:.1f}倍，需关注）"})
        else:
            checks.append({"name": "应付薪酬/现金", "level": GREEN,
                           "detail": f"应付职工薪酬 {payroll/1e8:.2f}亿 vs 货币资金 {money/1e8:.2f}亿（{ratio:.1f}倍，正常）"})

    # 9. 十年累计盈亏（百花医药案例：十年亏 23 亿赚 3 亿 = 大亏小赚）
    if len(annual) >= 5:
        cum = sum(_to_float(r.get("n_income_attr_p")) or 0 for r in annual)
        loss_years = sum(1 for r in annual if (_to_float(r.get("n_income_attr_p")) or 0) < 0)
        if cum < 0:
            checks.append({"name": "十年累计盈亏", "level": RED,
                           "detail": f"近 {len(annual)} 年累计归母 {cum/1e8:.2f}亿（亏损年 {loss_years}/{len(annual)}，大亏小赚）"})
        elif loss_years > len(annual) / 2:
            checks.append({"name": "十年累计盈亏", "level": YELLOW,
                           "detail": f"近 {len(annual)} 年累计归母 {cum/1e8:.2f}亿但亏损年 {loss_years}/{len(annual)}（盈利不稳定）"})
        else:
            checks.append({"name": "十年累计盈亏", "level": GREEN,
                           "detail": f"近 {len(annual)} 年累计归母 {cum/1e8:.2f}亿（亏损年 {loss_years}/{len(annual)}）"})

    # 10. 营业周期（派瑞股份案例：营业周期 800 天 = 收入确认可操纵空间大）
    # 注意：白酒/地产等天然长周期行业（茅台 1399 天）属行业特性，仅作提示不参与综合评级
    turn = _to_float(latest.get("turn_days"))
    if turn is not None:
        if turn > 730:
            checks.append({"name": "营业周期", "level": RED,
                           "detail": f"营业周期 {turn:.0f} 天（>730 天，订单到交付超两年，收入确认可操纵空间大；白酒/地产等长周期行业属特性，需结合业绩判断）"})
        elif turn > 365:
            checks.append({"name": "营业周期", "level": YELLOW,
                           "detail": f"营业周期 {turn:.0f} 天（365~730 天，偏长）"})
        else:
            checks.append({"name": "营业周期", "level": GREEN,
                           "detail": f"营业周期 {turn:.0f} 天（<=365 天，正常）"})

    # 11. 债务偿还年限（京东方案例：有息负债 ÷ 年利润 = 还债要几年）
    intdebt = _to_float(latest.get("interestdebt"))
    if intdebt is not None and ni is not None and ni > 0 and not is_fin:
        years = intdebt / ni
        if years > 20:
            checks.append({"name": "债务偿还年限", "level": RED,
                           "detail": f"有息负债 {intdebt/1e8:.2f}亿 ÷ 年利润 {ni/1e8:.2f}亿 = {years:.1f} 年（>20 年，债务沉重）"})
        elif years > 10:
            checks.append({"name": "债务偿还年限", "level": YELLOW,
                           "detail": f"有息负债 {intdebt/1e8:.2f}亿 ÷ 年利润 {ni/1e8:.2f}亿 = {years:.1f} 年（10~20 年）"})
        else:
            checks.append({"name": "债务偿还年限", "level": GREEN,
                           "detail": f"有息负债 {intdebt/1e8:.2f}亿 ÷ 年利润 {ni/1e8:.2f}亿 = {years:.1f} 年（<=10 年，正常）"})

    # 12. 单季转负预警（派瑞股份案例：上坡路企业单季突然转负 = 要搞幺蛾子）
    if trend and trend.get("latest_ed"):
        # trend 只有最新报告期 vs 去年同期；单季转负需更多季度数据，此处用最新报告期归母为负 + 上年同期为正判断
        cur_np = trend.get("np")
        prev_np = trend.get("prev_np")
        if cur_np is not None and prev_np is not None and cur_np < 0 and prev_np > 0:
            checks.append({"name": "单季转负预警", "level": YELLOW,
                           "detail": f"{trend['latest_ed'][:4]}-{trend['latest_ed'][4:6]} 归母 {cur_np/1e8:.2f}亿 vs 去年同期 {prev_np/1e8:.2f}亿（上坡路企业单季转负，警惕后续动作）"})

    return checks


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

    # ================= 深度排雷检查（李神奇方法扩展） =================
    res.deep_checks = _deep_checks(annual, trend, basic.get("industry") or "")

    # ================= 综合判定（五档） =================
    flags = [res.r0, res.r1, res.r2, res.r3, res.r4]
    levels = [f for f in flags if f in (RED, YELLOW, GREEN)]
    # 营业周期为提示项（白酒/地产等长周期行业天然偏长），不参与综合评级
    deep_levels = [c["level"] for c in res.deep_checks if c["level"] in (RED, YELLOW, GREEN) and c["name"] != "营业周期"]
    if RED in levels or RED in deep_levels:
        res.rating = "高"
    elif YELLOW in levels or YELLOW in deep_levels:
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
    for c in res.deep_checks:
        if c["level"] in (RED, YELLOW):
            res.reasons.append(f"深度·{c['name']}：{c['detail']}")
    if not res.reasons:
        res.reasons.append("五雷区及深度检查均未触发预警")
    return res


def _end_year(end_date: Any) -> int:
    return int(str(end_date)[:4])


def _format_report(r: StockResult) -> str:
    """单票完整排雷报告：综合评级 + 触发项 + 详细检查（五雷区 + 深度）。"""
    lines = []
    lines.append(f"# {r.ts_code} {r.name} 排雷报告")
    lines.append(f"- 行业：{r.industry or '-'} ｜ 综合评级：**{r.rating}**（任一红=高，有黄=中，全绿=低）")
    lines.append("")
    lines.append("## 触发项（红/黄）")
    if r.reasons:
        for reason in r.reasons:
            lines.append(f"- {reason}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 五雷区")
    for tag, level, detail in (
        ("零 审计意见", r.r0, r.r0_detail),
        ("一 利润结构", r.r1, r.r1_detail),
        ("二 现金流质量", r.r2, r.r2_detail),
        ("三 商誉", r.r3, r.r3_detail),
        ("四 业绩拐点", r.r4, r.r4_detail),
    ):
        lines.append(f"- [{level}] 雷区{tag}：{detail}")
    lines.append("")
    lines.append("## 深度检查（利润质量 / 资产现金流 / 结构异常）")
    if r.deep_checks:
        for c in r.deep_checks:
            lines.append(f"- [{c['level']}] {c['name']}：{c['detail']}")
    else:
        lines.append("- 数据不足，跳过")
    return "\n".join(lines)


def run_baolei(*, codes: str = "", self_test: bool = False, report: str = "", db_path: str | None = None, repository=None) -> dict[str, object]:
    """baolei CLI 逻辑入口（由 app.cli 分发）。按需指定股票排雷，不做全市场扫描。"""
    if self_test:
        test_codes = ["000001.SZ", "000002.SZ", "000008.SZ"]
        by_code, basic_map, audit_map, trend_map = bulk_fetch(db_path)
        payload = {"ok": True, "self_test": True, "results": []}
        for code in test_codes:
            if code in by_code:
                r = evaluate(code, basic_map.get(code, {}), by_code[code], audit_map, trend_map.get(code))
                print(f"{code} {r.name}: 评级={r.rating} 零={r.r0}({r.r0_detail}) 一={r.r1}({r.r1_detail}) 二={r.r2}({r.r2_detail}) 三={r.r3}({r.r3_detail}) 四={r.r4}({r.r4_detail}) 深度={len(r.deep_checks)}项")
                payload["results"].append({"ts_code": code, "rating": r.rating, "r0": r.r0, "r1": r.r1, "r2": r.r2, "r3": r.r3, "r4": r.r4, "deep_checks": r.deep_checks})
            else:
                print(f"{code}: 无年报数据")
                payload["results"].append({"ts_code": code, "rating": None})
        return payload

    if not codes:
        raise DataInsufficientError("baolei 需要 --codes 指定股票（逗号分隔）或 --self-test", hint="示例：--codes 600519.SH,000001.SZ")

    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    by_code, basic_map, audit_map, trend_map = bulk_fetch(db_path)
    payload = {"ok": True, "mode": "codes", "results": []}
    reports = []
    for code in code_list:
        if code in by_code:
            r = evaluate(code, basic_map.get(code, {}), by_code[code], audit_map, trend_map.get(code))
            print(f"{code} {r.name}: 评级={r.rating} | 零={r.r0} | 一={r.r1} | 二={r.r2} | 三={r.r3} | 四={r.r4} | 深度={len(r.deep_checks)}项")
            reports.append(_format_report(r))
            payload["results"].append({"ts_code": code, "name": r.name, "rating": r.rating, "r0": r.r0, "r1": r.r1, "r2": r.r2, "r3": r.r3, "r4": r.r4, "deep_checks": r.deep_checks, "reasons": r.reasons})
        else:
            print(f"{code}: 无年报数据")
            payload["results"].append({"ts_code": code, "rating": None})
    if report:
        with open(report, "w", encoding="utf-8") as f:
            f.write("\n\n".join(reports))
        payload["report_path"] = str(Path(report).resolve())
    return payload