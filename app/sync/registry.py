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
    # 个股资金流向（标准版，2000 积分档可调；2026-09-01 实测解锁，按交易日全市场拉取）
    "moneyflow": Dataset("moneyflow", "moneyflow", "moneyflow", ("ts_code", "trade_date"), "trade_date"),
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
    # 龙虎榜每日明细（lhb 信号，按交易日同步）。2026-09-22 纳入 market 组：
    # 旧 stock_research 停用后此表无定时调度，数据曾断档（停在 2026-08-28）
    "top_list": Dataset("top_list", "top_list", "top_list", ("trade_date", "ts_code"), "trade_date"),
    # 龙虎榜席位明细（营业部/机构逐条，side=0买入/1卖出，exalter=席位名）。
    # 唯一键必须含金额列：同一榜单"机构专用"会多行出现（不同机构），
    # (trade_date,ts_code,exalter,side,reason) 五列键实测单日 67 行撞车（2026-09-22）
    "top_inst": Dataset(
        "top_inst",
        "top_inst",
        "top_inst",
        ("trade_date", "ts_code", "exalter", "side", "reason", "buy", "sell", "net_buy"),
        "trade_date",
    ),
    # 上市公司资料（主营/省份等，lhb 细分链归类用；basic 策略=单次全量拉取，需 2000 积分档）
    "stock_company": Dataset("stock_company", "stock_company", "stock_company", ("ts_code",), "basic"),
    # 三路聪明钱跟踪（2026-09-12 新增，见 specs/smart-money-tracking.md）
    # 前十大股东/流通股东：社保/汇金/养老金/公募都在这里；holders 策略=按 period 全市场分页拉
    "top10_holders": Dataset(
        "top10_holders",
        "top10_holders",
        "top10_holders",
        ("ts_code", "end_date", "ann_date", "holder_name"),
        "holders",
    ),
    "top10_floatholders": Dataset(
        "top10_floatholders",
        "top10_floatholders",
        "top10_floatholders",
        ("ts_code", "end_date", "ann_date", "holder_name"),
        "holders",
    ),
    # 基金列表（场外，market='O'；主动权益筛选在 sync_fund_holdings 内做）
    # 策略 fund_basic_paged：tushare 单次上限 5000 行，必须分页拉全（否则漏老基金）
    "fund_basic": Dataset(
        "fund_basic",
        "fund_basic",
        "fund_basic",
        ("ts_code",),
        "fund_basic_paged",
        {"market": "O", "status": "L"},
    ),
    # 基金前十大重仓股（天天基金爬虫，fund_holdings 策略）
    "fund_holdings": Dataset(
        "fund_holdings",
        "",
        "fund_holdings",
        ("fund_code", "ts_code", "end_date"),
        "fund_holdings",
    ),
    # ---- 宏观（2026-09-16 新增，见 memory/2026-09-16/macro-data-sources.md）----
    # 月度序列：历史总量仅数百行，走 basic 策略一次全量拉取 + 幂等 upsert
    # （sf_month 实测传 start_month 会被忽略、仍返回全量，所以不加日期参数）
    "sf_month": Dataset("sf_month", "sf_month", "sf_month", ("month",), "basic"),
    "cn_m": Dataset("cn_m", "cn_m", "cn_m", ("month",), "basic"),
    "cn_cpi": Dataset("cn_cpi", "cn_cpi", "cn_cpi", ("month",), "basic"),
    "cn_ppi": Dataset("cn_ppi", "cn_ppi", "cn_ppi", ("month",), "basic"),
    "cn_gdp": Dataset("cn_gdp", "cn_gdp", "cn_gdp", ("quarter",), "basic"),
    # 宏观发布日历 = "预期差"来源：value(实际)/fore_value(市场预期)/pre_value(上月实际)
    # eco_cal 单次最多 100 行（宽区间会被静默截断）→ macro_calendar 策略按自然月分块 + 数值解析
    "macro_calendar": Dataset(
        "macro_calendar",
        "eco_cal",
        "macro_calendar",
        ("date", "time", "country", "event"),
        "macro_calendar",
        {"country": "中国"},
    ),
    # 日频资金面/杠杆资金（按日期区间增量拉取）
    "shibor": Dataset("shibor", "shibor", "shibor", ("date",), "date_range"),
    "margin": Dataset("margin", "margin", "margin", ("trade_date", "exchange_id"), "date_range"),
    "moneyflow_hsgt": Dataset("moneyflow_hsgt", "moneyflow_hsgt", "moneyflow_hsgt", ("trade_date",), "date_range"),
    # ---- 外部条件变量（2026-09-16 新增，见 specs/macro-data.md §8）----
    # 外盘原油：tushare 无权限（index_global 只有股指、fut_basic(IPE/NYMEX) 为空），
    # 改走新浪全球期货日线（strategy=sina_oil，非 tushare 通道）→ 表 oil_global(symbol=BRENT/WTI)
    "oil_global": Dataset(
        "oil_global",
        "",
        "oil_global",
        ("symbol", "date"),
        "sina_oil",
    ),
    # 美国国债收益率曲线（美元定价锚，含 y10/y2）——文章"降息/加息通道"的直接可测项
    "us_tycr": Dataset("us_tycr", "us_tycr", "us_tycr", ("date",), "date_range"),
    # 离岸人民币汇率（USDCNH）——文章传导链"美债利率↓→人民币升值预期→北向回流"的中段变量
    "fx_daily": Dataset(
        "fx_daily",
        "fx_daily",
        "fx_daily",
        ("ts_code", "trade_date"),
        "date_range",
        {"ts_code": "USDCNH.FXCM"},
    ),
}

BOOTSTRAP_ORDER = ("trade_cal", "stock_basic")

DAILY_ORDER = (
    "trade_cal",
    "stock_basic",
    "daily",
    "daily_basic",
    "adj_factor",
    "moneyflow",
    # 龙虎榜：个股汇总（恢复调度）+ 席位明细（新增），盘后 20:10 拉取足够
    "top_list",
    "top_inst",
    "index_basic",
    "index_daily",
    "index_daily_basic",
    # moneyflow_ths / kpl_concept_cons：2000 积分档无权限（2026-08-29 实测），暂不纳入定时同步
    # "moneyflow_ths",
    # "kpl_concept_cons",
)

FINANCE_ORDER = ("fina_indicator", "income", "balancesheet", "cashflow")

# 宏观：月度序列（对应研报里常见的"社融/信贷/M2/CPI/PPI"核对）
MACRO_MONTHLY_ORDER = ("sf_month", "cn_m", "cn_cpi", "cn_ppi", "cn_gdp")
# 宏观：日频（发布日历 + 资金面/杠杆资金 + 外部条件变量），不依赖交易日历（宏观数据周末也会公布）
MACRO_DAILY_ORDER = ("macro_calendar", "shibor", "margin", "moneyflow_hsgt", "oil_global", "us_tycr", "fx_daily")
MACRO_ORDER = MACRO_MONTHLY_ORDER + MACRO_DAILY_ORDER

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
    if name == "macro":
        return [DATASETS[item] for item in MACRO_ORDER]
    if name == "macro_monthly":
        return [DATASETS[item] for item in MACRO_MONTHLY_ORDER]
    if name == "macro_daily":
        return [DATASETS[item] for item in MACRO_DAILY_ORDER]
    return [get_dataset(name)]
