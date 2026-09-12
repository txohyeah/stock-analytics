"""天天基金爬虫：基金前十大重仓股（fundf10 jjcc 接口）。

数据源：https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={fund_code}&topline=10&year={year}&month={month}
返回 JS 变量 apidata.content（HTML），含 4 个季度表格（最新季度在前）。

实测结论（2026-09-12）：
- year/month 参数不控制返回季度，总是返回最新披露季度及其前 3 个季度（共 4 个 table）
- 最新季度 table 返回全部持仓（50+ 行），历史季度只返回前十大（10 行）→ 统一取 rank<=10 保证口径一致
- 部分基金表格含"变动详情"列，解析按 td class 识别字段，不依赖列位置
- 股票代码链接前缀：1.=沪市(.SH)、0.=深市(.SZ)、116.=港股(跳过)
"""
from __future__ import annotations

import logging
import re

import pandas as pd
import requests

logger = logging.getLogger(__name__)

FUNDF10_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
HEADERS = {
    "Referer": "https://fundf10.eastmoney.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# 股票代码链接前缀 → 交易所后缀
_MARKET_SUFFIX = {"1": ".SH", "0": ".SZ"}

# 季度 → 报告期末日期
_QUARTER_END = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}


def _parse_report_period(text: str) -> str | None:
    """从页面文本提取报告期，如 '2025年4季度' → '20251231'。"""
    m = re.search(r"(\d{4})年(\d)季度", text)
    if not m:
        return None
    year = int(m.group(1))
    quarter = int(m.group(2))
    month = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}[quarter]
    return f"{year}{month}"


def _extract_table_rows(table_html: str) -> list[dict]:
    """解析一个季度表格（字段识别不依赖列位置/class，兼容多种表格变体）。

    实测两种表格结构：
      A. <td>1</td><td><a>600519</a></td><td class='tol'><a>贵州茅台</a></td>
         <td class='xglj'>股吧行情</td><td class='tor'>9.52%</td><td class='tor'>103.68</td><td class='tor'>142,785.75</td>
      B. 同上但数值列带 class='cgs'(持股数) / 'last ccs'(持仓市值)，且可能多一列"变动详情"

    识别策略：
      - 代码：链接文本为纯数字（r/{market}.{code}）
      - 名称：链接文本含非数字字符（排除"股吧/行情"资讯列）
      - 占净值比例：含 % 的数值列
      - 持股数/持仓市值：优先 class cgs/ccs，否则按剩余数值列顺序
    """
    rows: list[dict] = []
    trs = re.findall(r"<tr>(.*?)</tr>", table_html, re.S)
    for tr in trs:
        if "<th" in tr:
            continue  # 表头
        tds = re.findall(r"<td([^>]*)>(.*?)</td>", tr, re.S)
        if not tds:
            continue
        rank_text = re.sub(r"<[^>]+>", "", tds[0][1]).strip()
        if not rank_text.isdigit():
            continue
        rank = int(rank_text)

        ts_code = None
        name = ""
        ratio = None
        vol_by_class = amount_by_class = None
        numbers: list[float] = []
        for attr, inner in tds[1:]:  # tds[0] 是序号，跳过
            if "xglj" in attr:
                continue  # 相关资讯列（股吧/行情）
            text = re.sub(r"<[^>]+>", "", inner).replace(",", "").strip()
            # 代码/名称（td 内链接）
            m = re.search(r"r/(\d+)\.(\d+)", inner)
            if m:
                suffix = _MARKET_SUFFIX.get(m.group(1))
                if not suffix:
                    continue  # 港股/其他市场，A股分析跳过
                if text.isdigit() and ts_code is None:
                    ts_code = f"{text}{suffix}"
                elif not text.isdigit() and not name:
                    name = text
                continue
            if not text:
                continue
            if text.endswith("%"):
                if ratio is None:
                    ratio = float(text[:-1])
                continue
            try:
                num = float(text)
            except ValueError:
                continue
            if "cgs" in attr:
                vol_by_class = num
            elif "ccs" in attr:
                amount_by_class = num
            else:
                numbers.append(num)

        if not ts_code:
            continue  # 港股等，跳过
        hold_vol = vol_by_class if vol_by_class is not None else (numbers[0] if len(numbers) > 0 else None)
        hold_amount = amount_by_class if amount_by_class is not None else (numbers[1] if len(numbers) > 1 else None)
        rows.append(
            {
                "rank": rank,
                "ts_code": ts_code,
                "name": name,
                "hold_ratio": ratio,
                "hold_vol": hold_vol,
                "hold_amount": hold_amount,
            }
        )
    # 统一口径：只保留前十大重仓（最新季度接口返回全部持仓，历史季度只返回前十；
    # 取 rank<=10 保证所有季度口径一致，QoQ 对比才可靠）
    return [r for r in rows if r["rank"] <= 10]


def fetch_fund_holdings(fund_code: str, year: int, month: int) -> pd.DataFrame:
    """爬一只基金的前十大重仓股（含最近 4 个季度）。

    Returns:
        DataFrame[fund_code, ts_code, name, end_date, rank, hold_ratio, hold_vol, hold_amount]
        无数据时返回空 DataFrame。
    """
    params = {
        "type": "jjcc",
        "code": fund_code,
        "topline": 10,
        "year": year,
        "month": month,
    }
    resp = requests.get(FUNDF10_URL, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    text = resp.text

    m = re.search(r'content:"(.*?)",\s*arryear', text, re.S)
    if not m:
        return pd.DataFrame()
    content = m.group(1)

    # 按 h4 标题切分季度块（每个季度一个 table）
    # 结构：<h4>...2025年4季度股票投资明细...</h4>...<table>...</table>
    blocks = re.split(r"<h4[^>]*>", content)
    frames: list[pd.DataFrame] = []
    for block in blocks[1:]:
        period_m = re.search(r"(\d{4})年(\d)季度", block)
        if not period_m:
            continue
        year_n = int(period_m.group(1))
        quarter = int(period_m.group(2))
        end_date = f"{year_n}{_QUARTER_END[quarter]}"
        table_end = block.find("</table>")
        if table_end == -1:
            continue
        table_html = block[: table_end + len("</table>")]
        rows = _extract_table_rows(table_html)
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df["end_date"] = end_date
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, ignore_index=True)
    result["fund_code"] = fund_code
    return result