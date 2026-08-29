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
        {"exchange": "", "list_status": "L"},
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
    "moneyflow_ths": Dataset("moneyflow_ths", "moneyflow_ths", "moneyflow_ths", ("ts_code", "trade_date"), "trade_date"),
    "kpl_concept_cons": Dataset("kpl_concept_cons", "kpl_concept_cons", "kpl_concept_cons", ("ts_code", "con_code", "trade_date"), "trade_date"),
    "fina_indicator": Dataset("fina_indicator", "fina_indicator", "fina_indicator", ("ts_code", "end_date", "ann_date"), "stock"),
    "income": Dataset("income", "income", "income", ("ts_code", "end_date", "ann_date", "report_type"), "stock"),
    "balancesheet": Dataset("balancesheet", "balancesheet", "balancesheet", ("ts_code", "end_date", "ann_date", "report_type"), "stock"),
    "cashflow": Dataset("cashflow", "cashflow", "cashflow", ("ts_code", "end_date", "ann_date", "report_type"), "stock"),
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
    "moneyflow_ths",
    "kpl_concept_cons",
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
