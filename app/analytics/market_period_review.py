from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any

from .repository import StockDataRepository
from .errors import DataInsufficientError, ReportWriteError, UserInputError
from .market_review import DEFAULT_INDEX_CODES


MAX_PERIOD_TRADE_DAYS = 250


@dataclass(frozen=True)
class MarketPeriodReviewResult:
    period_type: str
    start_date: str
    end_date: str
    trade_days: int
    market_count: int
    markdown: str


def run_market_period_review(
    repository: StockDataRepository,
    *,
    period: str | None = None,
    requested_date: str = "latest",
    start: str | None = None,
    end: str | None = None,
    output_path: str | Path | None = None,
) -> dict[str, object]:
    result = build_market_period_review(
        repository,
        period=period,
        requested_date=requested_date,
        start=start,
        end=end,
    )
    payload: dict[str, object] = {
        "ok": True,
        "period_type": result.period_type,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "trade_days": result.trade_days,
        "market_count": result.market_count,
    }
    if output_path:
        path = Path(output_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(result.markdown, encoding="utf-8")
        except OSError as exc:
            raise ReportWriteError(str(exc)) from exc
        payload["report_path"] = str(path.resolve())
    else:
        print(result.markdown)
    return payload


def build_market_period_review(
    repository: StockDataRepository,
    *,
    period: str | None = None,
    requested_date: str = "latest",
    start: str | None = None,
    end: str | None = None,
) -> MarketPeriodReviewResult:
    period_type, natural_start, natural_end = _resolve_natural_period(
        repository,
        period=period,
        requested_date=requested_date,
        start=start,
        end=end,
    )
    trade_dates = repository.list_open_trade_dates(natural_start, natural_end)
    if not trade_dates:
        raise DataInsufficientError(f"No open trade date found between {natural_start} and {natural_end}")
    if len(trade_dates) > MAX_PERIOD_TRADE_DAYS:
        raise UserInputError(f"Period contains {len(trade_dates)} trade days; maximum is {MAX_PERIOD_TRADE_DAYS}")

    start_date, end_date = trade_dates[0], trade_dates[-1]
    period_rows = repository.fetch_market_period_returns(start_date, end_date)
    if not period_rows:
        raise DataInsufficientError(f"No comparable market data found between {start_date} and {end_date}")

    daily_stats: list[dict[str, Any]] = []
    industry_positive_days: dict[str, int] = defaultdict(int)
    industry_observed_days: dict[str, int] = defaultdict(int)
    for trade_date in trade_dates:
        rows = repository.fetch_market_daily(trade_date)
        if not rows:
            continue
        daily_stats.append(_daily_market_stats(trade_date, rows))
        _accumulate_industry_days(rows, industry_positive_days, industry_observed_days)

    index_rows = {
        code: repository.fetch_index_daily(code, start_date, end_date)
        for code in DEFAULT_INDEX_CODES
    }
    markdown = render_market_period_review(
        period_type=period_type,
        natural_start=natural_start,
        natural_end=natural_end,
        trade_dates=trade_dates,
        period_rows=period_rows,
        daily_stats=daily_stats,
        industry_positive_days=industry_positive_days,
        industry_observed_days=industry_observed_days,
        index_rows=index_rows,
    )
    return MarketPeriodReviewResult(
        period_type=period_type,
        start_date=start_date,
        end_date=end_date,
        trade_days=len(trade_dates),
        market_count=len(period_rows),
        markdown=markdown,
    )


def render_market_period_review(
    *,
    period_type: str,
    natural_start: str,
    natural_end: str,
    trade_dates: list[str],
    period_rows: list[dict[str, Any]],
    daily_stats: list[dict[str, Any]],
    industry_positive_days: dict[str, int],
    industry_observed_days: dict[str, int],
    index_rows: dict[str, list[dict[str, Any]]],
) -> str:
    start_date, end_date = trade_dates[0], trade_dates[-1]
    returns = [_number(row.get("period_return")) for row in period_rows]
    valid_returns = [value for value in returns if value is not None]
    up_count = sum(value > 0 for value in valid_returns)
    period_label = {"week": "周度", "month": "月度", "custom": "自定义周期"}[period_type]
    lines = [
        f"### {start_date}—{end_date} A股{period_label}复盘",
        "",
        "#### 1. 周期概览",
        "",
        f"* **自然日期范围**：{natural_start}—{natural_end}",
        f"* **实际交易日期范围**：{start_date}—{end_date}",
        f"* **交易日数量**：{len(trade_dates)}",
        f"* **可比股票数量**：{len(valid_returns)}",
        f"* **周期上涨股票占比**：{_pct(up_count / len(valid_returns) * 100 if valid_returns else None)}",
        f"* **个股周期收益中位数**：{_pct(median(valid_returns) if valid_returns else None)}",
        "",
        "#### 2. 主要指数表现",
        "",
    ]
    lines.extend(_render_indexes(index_rows))
    lines.extend(["", "#### 3. 市场宽度与情绪变化", ""])
    lines.extend(_render_daily_stats(daily_stats))
    lines.extend(["", "#### 4. 市场分组表现", ""])
    lines.extend(_render_group_table(period_rows))
    lines.extend(["", "#### 5. 行业强弱与持续性", ""])
    lines.extend(_render_industries(period_rows, industry_positive_days, industry_observed_days))
    lines.extend(["", "#### 6. 个股周期涨跌幅 TOP10", ""])
    lines.extend(_render_stock_movers(period_rows))
    lines.extend(["", "#### 7. 数据质量", ""])
    lines.extend(_render_quality(trade_dates, daily_stats, period_rows))
    lines.extend(["", "#### 8. 简评", "", _period_comment(valid_returns, daily_stats, index_rows), ""])
    return "\n".join(lines)


def _resolve_natural_period(
    repository: StockDataRepository,
    *,
    period: str | None,
    requested_date: str,
    start: str | None,
    end: str | None,
) -> tuple[str, str, str]:
    if period:
        if start or end:
            raise UserInputError("--period cannot be combined with --start or --end")
        normalized_date = _normalize_date(requested_date)
        anchor = _parse_date(repository.resolve_trade_date("latest")) if normalized_date == "latest" else _parse_date(normalized_date)
        if period == "week":
            natural_start = anchor - timedelta(days=anchor.weekday())
            natural_end = natural_start + timedelta(days=6)
        elif period == "month":
            natural_start = anchor.replace(day=1)
            if anchor.month == 12:
                natural_end = anchor.replace(year=anchor.year + 1, month=1, day=1) - timedelta(days=1)
            else:
                natural_end = anchor.replace(month=anchor.month + 1, day=1) - timedelta(days=1)
        else:
            raise UserInputError("--period must be week or month")
        latest = _parse_date(repository.resolve_trade_date("latest"))
        if natural_start > latest:
            raise DataInsufficientError(f"No market data is available for the requested {period}")
        return period, _compact(natural_start), _compact(min(natural_end, latest))
    if not start or not end:
        raise UserInputError("Provide --period or both --start and --end")
    start_date, end_date = _parse_date(_normalize_date(start)), _parse_date(_normalize_date(end))
    if start_date > end_date:
        raise UserInputError("--start must not be after --end")
    return "custom", _compact(start_date), _compact(end_date)


def _daily_market_stats(trade_date: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [_number(row.get("pct_chg")) for row in rows]
    values = [value for value in values if value is not None]
    return {
        "trade_date": trade_date,
        "count": len(values),
        "up_count": sum(value > 0 for value in values),
        "down_count": sum(value < 0 for value in values),
        "flat_count": sum(value == 0 for value in values),
        "limit_up": sum(value >= 9.9 for value in values),
        "limit_down": sum(value <= -9.9 for value in values),
        "median_pct": median(values) if values else None,
    }


def _accumulate_industry_days(
    rows: list[dict[str, Any]],
    positive_days: dict[str, int],
    observed_days: dict[str, int],
) -> None:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        industry = str(row.get("industry") or "").strip()
        value = _number(row.get("pct_chg"))
        if industry and value is not None:
            grouped[industry].append(value)
    for industry, values in grouped.items():
        if len(values) < 5:
            continue
        observed_days[industry] += 1
        if median(values) > 0:
            positive_days[industry] += 1


def _render_indexes(index_rows: dict[str, list[dict[str, Any]]]) -> list[str]:
    lines = ["| 指数 | 周期涨跌 | 最大回撤 | 上涨日占比 |", "| :--- | ---: | ---: | ---: |"]
    found = False
    for code, rows in index_rows.items():
        values = sorted(rows, key=lambda row: str(row.get("trade_date") or ""))
        if not values:
            continue
        start_pre_close = _number(values[0].get("pre_close"))
        closes = [_number(row.get("close")) for row in values]
        closes = [value for value in closes if value is not None]
        daily = [_number(row.get("pct_chg")) for row in values]
        daily = [value for value in daily if value is not None]
        period_return = (closes[-1] / start_pre_close - 1) * 100 if closes and start_pre_close else None
        lines.append(
            f"| {DEFAULT_INDEX_CODES.get(code, code)} | {_pct(period_return)} | "
            f"{_pct(_max_drawdown(([start_pre_close] if start_pre_close else []) + closes))} | "
            f"{_pct(sum(value > 0 for value in daily) / len(daily) * 100 if daily else None)} |"
        )
        found = True
    return lines if found else ["暂无指数数据。"]


def _render_daily_stats(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["暂无每日市场数据。"]
    lines = [
        "| 日期 | 上涨 | 下跌 | 平盘 | 涨停 | 跌停 | 中位数涨幅 |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['trade_date']} | {row['up_count']} | {row['down_count']} | {row['flat_count']} | "
            f"{row['limit_up']} | {row['limit_down']} | {_pct(row['median_pct'])} |"
        )
    half = max(1, len(rows) // 2)
    first = mean(row["up_count"] for row in rows[:half])
    second = mean(row["up_count"] for row in rows[half:]) if rows[half:] else first
    lines.extend(["", f"* 前半周期日均上涨家数 `{first:.0f}`，后半周期 `{second:.0f}`。"])
    return lines


def _render_group_table(rows: list[dict[str, Any]]) -> list[str]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = _number(row.get("period_return"))
        if value is not None:
            grouped[_board_name(str(row.get("ts_code") or ""))].append(value)
    lines = ["| 市场分组 | 样本数 | 周期收益中位数 | 上涨占比 |", "| :--- | ---: | ---: | ---: |"]
    for name, values in grouped.items():
        lines.append(f"| {name} | {len(values)} | {_pct(median(values))} | {_pct(sum(v > 0 for v in values) / len(values) * 100)} |")
    return lines


def _render_industries(
    rows: list[dict[str, Any]],
    positive_days: dict[str, int],
    observed_days: dict[str, int],
) -> list[str]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        industry = str(row.get("industry") or "").strip()
        value = _number(row.get("period_return"))
        if industry and value is not None:
            grouped[industry].append(value)
    stats = [
        (name, values)
        for name, values in grouped.items()
        if len(values) >= 5
    ]
    stats.sort(key=lambda item: median(item[1]), reverse=True)
    if not stats:
        return ["暂无满足最少 5 只样本要求的行业数据。"]
    lines = ["| 行业 | 样本数 | 周期收益中位数 | 上涨占比 | 日度走强占比 |", "| :--- | ---: | ---: | ---: | ---: |"]
    selected = stats[:10]
    selected_names = {name for name, _ in selected}
    selected.extend((name, values) for name, values in reversed(stats[-10:]) if name not in selected_names)
    for name, values in selected:
        observed = observed_days.get(name, 0)
        persistence = positive_days.get(name, 0) / observed * 100 if observed else None
        lines.append(
            f"| {name} | {len(values)} | {_pct(median(values))} | "
            f"{_pct(sum(value > 0 for value in values) / len(values) * 100)} | {_pct(persistence)} |"
        )
    return lines


def _render_stock_movers(rows: list[dict[str, Any]]) -> list[str]:
    valid = [row for row in rows if _number(row.get("period_return")) is not None]
    valid.sort(key=lambda row: float(row["period_return"]), reverse=True)
    lines = ["**涨幅 TOP10**", "", "| 股票 | 名称 | 行业 | 周期涨跌 |", "| :--- | :--- | :--- | ---: |"]
    for row in valid[:10]:
        lines.append(f"| {row['ts_code']} | {row.get('name') or '-'} | {row.get('industry') or '-'} | {_pct(row['period_return'])} |")
    lines.extend(["", "**跌幅 TOP10**", "", "| 股票 | 名称 | 行业 | 周期涨跌 |", "| :--- | :--- | :--- | ---: |"])
    for row in reversed(valid[-10:]):
        lines.append(f"| {row['ts_code']} | {row.get('name') or '-'} | {row.get('industry') or '-'} | {_pct(row['period_return'])} |")
    return lines


def _render_quality(trade_dates: list[str], daily_stats: list[dict[str, Any]], period_rows: list[dict[str, Any]]) -> list[str]:
    missing_days = len(trade_dates) - len(daily_stats)
    missing_industry = sum(not str(row.get("industry") or "").strip() for row in period_rows)
    return [
        f"* 缺少全市场快照的交易日：{missing_days}",
        f"* 可比股票中缺少行业标签：{missing_industry}",
        "* 个股周期排行仅包含期初与期末均有行情、且期初前收盘价有效的股票。",
        "* 行业表现由个股收益聚合派生，不代表行业指数收益。",
    ]


def _period_comment(
    returns: list[float],
    daily_stats: list[dict[str, Any]],
    index_rows: dict[str, list[dict[str, Any]]],
) -> str:
    notes: list[str] = []
    if returns:
        notes.append("多数可比个股周期上涨" if median(returns) > 0 else "多数可比个股周期承压")
    if len(daily_stats) >= 2:
        half = max(1, len(daily_stats) // 2)
        first = mean(row["up_count"] for row in daily_stats[:half])
        second = mean(row["up_count"] for row in daily_stats[half:]) if daily_stats[half:] else first
        notes.append("后半周期市场宽度改善" if second > first else "后半周期市场宽度走弱")
    positive_indexes = 0
    observed_indexes = 0
    for rows in index_rows.values():
        values = sorted(rows, key=lambda row: str(row.get("trade_date") or ""))
        if values:
            start = _number(values[0].get("pre_close"))
            close = _number(values[-1].get("close"))
            if start and close is not None:
                observed_indexes += 1
                positive_indexes += close > start
    if observed_indexes:
        notes.append("主要指数整体偏强" if positive_indexes > observed_indexes / 2 else "主要指数整体偏弱")
    return "；".join(notes) + "。仅基于确定性行情指标，不解释涨跌原因。" if notes else "暂无足够数据形成简评。"


def _board_name(ts_code: str) -> str:
    if ts_code.endswith(".BJ"):
        return "北交所"
    if ts_code.startswith("68"):
        return "科创板"
    if ts_code.startswith("30"):
        return "创业板"
    if ts_code.endswith(".SH"):
        return "沪市主板"
    return "深市主板"


def _max_drawdown(closes: list[float]) -> float | None:
    if not closes:
        return None
    peak = closes[0]
    drawdown = 0.0
    for close in closes:
        peak = max(peak, close)
        drawdown = min(drawdown, (close / peak - 1) * 100)
    return drawdown


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _pct(value: object) -> str:
    number = _number(value)
    return "N/A" if number is None else f"{number:+.2f}%"


def _normalize_date(value: str) -> str:
    text = str(value).strip()
    if text == "latest":
        return text
    compact = text.replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise UserInputError("Date must use latest, YYYYMMDD, or YYYY-MM-DD format")
    try:
        datetime.strptime(compact, "%Y%m%d")
    except ValueError as exc:
        raise UserInputError(f"Invalid date: {value}") from exc
    return compact


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _compact(value: date) -> str:
    return value.strftime("%Y%m%d")
