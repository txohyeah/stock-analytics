from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .repository import StockDataRepository, StockHistory
from .errors import DataInsufficientError, ReportWriteError, UserInputError
from .inputs import parse_positions_file


THEME_TRADE = "theme_trade"
CORE_GROWTH = "core_growth"
SUPPORTED_POSITION_TYPES = {THEME_TRADE, CORE_GROWTH}


@dataclass(frozen=True)
class AccountPositionReview:
    code: str
    ts_code: str
    name: str
    position_type: str
    position_size_pct: float | None
    cost_price: float | None
    close: float | None
    profit_pct: float | None
    buy_date: str | None
    lifecycle: str
    action: str
    reason: str
    thesis: str | None = None
    notes: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccountReview:
    trade_date: str
    positions: list[AccountPositionReview]
    invalid_codes: list[str]
    missing_data: list[str]
    total_position_pct: float
    theme_trade_pct: float
    core_growth_pct: float
    unknown_position_pct: float
    cash_pct: float | None
    account_mode: str
    warnings: list[str]
    markdown: str


def run_account_review(
    repository: StockDataRepository,
    *,
    positions_path: str | Path,
    requested_date: str = "latest",
    output_path: str | Path | None = None,
    lookback_days: int = 120,
) -> dict[str, object]:
    review = build_account_review(
        repository,
        positions_path=positions_path,
        requested_date=requested_date,
        lookback_days=lookback_days,
    )
    if output_path:
        path = Path(output_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(review.markdown, encoding="utf-8")
        except OSError as exc:
            raise ReportWriteError(str(exc)) from exc
        return {
            "ok": True,
            "report_path": str(path.resolve()),
            "trade_date": review.trade_date,
            "position_count": len(review.positions),
            "invalid_count": len(review.invalid_codes),
            "missing_data_count": len(review.missing_data),
            "account_mode": review.account_mode,
            "total_position_pct": review.total_position_pct,
            "theme_trade_pct": review.theme_trade_pct,
            "core_growth_pct": review.core_growth_pct,
            "warning_count": len(review.warnings),
        }

    print(review.markdown)
    return {
        "ok": True,
        "trade_date": review.trade_date,
        "position_count": len(review.positions),
        "invalid_count": len(review.invalid_codes),
        "missing_data_count": len(review.missing_data),
        "account_mode": review.account_mode,
    }


def build_account_review(
    repository: StockDataRepository,
    *,
    positions_path: str | Path,
    requested_date: str = "latest",
    lookback_days: int = 120,
) -> AccountReview:
    if lookback_days <= 0:
        raise UserInputError("--lookback-days must be greater than 0")
    codes, invalid_codes, contexts = parse_positions_file(positions_path)
    if not codes:
        raise UserInputError("No valid positions found")

    trade_date = repository.resolve_trade_date(_normalize_requested_date(requested_date))
    start_date = repository.resolve_start_date(trade_date, lookback_days)
    histories = repository.fetch_histories_batch([item.ts_code for item in codes], start_date, trade_date)

    reviews: list[AccountPositionReview] = []
    missing_data: list[str] = []
    for code in codes:
        history = histories.get(code.ts_code)
        if history is None or history.bars.empty:
            missing_data.append(code.ts_code)
            continue
        latest_bar_date = str(history.bars.iloc[-1].get("trade_date"))
        if latest_bar_date != trade_date:
            missing_data.append(code.ts_code)
            continue
        reviews.append(_review_position(code.ts_code, history, contexts.get(code.ts_code) or {}))

    if not reviews:
        raise DataInsufficientError("No position has enough data to review")

    total_position_pct = _sum_position_pct(reviews)
    theme_trade_pct = _sum_position_pct([item for item in reviews if item.position_type == THEME_TRADE])
    core_growth_pct = _sum_position_pct([item for item in reviews if item.position_type == CORE_GROWTH])
    unknown_position_pct = _sum_position_pct(
        [item for item in reviews if item.position_type not in SUPPORTED_POSITION_TYPES]
    )
    cash_pct = max(0.0, 100.0 - total_position_pct) if total_position_pct <= 100 else None
    warnings = _account_warnings(
        reviews,
        theme_trade_pct=theme_trade_pct,
        total_position_pct=total_position_pct,
        unknown_position_pct=unknown_position_pct,
        invalid_codes=invalid_codes,
        missing_data=missing_data,
    )
    account_mode = _account_mode(
        reviews,
        total_position_pct=total_position_pct,
        theme_trade_pct=theme_trade_pct,
        warnings=warnings,
    )
    markdown = render_account_review(
        trade_date=trade_date,
        positions=reviews,
        invalid_codes=invalid_codes,
        missing_data=missing_data,
        total_position_pct=total_position_pct,
        theme_trade_pct=theme_trade_pct,
        core_growth_pct=core_growth_pct,
        unknown_position_pct=unknown_position_pct,
        cash_pct=cash_pct,
        account_mode=account_mode,
        warnings=warnings,
    )
    return AccountReview(
        trade_date=trade_date,
        positions=reviews,
        invalid_codes=invalid_codes,
        missing_data=missing_data,
        total_position_pct=total_position_pct,
        theme_trade_pct=theme_trade_pct,
        core_growth_pct=core_growth_pct,
        unknown_position_pct=unknown_position_pct,
        cash_pct=cash_pct,
        account_mode=account_mode,
        warnings=warnings,
        markdown=markdown,
    )


def render_account_review(
    *,
    trade_date: str,
    positions: list[AccountPositionReview],
    invalid_codes: list[str],
    missing_data: list[str],
    total_position_pct: float,
    theme_trade_pct: float,
    core_growth_pct: float,
    unknown_position_pct: float,
    cash_pct: float | None,
    account_mode: str,
    warnings: list[str],
) -> str:
    lines = [
        f"# 账户管理复盘",
        "",
        "## 账户摘要",
        "",
        f"- 交易日：{trade_date}",
        f"- 当前账户模式：{account_mode}",
        f"- 总仓位：{_fmt_pct(total_position_pct)}",
        f"- 题材短博仓：{_fmt_pct(theme_trade_pct)}",
        f"- 核心趋势仓：{_fmt_pct(core_growth_pct)}",
        f"- 未分类仓：{_fmt_pct(unknown_position_pct)}",
        f"- 现金估算：{_fmt_pct(cash_pct) if cash_pct is not None else 'N/A（总仓位超过 100%）'}",
        "",
        "## 持仓分层",
        "",
    ]
    lines.extend(_render_positions_table(positions))
    lines.extend(["", "## 账户约束与提示", ""])
    lines.extend([f"- {warning}" for warning in warnings] or ["暂无。"])
    if invalid_codes or missing_data:
        lines.extend(["", "## 数据质量", ""])
        if invalid_codes:
            lines.append(f"- 无法识别代码：{', '.join(invalid_codes)}")
        if missing_data:
            lines.append(f"- 缺失或未同步行情：{', '.join(missing_data)}")
    lines.extend(
        [
            "",
            "## 规则说明",
            "",
            "- theme_trade：题材短博仓，单票原则上不超过 10%，卖出形态和弱反馈优先处理。",
            "- core_growth：业绩与行业上行仓，盈利低于 30% 时优先保护利润垫，盈利达到 30% 后转入核心持有。",
            "- 本报告只做账户管理复盘，不构成投资建议，也不执行自动交易。",
            "",
        ]
    )
    return "\n".join(lines)


def _review_position(ts_code: str, history: StockHistory, context: dict[str, Any]) -> AccountPositionReview:
    bars = history.bars.copy()
    latest = bars.iloc[-1]
    close = _float_or_none(latest.get("close"))
    pct_chg = _float_or_none(latest.get("pct_chg"))
    cost_price = _float_or_none(context.get("cost_price"))
    profit_pct = None if close is None or cost_price in (None, 0) else round((close / cost_price - 1) * 100, 2)
    position_type = _normalize_position_type(context.get("position_type"))
    position_size_pct = _parse_position_size_pct(context.get("position_size"))
    lifecycle = _lifecycle(position_type, profit_pct)
    warnings = _position_warnings(position_type, position_size_pct, profit_pct)
    action, reason = _position_action(
        position_type=position_type,
        lifecycle=lifecycle,
        profit_pct=profit_pct,
        pct_chg=pct_chg,
        bars=bars,
        warnings=warnings,
    )
    return AccountPositionReview(
        code=str(history.meta.get("symbol") or ts_code.split(".")[0]),
        ts_code=ts_code,
        name=str(history.meta.get("name") or ts_code),
        position_type=position_type,
        position_size_pct=position_size_pct,
        cost_price=cost_price,
        close=close,
        profit_pct=profit_pct,
        buy_date=_optional_text(context.get("buy_date")),
        lifecycle=lifecycle,
        action=action,
        reason=reason,
        thesis=_optional_text(context.get("thesis")),
        notes=_optional_text(context.get("notes")),
        warnings=tuple(warnings),
    )


def _position_action(
    *,
    position_type: str,
    lifecycle: str,
    profit_pct: float | None,
    pct_chg: float | None,
    bars: pd.DataFrame,
    warnings: list[str],
) -> tuple[str, str]:
    sell_shape = _has_short_sell_shape(bars)
    structure_break = _has_structure_break(bars)
    extreme_selloff = _has_extreme_selloff(bars)

    if position_type == THEME_TRADE:
        if sell_shape or structure_break:
            return "优先处理", "题材短博仓对卖出形态敏感，出现弱反馈或短线结构走坏。"
        if warnings:
            return "控制仓位", "题材短博仓触发账户约束，避免继续扩大同类风险。"
        return "持有观察", "题材短博仓未触发硬性处理条件，继续按短线反馈管理。"

    if position_type == CORE_GROWTH:
        if lifecycle == "core_holding":
            if structure_break or extreme_selloff:
                return "风险处理", "核心趋势仓已有利润垫，但出现结构破坏或极端杀跌，需要降风险。"
            return "核心持有", "盈利已达到 30% 利润垫，普通短线波动不改变长线持有逻辑。"
        if profit_pct is not None and profit_pct < 0 and (sell_shape or structure_break):
            return "验证失败", "核心票入场阶段尚无利润垫，且短线反馈走弱。"
        if lifecycle == "cushion_building" and sell_shape:
            return "保护利润垫", "盈利尚未达到 30%，出现弱反馈时优先保护已有利润。"
        return "持有观察", "核心票仍处于验证或利润垫阶段，未触发结构性退出。"

    if structure_break or extreme_selloff:
        return "风险处理", "未分类持仓出现结构风险，需人工确认持仓逻辑。"
    return "人工确认", "持仓类型未识别，系统不套用题材或核心仓规则。"


def _lifecycle(position_type: str, profit_pct: float | None) -> str:
    if position_type == THEME_TRADE:
        return "short_trade"
    if position_type != CORE_GROWTH:
        return "unclassified"
    if profit_pct is None:
        return "starter"
    if profit_pct < 0:
        return "risk_watch"
    if profit_pct < 10:
        return "starter"
    if profit_pct < 30:
        return "cushion_building"
    return "core_holding"


def _position_warnings(position_type: str, position_size_pct: float | None, profit_pct: float | None) -> list[str]:
    warnings: list[str] = []
    if position_type == THEME_TRADE and position_size_pct is not None and position_size_pct > 10:
        warnings.append("题材短博单票仓位超过 10%")
    if position_type == CORE_GROWTH and profit_pct is not None and profit_pct >= 30:
        warnings.append("核心趋势仓已达到 30% 利润垫")
    if position_type not in SUPPORTED_POSITION_TYPES:
        warnings.append("持仓类型未识别")
    return warnings


def _account_warnings(
    positions: list[AccountPositionReview],
    *,
    theme_trade_pct: float,
    total_position_pct: float,
    unknown_position_pct: float,
    invalid_codes: list[str],
    missing_data: list[str],
) -> list[str]:
    warnings: list[str] = []
    if theme_trade_pct > 30:
        warnings.append("题材短博仓合计超过 30%，短线情绪相关性偏高。")
    elif theme_trade_pct > 20:
        warnings.append("题材短博仓合计超过 20%，新增短博仓需要更严格。")
    if total_position_pct > 85:
        warnings.append("总仓位超过 85%，账户防守弹性偏低。")
    if unknown_position_pct > 0:
        warnings.append("存在未分类持仓，建议补充 position_type。")
    if invalid_codes:
        warnings.append("存在无法识别的持仓代码，账户统计可能不完整。")
    if missing_data:
        warnings.append("存在缺失行情的持仓，账户统计可能不完整。")
    if any(item.action in {"优先处理", "风险处理", "验证失败"} for item in positions):
        warnings.append("存在需要优先处理的持仓，先降风险再考虑新增。")
    return warnings


def _account_mode(
    positions: list[AccountPositionReview],
    *,
    total_position_pct: float,
    theme_trade_pct: float,
    warnings: list[str],
) -> str:
    urgent_actions = {item.action for item in positions if item.action in {"优先处理", "风险处理", "验证失败"}}
    if urgent_actions or theme_trade_pct > 30:
        return "收缩"
    if total_position_pct > 85 or len(warnings) >= 2:
        return "平衡偏防守"
    if total_position_pct < 50 and theme_trade_pct <= 10:
        return "可进攻"
    return "平衡"


def _render_positions_table(items: list[AccountPositionReview]) -> list[str]:
    if not items:
        return ["暂无。"]
    lines = [
        "| 股票 | 代码 | 类型 | 仓位 | 成本 | 现价 | 盈亏 | 生命周期 | 动作 | 原因 |",
        "| :--- | :--- | :--- | ---: | ---: | ---: | ---: | :--- | :--- | :--- |",
    ]
    for item in sorted(items, key=lambda value: (value.position_type, value.code)):
        lines.append(
            f"| {item.name} | {item.code} | {item.position_type} | {_fmt_pct(item.position_size_pct)} | "
            f"{_fmt(item.cost_price)} | {_fmt(item.close)} | {_fmt_pct(item.profit_pct)} | "
            f"{item.lifecycle} | {item.action} | {item.reason} |"
        )
    return lines


def _has_short_sell_shape(bars: pd.DataFrame) -> bool:
    if bars.empty:
        return False
    latest = bars.iloc[-1]
    close = _float_or_none(latest.get("close"))
    high = _float_or_none(latest.get("high"))
    low = _float_or_none(latest.get("low"))
    pct_chg = _float_or_none(latest.get("pct_chg"))
    if close is None or high is None or low is None:
        return False
    day_range = high - low
    close_position = 1.0 if day_range <= 0 else (close - low) / day_range
    return bool((pct_chg is not None and pct_chg <= -3) or close_position <= 0.25)


def _has_structure_break(bars: pd.DataFrame) -> bool:
    if len(bars) < 20:
        return False
    closes = pd.to_numeric(bars["close"], errors="coerce")
    latest_close = closes.iloc[-1]
    ma20 = closes.rolling(20).mean().iloc[-1]
    prior_low = closes.iloc[-20:-1].min()
    if pd.isna(latest_close) or pd.isna(ma20) or pd.isna(prior_low):
        return False
    return bool(latest_close < ma20 and latest_close < prior_low)


def _has_extreme_selloff(bars: pd.DataFrame) -> bool:
    if bars.empty:
        return False
    latest = bars.iloc[-1]
    pct_chg = _float_or_none(latest.get("pct_chg"))
    close = _float_or_none(latest.get("close"))
    high = _float_or_none(latest.get("high"))
    low = _float_or_none(latest.get("low"))
    if pct_chg is None or close is None or high is None or low is None:
        return False
    day_range = high - low
    close_position = 1.0 if day_range <= 0 else (close - low) / day_range
    return bool(pct_chg <= -5 and close_position <= 0.35)


def _sum_position_pct(items: list[AccountPositionReview]) -> float:
    return round(sum(item.position_size_pct or 0 for item in items), 2)


def _parse_position_size_pct(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        parsed = float(text)
    except ValueError:
        return None
    if parsed < 0:
        return None
    return parsed * 100 if 0 < parsed <= 1 else parsed


def _normalize_position_type(value: object) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "theme": THEME_TRADE,
        "short": THEME_TRADE,
        "short_trade": THEME_TRADE,
        "topic": THEME_TRADE,
        "core": CORE_GROWTH,
        "growth": CORE_GROWTH,
        "long": CORE_GROWTH,
        "long_term": CORE_GROWTH,
    }
    return aliases.get(text, text or "unknown")


def _normalize_requested_date(value: str) -> str:
    if value == "latest":
        return value
    compact = str(value).replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise UserInputError("--date must be latest, YYYYMMDD, or YYYY-MM-DD")
    return compact


def _float_or_none(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _fmt(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _fmt_pct(value: object) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.2f}%"
