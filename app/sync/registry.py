from __future__ import annotations

from app.sync.base import Dataset


DATASETS: dict[str, Dataset] = {
    "trade_cal": Dataset("trade_cal", "trade_cal", "trade_cal", ("exchange", "cal_date"), "trade_cal"),
    "stock_basic": Dataset(
        "stock_basic",
        "stock_basic",
        "stock_basic",
        ("ts_code",),
        "basic",
        {
            "exchange": "",
            "list_status": "L",
            # 显式全字段：默认返回仅 10 列（缺 fullname/exchange/list_status 等 A股分析所需字段）
            "fields": "ts_code,symbol,name,area,industry,fullname,enname,cnspell,market,exchange,curr_type,list_status,list_date,delist_date,is_hs,act_name,act_ent_type",
        },
    ),
    "daily": Dataset("daily", "daily", "daily", ("ts_code", "trade_date"), "trade_date"),
    "daily_basic": Dataset("daily_basic", "daily_basic", "daily_basic", ("ts_code", "trade_date"), "trade_date"),
    "adj_factor": Dataset("adj_factor", "adj_factor", "adj_factor", ("ts_code", "trade_date"), "trade_date"),
    "index_basic": Dataset(
        "index_basic",
        "index_basic",
        "index_basic",
        ("ts_code",),
        "index_basic",
    ),
    "index_daily": Dataset("index_daily", "index_daily", "index_daily", ("ts_code", "trade_date"), "trade_date"),
    "index_daily_basic": Dataset("index_daily_basic", "index_dailybasic", "index_daily_basic", ("ts_code", "trade_date"), "trade_date"),
    # moneyflow_ths（同花顺资金流）/ kpl_concept_cons（概念成分）：Tushare 2000 积分档实测无访问权限
    # （2026-08-29 确认，见 memory/2026-08-29/tushare-sync-redesign.md），已从 DAILY_ORDER 摘除避免
    # 每个交易日 20:10 定时同步报错并触发飞书通知；待积分升档后再恢复。
    # "moneyflow_ths": Dataset("moneyflow_ths", "moneyflow_ths", "moneyflow_ths", ("ts_code", "trade_date"), "trade_date"),
    # "kpl_concept_cons": Dataset("kpl_concept_cons", "kpl_concept_cons", "kpl_concept_cons", ("ts_code", "con_code", "trade_date"), "trade_date"),
    "fina_indicator": Dataset("fina_indicator", "fina_indicator", "fina_indicator", ("ts_code", "end_date", "ann_date"), "stock"),
    "income": Dataset("income", "income", "income", ("ts_code", "end_date", "ann_date", "report_type"), "stock"),
    "balancesheet": Dataset("balancesheet", "balancesheet", "balancesheet", ("ts_code", "end_date", "ann_date", "report_type"), "stock"),
    "cashflow": Dataset("cashflow", "cashflow", "cashflow", ("ts_code", "end_date", "ann_date", "report_type"), "stock"),
    # 排雷审计意见（baolei 雷区零，按股全量拉：带日期会漏最新年报审计意见）
    "fina_audit": Dataset("fina_audit", "fina_audit", "fina_audit", ("ts_code", "end_date"), "stock_no_date"),
    # 龙虎榜每日明细（lhb 信号，按交易日同步）
    "top_list": Dataset("top_list", "top_list", "top_list", ("trade_date", "ts_code"), "trade_date"),
    # 上市公司资料（主营/省份等，lhb 细分链归类用；basic 策略=单次全量拉取，需 2000 积分档）
    "stock_company": Dataset("stock_company", "stock_company", "stock_company", ("ts_code",), "basic"),
}

BOOTSTRAP_ORDER = ("trade_cal", "stock_basic")

DAILY_ORDER = (
    "trade_cal",
    "stock_basic",
    "daily",
    "daily_basic",
    "adj_factor",
    "index_basic",
    "index_daily",
    "index_daily_basic",
    # moneyflow_ths / kpl_concept_cons：2000 积分档无权限（2026-08-29 实测），暂不纳入定时同步
    # "moneyflow_ths",
    # "kpl_concept_cons",
)

FINANCE_ORDER = ("fina_indicator", "income", "balancesheet", "cashflow")

ALL_ORDER = DAILY_ORDER + FINANCE_ORDER


def get_dataset(name: str) -> Dataset:
    try:
        return DATASETS[name]
    except KeyError as exc:
        known = ", ".join(sorted(DATASETS))
        raise ValueError(f"Unknown dataset: {name}. Known datasets: {known}") from exc


def datasets_for(name: str) -> list[Dataset]:
    if name == "all":
        return [DATASETS[item] for item in ALL_ORDER]
    if name == "daily_group":
        return [DATASETS[item] for item in DAILY_ORDER]
    if name == "finance_group":
        return [DATASETS[item] for item in FINANCE_ORDER]
    return [get_dataset(name)]
