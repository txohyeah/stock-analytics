from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .repository import StockDataRepository
from .errors import DataInsufficientError, ReportWriteError, UserInputError


DEFAULT_INDEX_CODES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
}

FOCUS_GROUPS = {
    "半导体": {
        "688981.SH": "中芯国际",
        "002371.SZ": "北方华创",
        "688012.SH": "中微公司",
        "603501.SH": "韦尔股份",
        "002049.SZ": "紫光国微",
        "603986.SH": "兆易创新",
    },
    "银行": {
        "600036.SH": "招商银行",
        "601166.SH": "兴业银行",
        "600000.SH": "浦发银行",
        "601398.SH": "工商银行",
        "601288.SH": "农业银行",
        "601988.SH": "中国银行",
    },
}

WEIGHT_STOCKS = {
    "600519.SH": "贵州茅台",
    "300750.SZ": "宁德时代",
    "601398.SH": "工商银行",
    "688981.SH": "中芯国际",
}


@dataclass(frozen=True)
class MarketReviewResult:
    trade_date: str
    previous_trade_date: str | None
    market_count: int
    markdown: str


def run_market_review(
    repository: StockDataRepository,
    *,
    requested_date: str = "latest",
    output_path: str | Path | None = None,
) -> dict[str, object]:
    result = build_market_review(repository, requested_date=requested_date)
    if output_path:
        path = Path(output_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(result.markdown, encoding="utf-8")
        except OSError as exc:
            raise ReportWriteError(str(exc)) from exc
        return {
            "ok": True,
            "report_path": str(path.resolve()),
            "trade_date": result.trade_date,
            "previous_trade_date": result.previous_trade_date,
            "market_count": result.market_count,
        }

    print(result.markdown)
    return {
        "ok": True,
        "trade_date": result.trade_date,
        "previous_trade_date": result.previous_trade_date,
        "market_count": result.market_count,
    }


def build_market_review(repository: StockDataRepository, *, requested_date: str = "latest") -> MarketReviewResult:
    trade_date = repository.resolve_trade_date(_normalize_requested_date(requested_date))
    previous_trade_date = repository.resolve_previous_trade_date(trade_date)
    rows = repository.fetch_market_daily(trade_date)
    if not rows:
        raise DataInsufficientError(f"No market daily data found for {trade_date}")
    previous_rows = repository.fetch_market_daily(previous_trade_date) if previous_trade_date else []
    index_start = repository.resolve_start_date(trade_date, 80)
    index_rows = {
        code: repository.fetch_index_daily(code, index_start, trade_date)
        for code in DEFAULT_INDEX_CODES
    }
    markdown = render_market_review(
        trade_date=trade_date,
        rows=rows,
        previous_trade_date=previous_trade_date,
        previous_rows=previous_rows,
        index_rows=index_rows,
    )
    return MarketReviewResult(
        trade_date=trade_date,
        previous_trade_date=previous_trade_date,
        market_count=len(rows),
        markdown=markdown,
    )


def render_market_review(
    *,
    trade_date: str,
    rows: list[dict[str, Any]],
    previous_trade_date: str | None = None,
    previous_rows: list[dict[str, Any]] | None = None,
    index_rows: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    df = _market_frame(rows)
    if df.empty:
        raise DataInsufficientError(f"No market daily data found for {trade_date}")
    previous = _market_frame(previous_rows or [])
    index_rows = index_rows or {}

    stats = _market_stats(df)
    board_rows = _board_rows(df)
    industry_rows = _industry_rows(df)
    index_summary = _index_summary(index_rows)
    mood = _mood(stats)
    style = _market_style(stats)

    lines: list[str] = [
        f"### {trade_date} A股复盘",
        "",
        "#### 1. 核心数据统计",
        "",
        "| 指标 | 数据 | 备注 |",
        "| :--- | :--- | :--- |",
        f"| **涨跌家数** | 上涨 {stats['up_count']} / 下跌 {stats['down_count']} / 平盘 {stats['flat_count']} | {mood} |",
        f"| **中位数涨幅** | `{stats['median_pct']:+.2f}%` | 多数个股表现 |",
        f"| **平均涨幅** | `{stats['mean_pct']:+.2f}%` | 账户体感 |",
        f"| **涨停/跌停** | 涨停 {stats['limit_up']} / 跌停 {stats['limit_down']} | 极端情绪 |",
        f"| **≥5% 涨跌** | 涨幅≥5%: {stats['pct_5up']}家 / 跌幅≥5%: {stats['pct_5down']}家 | 赚钱效应 |",
        "",
        "#### 2. 指数趋势",
        "",
    ]
    lines.extend(_render_index_table(index_summary))
    lines.extend(
        [
            "",
            "#### 3. 宽基与市场分组",
            "",
            f"* **大盘情绪**：{_market_sentiment(stats)}",
        ]
    )
    if board_rows:
        strongest = max(board_rows, key=lambda row: row["median_pct"])
        weakest = min(board_rows, key=lambda row: row["median_pct"])
        lines.append(f"* **宽基强弱**：{strongest['name']} ({strongest['median_pct']:+.2f}%) 领跑，{weakest['name']} ({weakest['median_pct']:+.2f}%) 偏弱")
    lines.extend(["", "| 板块 | 中位数 | 平均 | 上涨率 |", "| :--- | :--- | :--- | :--- |"])
    for row in board_rows:
        marker = "✅" if row["median_pct"] > 0 else "❌"
        lines.append(f"| {row['name']} | `{row['median_pct']:+.2f}%` | `{row['mean_pct']:+.2f}%` | {row['up_ratio']:.0f}% {marker} |")

    lines.extend(["", "#### 4. 行业热度", ""])
    lines.extend(_render_industry_tables(industry_rows))
    lines.extend(["", "#### 5. 热门板块追踪", ""])
    lines.extend(_render_focus_groups(df))
    lines.extend(["", "#### 6. 核心权重股表现", ""])
    lines.extend(_render_named_stock_table(df, WEIGHT_STOCKS))
    lines.extend(["", "#### 7. 赚钱效应（昨日涨停股表现）", ""])
    lines.extend(_render_limit_up_premium(df, previous, previous_trade_date))
    lines.extend(["", "#### 8. 今日涨幅 TOP5", ""])
    lines.extend(_render_top_movers(df, ascending=False))
    lines.extend(["", "#### 9. 今日跌幅 TOP5", ""])
    lines.extend(_render_top_movers(df, ascending=True))
    lines.extend(
        [
            "",
            "#### 10. 简评",
            "",
            f"市场情绪{'偏暖' if stats['median_pct'] > 0 else '偏冷'}，{style}。",
            _risk_note(stats, index_summary),
            "",
        ]
    )
    return "\n".join(lines)


def kline_analysis(row: pd.Series) -> list[str]:
    body = float(row["close"]) - float(row["open"])
    upper_shadow = float(row["high"]) - max(float(row["open"]), float(row["close"]))
    lower_shadow = min(float(row["open"]), float(row["close"])) - float(row["low"])
    pre_close = float(row["pre_close"]) if pd.notna(row.get("pre_close")) else float(row["close"])

    signals: list[str] = []
    if float(row["close"]) < pre_close and float(row["close"]) > float(row["open"]):
        signals.append("假阳线")
    elif float(row["close"]) > pre_close and float(row["close"]) < float(row["open"]):
        signals.append("假阴线")
    if abs(body) < 0.005 * pre_close:
        signals.append("十字星")
    if upper_shadow > 2 * abs(body) and abs(body) > 0:
        signals.append("长上影")
    if lower_shadow > 2 * abs(body) and abs(body) > 0:
        signals.append("长下影")
    if upper_shadow < 0.001 * pre_close and lower_shadow < 0.001 * pre_close:
        signals.append("光头光脚")
    return signals


def _normalize_requested_date(value: str) -> str:
    if value == "latest":
        return value
    compact = str(value).replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise UserInputError("--date must be latest, YYYYMMDD, or YYYY-MM-DD")
    return compact


def _market_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for column in ["open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"]:
        if column in df:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["ts_code", "open", "high", "low", "close", "pct_chg"])


def _market_stats(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "total": int(len(df)),
        "up_count": int((df["pct_chg"] > 0).sum()),
        "down_count": int((df["pct_chg"] < 0).sum()),
        "flat_count": int((df["pct_chg"] == 0).sum()),
        "median_pct": float(df["pct_chg"].median()),
        "mean_pct": float(df["pct_chg"].mean()),
        "limit_up": int((df["pct_chg"] >= 9.9).sum()),
        "limit_down": int((df["pct_chg"] <= -9.9).sum()),
        "pct_5up": int((df["pct_chg"] >= 5).sum()),
        "pct_5down": int((df["pct_chg"] <= -5).sum()),
    }


def _mood(stats: dict[str, Any]) -> str:
    if stats["median_pct"] > 0 and stats["up_count"] > stats["down_count"]:
        return "📈 情绪高昂"
    if stats["median_pct"] < 0 and stats["down_count"] > stats["up_count"]:
        return "📉 情绪低迷"
    return "⚖️ 情绪分化"


def _market_sentiment(stats: dict[str, Any]) -> str:
    if stats["up_count"] > stats["down_count"]:
        return "普涨，赚钱效应较好"
    if stats["down_count"] > stats["up_count"]:
        return "普跌，亏钱效应明显"
    return "结构性分化，涨跌互现"


def _market_style(stats: dict[str, Any]) -> str:
    if stats["median_pct"] > 0 and stats["mean_pct"] > stats["median_pct"]:
        return "大盘股表现优于中小盘"
    if stats["median_pct"] > 0 and stats["mean_pct"] < stats["median_pct"]:
        return "中小盘活跃，题材股表现较好"
    return "大盘股护盘或分化明显，多数个股承压"


def _board_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    groups = {
        "沪市主板": df[df["ts_code"].str.endswith(".SH") & ~df["ts_code"].str.startswith("68")],
        "深市主板": df[df["ts_code"].str.endswith(".SZ") & ~df["ts_code"].str.startswith("30")],
        "创业板": df[df["ts_code"].str.startswith("30")],
        "科创板": df[df["ts_code"].str.startswith("68")],
        "北交所": df[df["ts_code"].str.endswith(".BJ")],
    }
    return [_group_stats(name, group) for name, group in groups.items() if not group.empty]


def _industry_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    if "industry" not in df:
        return []
    rows = [
        _group_stats(str(name), group)
        for name, group in df.dropna(subset=["industry"]).groupby("industry")
        if len(group) >= 5 and str(name).strip()
    ]
    return sorted(rows, key=lambda row: row["median_pct"], reverse=True)


def _group_stats(name: str, group: pd.DataFrame) -> dict[str, Any]:
    return {
        "name": name,
        "count": int(len(group)),
        "median_pct": float(group["pct_chg"].median()),
        "mean_pct": float(group["pct_chg"].mean()),
        "up_ratio": float((group["pct_chg"] > 0).mean() * 100),
        "limit_up": int((group["pct_chg"] >= 9.9).sum()),
    }


def _index_summary(index_rows: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    result = []
    for code, rows in index_rows.items():
        if not rows:
            continue
        df = pd.DataFrame(rows).sort_values("trade_date")
        for column in ["close", "pct_chg"]:
            df[column] = pd.to_numeric(df[column], errors="coerce")
        latest = df.iloc[-1]
        ma5 = df["close"].rolling(5).mean().iloc[-1] if len(df) >= 5 else None
        ma20 = df["close"].rolling(20).mean().iloc[-1] if len(df) >= 20 else None
        ma60 = df["close"].rolling(60).mean().iloc[-1] if len(df) >= 60 else None
        pct5 = _window_return(df["close"], 5)
        pct20 = _window_return(df["close"], 20)
        ma20_slope = _ma_slope(df["close"], 20, 5)
        close = float(latest["close"])
        result.append(
            {
                "code": code,
                "name": DEFAULT_INDEX_CODES.get(code, code),
                "close": close,
                "pct_chg": float(latest["pct_chg"]) if pd.notna(latest["pct_chg"]) else None,
                "pct5": pct5,
                "pct20": pct20,
                "above_ma20": bool(pd.notna(ma20) and close >= float(ma20)),
                "above_ma60": bool(pd.notna(ma60) and close >= float(ma60)),
                "ma5": float(ma5) if pd.notna(ma5) else None,
                "ma20": float(ma20) if pd.notna(ma20) else None,
                "ma60": float(ma60) if pd.notna(ma60) else None,
                "ma20_slope": ma20_slope,
            }
        )
    return result


def _window_return(series: pd.Series, days: int) -> float | None:
    if len(series) <= days:
        return None
    previous = series.iloc[-days - 1]
    latest = series.iloc[-1]
    if pd.isna(previous) or previous == 0 or pd.isna(latest):
        return None
    return float((latest - previous) / previous * 100)


def _ma_slope(series: pd.Series, period: int, lookback: int) -> float | None:
    ma = series.rolling(period).mean()
    if len(ma.dropna()) <= lookback:
        return None
    previous = ma.dropna().iloc[-lookback - 1]
    latest = ma.dropna().iloc[-1]
    if previous == 0:
        return None
    return float((latest - previous) / previous * 100)


def _render_index_table(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["暂无指数数据。"]
    lines = [
        "| 指数 | 当日涨跌 | 5日涨跌 | 20日涨跌 | MA20 | MA60 | 趋势 |",
        "| :--- | ---: | ---: | ---: | :---: | :---: | :--- |",
    ]
    for row in rows:
        trend = "偏强" if row["above_ma20"] and (row["ma20_slope"] is None or row["ma20_slope"] >= 0) else "偏弱" if not row["above_ma20"] else "震荡"
        lines.append(
            f"| {row['name']} | {_fmt_pct(row['pct_chg'])} | {_fmt_pct(row['pct5'])} | {_fmt_pct(row['pct20'])} | "
            f"{'上方' if row['above_ma20'] else '下方'} | {'上方' if row['above_ma60'] else '下方'} | {trend} |"
        )
    return lines


def _render_industry_tables(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["暂无行业数据。"]
    lines = ["**强势行业 TOP10**", "", "| 行业 | 样本数 | 中位数 | 平均 | 上涨率 | 涨停数 |", "| :--- | ---: | ---: | ---: | ---: | ---: |"]
    for row in rows[:10]:
        lines.append(f"| {row['name']} | {row['count']} | {row['median_pct']:+.2f}% | {row['mean_pct']:+.2f}% | {row['up_ratio']:.0f}% | {row['limit_up']} |")
    lines.extend(["", "**弱势行业 TOP10**", "", "| 行业 | 样本数 | 中位数 | 平均 | 上涨率 | 涨停数 |", "| :--- | ---: | ---: | ---: | ---: | ---: |"])
    for row in list(reversed(rows[-10:])):
        lines.append(f"| {row['name']} | {row['count']} | {row['median_pct']:+.2f}% | {row['mean_pct']:+.2f}% | {row['up_ratio']:.0f}% | {row['limit_up']} |")
    return lines


def _render_focus_groups(df: pd.DataFrame) -> list[str]:
    lines: list[str] = []
    for group_name, stocks in FOCUS_GROUPS.items():
        rows = df[df["ts_code"].isin(stocks)].copy()
        if rows.empty:
            lines.append(f"* {group_name}板块无数据")
            continue
        lines.append(f"**{group_name}板块中位数**：`{rows['pct_chg'].median():+.2f}%`")
        lines.extend(_render_named_stock_table(rows, stocks))
        lines.append("")
    return lines or ["暂无。"]


def _render_named_stock_table(df: pd.DataFrame, names: dict[str, str]) -> list[str]:
    rows = df[df["ts_code"].isin(names)].copy()
    if rows.empty:
        return ["暂无。"]
    rows["display_name"] = rows["ts_code"].map(names).fillna(rows.get("name"))
    rows["signals"] = rows.apply(kline_analysis, axis=1)
    lines = ["| 股票 | 代码 | 涨幅 | K线形态 |", "| :--- | :--- | ---: | :--- |"]
    for _, row in rows.sort_values("pct_chg", ascending=False).iterrows():
        signal = "、".join(row["signals"]) if row["signals"] else "-"
        lines.append(f"| {row['display_name']} | {row['ts_code']} | {row['pct_chg']:+.2f}% | {signal} |")
    return lines


def _render_limit_up_premium(df: pd.DataFrame, previous: pd.DataFrame, previous_trade_date: str | None) -> list[str]:
    if previous.empty or not previous_trade_date:
        return ["* 无法获取上一交易日数据。"]
    limit_up_codes = previous[previous["pct_chg"] >= 9.9]["ts_code"].tolist()
    if not limit_up_codes:
        return [f"* {previous_trade_date} 无涨停股。"]
    today = df[df["ts_code"].isin(limit_up_codes)]
    if today.empty:
        return ["* 昨日涨停股今日无数据。"]
    continue_up = int((today["pct_chg"] > 0).sum())
    avg_premium = float(today["pct_chg"].mean())
    continue_limit = int((today["pct_chg"] >= 9.9).sum())
    return [
        f"* **昨日涨停**：{len(limit_up_codes)} 只",
        f"* **今日上涨**：{continue_up} 只",
        f"* **平均溢价**：`{avg_premium:+.2f}%`",
        f"* **连板数**：{continue_limit} 只",
    ]


def _render_top_movers(df: pd.DataFrame, *, ascending: bool) -> list[str]:
    rows = df.sort_values("pct_chg", ascending=ascending).head(5)
    if rows.empty:
        return ["暂无。"]
    lines = ["| 股票 | 名称 | 涨跌幅 | K线形态 |", "| :--- | :--- | ---: | :--- |"]
    for _, row in rows.iterrows():
        signal = "、".join(kline_analysis(row)) or "-"
        lines.append(f"| {row['ts_code']} | {row.get('name') or '-'} | {row['pct_chg']:+.2f}% | {signal} |")
    return lines


def _risk_note(stats: dict[str, Any], index_rows: list[dict[str, Any]]) -> str:
    weak_indexes = [row["name"] for row in index_rows if not row["above_ma20"]]
    notes = []
    if stats["down_count"] > stats["up_count"] * 1.5:
        notes.append("下跌家数显著多于上涨家数，短线风险偏高")
    if stats["pct_5down"] > stats["pct_5up"]:
        notes.append("大跌股数量多于大涨股，亏钱效应需要警惕")
    if weak_indexes:
        notes.append(f"{'、'.join(weak_indexes[:3])} 位于 MA20 下方")
    return "风险提示：" + "；".join(notes) + "。" if notes else "风险提示：暂无明显系统性风险信号，但仍需结合成交量和消息面验证。"


def _fmt_pct(value: object) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value):+.2f}%"
