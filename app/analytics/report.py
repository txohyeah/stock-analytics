from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ReportWriteError
from tech_indicators.models import StrategyEvaluation
from tech_indicators.strategies import Strategy


def write_markdown_report(
    output_path: str | Path,
    *,
    strategy: Strategy,
    trade_date: str,
    input_count: int,
    invalid_codes: list[str],
    evaluations: list[StrategyEvaluation],
    missing_data: list[str],
    research_contexts: dict[str, dict[str, Any]] | None = None,
    research_warnings: list[str] | None = None,
) -> None:
    path = Path(output_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_markdown_report(
                strategy=strategy,
                trade_date=trade_date,
                input_count=input_count,
                invalid_codes=invalid_codes,
                evaluations=evaluations,
                missing_data=missing_data,
                research_contexts=research_contexts,
                research_warnings=research_warnings,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        raise ReportWriteError(str(exc)) from exc


def render_markdown_report(
    *,
    strategy: Strategy,
    trade_date: str,
    input_count: int,
    invalid_codes: list[str],
    evaluations: list[StrategyEvaluation],
    missing_data: list[str],
    research_contexts: dict[str, dict[str, Any]] | None = None,
    research_warnings: list[str] | None = None,
) -> str:
    if strategy.category == "position_rating":
        return _render_position_rating_report(
            strategy=strategy,
            trade_date=trade_date,
            input_count=input_count,
            invalid_codes=invalid_codes,
            evaluations=evaluations,
            missing_data=missing_data,
            research_contexts=research_contexts,
            research_warnings=research_warnings,
        )

    selected = [item for item in evaluations if item.bucket == "selected"]
    watch = [item for item in evaluations if item.bucket == "watch"]
    excluded = [item for item in evaluations if item.bucket == "excluded"]
    warnings = sorted({warning for item in evaluations for warning in item.warnings})

    lines: list[str] = [
        f"# {strategy.display_name}{'报告' if strategy.category == 'position_rating' else '选股报告'}",
        "",
        "## 运行摘要",
        "",
        f"- 交易日：{trade_date}",
        f"- 策略：{strategy.display_name} (`{strategy.name}`)",
        f"- 输入股票数：{input_count}",
        f"- 入选：{len(selected)}",
        f"- 观察：{len(watch)}",
        f"- 剔除：{len(excluded)}",
        f"- 数据缺失：{len(missing_data)}",
        "",
        "## 入选列表",
        "",
    ]
    lines.extend(_render_eval_table(selected))
    lines.extend(["", "## 观察列表", ""])
    lines.extend(_render_eval_table(watch))
    lines.extend(["", "## 剔除列表", ""])
    lines.extend(_render_excluded_table(excluded))
    lines.extend(["", "## 核心规则检查", ""])
    lines.extend(_render_core_rules(evaluations))
    lines.extend(["", "## 数据质量警告", ""])
    lines.extend(_render_warnings(warnings, invalid_codes, missing_data))
    if research_contexts is not None:
        lines.extend(["", "## 事件与研究面", ""])
        lines.extend(_render_research_contexts([*selected, *watch], research_contexts, research_warnings or []))
    lines.extend(
        [
            "",
            "## 后续可由 Agent 补充分析的问题",
            "",
            "- 请检查入选股票近 7 日是否存在重大利空公告。",
            "- 请补充入选股票所属板块近期催化和资金流变化。",
            "- 请结合当日大盘环境判断仓位上限。",
            "",
            "## 风险提示",
            "",
            "本报告仅为基于既定策略和历史数据的筛选结果，不构成投资建议。市场有风险，交易需独立判断并控制仓位。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_position_rating_report(
    *,
    strategy: Strategy,
    trade_date: str,
    input_count: int,
    invalid_codes: list[str],
    evaluations: list[StrategyEvaluation],
    missing_data: list[str],
    research_contexts: dict[str, dict[str, Any]] | None = None,
    research_warnings: list[str] | None = None,
) -> str:
    warnings = sorted({warning for item in evaluations for warning in item.warnings})
    lines = [
        f"# {strategy.display_name}报告",
        "",
        "## 运行摘要",
        "",
        f"- 交易日：{trade_date}",
        f"- 策略：{strategy.display_name} (`{strategy.name}`)",
        f"- 输入股票数：{input_count}",
        f"- 持股评级数：{len(evaluations)}",
        f"- 数据缺失：{len(missing_data)}",
        "",
        "## 持股列表",
        "",
    ]
    lines.extend(_render_position_rating_table(evaluations))
    lines.extend(["", "## 辅助信号", ""])
    lines.extend(_render_position_auxiliary_signals(evaluations))
    lines.extend(["", "## 数据异常", ""])
    lines.extend(_render_warnings(warnings, invalid_codes, missing_data))
    if research_contexts is not None:
        lines.extend(["", "## 事件与研究面", ""])
        lines.extend(_render_research_contexts(evaluations, research_contexts, research_warnings or []))
    lines.extend(
        [
            "",
            "## 风险提示",
            "",
            "本报告仅为基于既定策略和历史数据的持仓评级结果，不构成投资建议。市场有风险，交易需独立判断并控制仓位。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_position_rating_table(items: list[StrategyEvaluation]) -> list[str]:
    if not items:
        return ["暂无。"]
    lines = [
        "| 股票 | 代码 | 现价 | 涨跌幅 | 成本价 | 盈亏 | 上轨线（来源） | 生命线 | 趋势确认线 | 下轨线（来源） | 通道状态/强度 | 加减仓信号 | 信号强度（基础/修正） | 信号依据 | 风险/失效条件 |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for item in sorted(items, key=lambda x: x.code):
        rating = (item.indicators or {}).get("golden_bull_position_rating")
        if not isinstance(rating, dict):
            rating = {}
        channel_lines = rating.get("channel_lines") if isinstance(rating.get("channel_lines"), dict) else {}
        indicator_lines = rating.get("indicator_lines") if isinstance(rating.get("indicator_lines"), dict) else {}
        metrics = rating.get("metrics") if isinstance(rating.get("metrics"), dict) else {}
        ratings = [entry for entry in rating.get("ratings", []) if isinstance(entry, dict)]
        actions = " / ".join(str(entry.get("action")) for entry in ratings) or "数据不足"
        strengths = " / ".join(_render_rating_strength(entry) for entry in ratings) or "N/A"
        reasons = "；".join(str(entry.get("reason")) for entry in ratings if entry.get("reason")) or str(rating.get("reason") or "无")
        risks = [
            *[str(value) for value in rating.get("risk_flags", []) if value],
            *[str(value) for value in rating.get("invalidations", []) if value],
        ]
        lines.append(
            f"| {item.name} | {item.code} | {_fmt(item.close)} | {_fmt_pct(item.pct_chg)} | "
            f"{_fmt(rating.get('cost_price'))} | {_fmt_pct(rating.get('profit_pct'))} | "
            f"{_fmt(channel_lines.get('upper', metrics.get('upper_line')))}（{_golden_line_name(channel_lines.get('upper_source'))}） | "
            f"{_fmt(indicator_lines.get('life_line', channel_lines.get('life_line', metrics.get('bull_bear_boundary'))))} | "
            f"{_fmt(indicator_lines.get('trend_confirmation_line', channel_lines.get('trend_confirmation_line')))} | "
            f"{_fmt(channel_lines.get('lower', metrics.get('lower_line')))}（{_golden_line_name(channel_lines.get('lower_source'))}） | "
            f"{rating.get('channel_regime', 'unknown')} / {rating.get('channel_strength', 'N/A')} | "
            f"{actions} | {strengths} | {reasons} | "
            f"{'；'.join(dict.fromkeys(risks)) or '无'} |"
        )
    return lines


def _render_rating_strength(entry: dict[str, Any]) -> str:
    base_strength = entry.get("base_strength", entry.get("strength"))
    adjustment = int(entry.get("strength_adjustment") or 0)
    detail = f"{entry.get('strength')}（{base_strength}{adjustment:+d}）"
    modifiers = [item for item in entry.get("modifiers", []) if isinstance(item, dict)]
    if not modifiers:
        return detail

    modifier_parts = []
    for modifier in modifiers:
        indicator = str(modifier.get("indicator") or "未知辅助")
        modifier_adjustment = int(modifier.get("adjustment") or 0)
        reason = str(modifier.get("reason") or "").strip()
        modifier_parts.append(f"{indicator} {modifier_adjustment:+d}" + (f"：{reason}" if reason else ""))
    return f"{detail}<br>{'<br>'.join(modifier_parts)}"


def _render_position_auxiliary_signals(items: list[StrategyEvaluation]) -> list[str]:
    rows: list[str] = []
    for item in sorted(items, key=lambda x: x.code):
        rating = (item.indicators or {}).get("golden_bull_position_rating")
        if not isinstance(rating, dict):
            continue
        signals = [signal for signal in rating.get("auxiliary_signals", []) if isinstance(signal, dict)]
        if not signals:
            continue
        rendered = []
        for signal in signals:
            name = str(signal.get("name") or "未知辅助")
            bullish_adjustment = int(signal.get("bullish_adjustment") or 0)
            bearish_adjustment = int(signal.get("bearish_adjustment") or 0)
            reason = str(signal.get("reason") or "").strip()
            rendered.append(
                f"{name}（偏多 {bullish_adjustment:+d} / 偏空 {bearish_adjustment:+d}"
                f"{'，' + reason if reason else ''}）"
            )
        rows.append(f"- {item.name}（{item.code}）：{'；'.join(rendered)}")
    return rows or ["暂无。"]


def _golden_line_name(value: object) -> str:
    return {
        "trend_upper": "趋势上轨",
        "life_line": "生命线",
        "trend_confirmation_line": "趋势确认线",
    }.get(str(value), str(value or "未知"))


def _render_eval_table(items: list[StrategyEvaluation]) -> list[str]:
    if not items:
        return ["暂无。"]
    lines = [
        "| 股票 | 代码 | 分数 | 等级 | 收盘价 | 涨跌幅 | 核心理由 | 形态细节 | 风险提示 |",
        "| :--- | :--- | ---: | :---: | ---: | ---: | :--- | :--- | :--- |",
    ]
    for item in sorted(items, key=lambda x: (-x.score, x.code)):
        reasons = "；".join(item.hit_reasons[:4]) or "无"
        details = _render_signal_details(item)
        risks = _render_risks(item)
        lines.append(
            f"| {item.name} | {item.code} | {item.score} | {item.grade} | "
            f"{_fmt(item.close)} | {_fmt_pct(item.pct_chg)} | {reasons} | {details} | {risks} |"
        )
    return lines


def _render_risks(item: StrategyEvaluation) -> str:
    rating = (item.indicators or {}).get("golden_bull_position_rating")
    if isinstance(rating, dict):
        risk_flags = [str(value) for value in rating.get("risk_flags", []) if value]
        if risk_flags:
            return "；".join(risk_flags[:4])
    return "；".join(item.exclude_reasons or item.penalty_reasons or item.warnings[:3]) or "无"


def _render_excluded_table(items: list[StrategyEvaluation]) -> list[str]:
    if not items:
        return ["暂无。"]
    lines = [
        "| 股票 | 代码 | 分数 | 剔除理由 |",
        "| :--- | :--- | ---: | :--- |",
    ]
    for item in sorted(items, key=lambda x: (-x.score, x.code)):
        reason = "；".join(item.exclude_reasons or item.warnings[:3]) or "未满足策略阈值"
        lines.append(f"| {item.name} | {item.code} | {item.score} | {reason} |")
    return lines


def _render_core_rules(items: list[StrategyEvaluation]) -> list[str]:
    if not items:
        return ["暂无。"]
    lines = [
        "| 股票 | 代码 | 核心规则 | 状态 | 说明 |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for item in sorted(items, key=lambda x: x.code):
        for rule in item.core_rule_results:
            status = rule.status if rule.status != "passed" else ("通过" if rule.passed else "未通过")
            lines.append(f"| {item.name} | {item.code} | {rule.name} | {status} | {rule.reason} |")
    return lines


def _render_warnings(warnings: list[str], invalid_codes: list[str], missing_data: list[str]) -> list[str]:
    lines: list[str] = []
    if invalid_codes:
        lines.append(f"- 无法识别代码：{', '.join(invalid_codes)}")
    if missing_data:
        lines.append(f"- 缺失数据：{', '.join(missing_data)}")
    for warning in warnings:
        lines.append(f"- {warning}")
    return lines or ["暂无。"]


def _render_research_contexts(
    evaluations: list[StrategyEvaluation],
    contexts: dict[str, dict[str, Any]],
    warnings: list[str],
) -> list[str]:
    lines: list[str] = []
    for warning in warnings:
        lines.append(f"- 研究资料警告：{warning}")
    if warnings:
        lines.append("")
    if not evaluations:
        return lines or ["暂无需要展示研究上下文的股票。"]

    for evaluation in sorted(evaluations, key=lambda item: item.code):
        lines.extend([f"### {evaluation.name}（{evaluation.code}）", ""])
        context = contexts.get(evaluation.ts_code)
        if not context:
            lines.extend(["- 研究资料状态：未能读取研究上下文。", ""])
            continue
        coverage = context.get("coverage") or {}
        if not coverage.get("document_count"):
            lines.extend([f"- 资料覆盖：截至 {context.get('as_of')} 暂无有效窗口内的已标注资料。", ""])
            continue
        sources = "、".join(coverage.get("sources") or []) or "未知"
        lines.append(
            f"- 资料覆盖：{coverage.get('document_count')} 份；最近更新于 "
            f"{coverage.get('latest_published_at')}；来源：{sources}"
        )
        for label, key in [
            ("主要风险", "risks"),
            ("近期催化", "catalysts"),
            ("近期观点", "claims"),
            ("预测", "predictions"),
            ("事实", "facts"),
            ("情绪", "sentiments"),
            ("推荐", "recommendations"),
        ]:
            items = context.get(key) or []
            if not items:
                continue
            lines.append(f"- {label}：")
            for item in items:
                lines.append(
                    f"  - [{item.get('type')}] {item.get('statement')} "
                    f"（{item.get('source')}，{item.get('published_at')}，文档 `{item.get('document_id')}`）"
                )
                lines.append(f"    - 原文证据：{item.get('evidence')}")
        for conflict in context.get("conflicts") or []:
            lines.append(f"- 观点分歧：{conflict}")
        for warning in context.get("warnings") or []:
            lines.append(f"- 研究限制：{warning}")
        lines.append("")
    return lines


def _render_signal_details(item: StrategyEvaluation) -> str:
    indicators = item.indicators or {}
    rating = indicators.get("golden_bull_position_rating")
    if isinstance(rating, dict) and rating.get("ratings"):
        ratings = " / ".join(
            f"{entry.get('action')} {entry.get('strength')}分"
            for entry in rating.get("ratings", [])[:4]
            if isinstance(entry, dict)
        )
        scenes = ",".join(str(scene) for scene in rating.get("scenes", [])[:4]) or "无"
        metrics = rating.get("metrics") if isinstance(rating.get("metrics"), dict) else {}
        parts = [
            f"通道 {rating.get('channel_regime', 'unknown')}",
            f"场景 {scenes}",
            f"评级 {ratings}",
            f"距支撑 {_fmt_pct(metrics.get('distance_to_support_pct'))}",
            f"距上轨 {_fmt_pct(metrics.get('distance_to_upper_pct'))}",
        ]
        if rating.get("cost_price") is not None:
            parts.append(f"成本 {_fmt(rating.get('cost_price'))}")
            parts.append(f"盈亏 {_fmt_pct(rating.get('profit_pct'))}")
        if rating.get("invalidations"):
            parts.append(f"失效 {'；'.join(str(item) for item in rating.get('invalidations', [])[:2])}")
        return "；".join(parts)

    golden = indicators.get("golden_bull_profile")
    if isinstance(golden, dict) and golden.get("life_line") is not None:
        parts = [
            f"生命线 {_fmt(golden.get('life_line'))}",
            f"距生命线 {_fmt_pct(golden.get('distance_to_life_pct'))}",
            f"距支撑 {_fmt_pct(golden.get('distance_to_support_pct'))}",
            f"距上轨 {_fmt_pct(golden.get('distance_to_upper_pct'))}",
        ]
        bull_context = golden.get("bull_context")
        if isinstance(bull_context, dict):
            parts.append(f"生命线斜率 {_fmt_pct(bull_context.get('life_slope_pct'))}")
        if golden.get("close_below_life_days"):
            parts.append(f"连续低于生命线 {golden.get('close_below_life_days')} 日")
        duanxian_detail = _render_duanxian_auxiliary(indicators)
        if duanxian_detail:
            parts.append(duanxian_detail)
        return "；".join(parts)

    duanxian_detail = _render_duanxian_auxiliary(indicators)
    if duanxian_detail:
        return duanxian_detail

    box = indicators.get("close_breakout_box") or indicators.get("consolidation_box")
    if not isinstance(box, dict) or not box.get("passed"):
        return "无"

    parts = [
        f"平台 {box.get('box_start_trade_date', 'N/A')}~{box.get('box_end_trade_date', 'N/A')}",
        f"{box.get('box_len', 'N/A')}K",
        f"振幅 {_fmt_pct(box.get('box_range_pct'))}",
        f"高收 {_fmt(box.get('box_high_close'))}",
        f"突破 {_fmt_pct(box.get('breakout_pct'))}",
    ]
    volume = indicators.get("breakout_volume")
    if isinstance(volume, dict) and volume.get("volume_ratio") is not None:
        parts.append(f"量比 {_fmt(volume.get('volume_ratio'))}x")
    drawdown = indicators.get("pre_box_drawdown")
    if isinstance(drawdown, dict) and drawdown.get("drawdown_pct") is not None:
        parts.append(f"前置回撤 {_fmt_pct(drawdown.get('drawdown_pct'))}")
    duanxian_detail = _render_duanxian_auxiliary(indicators)
    if duanxian_detail:
        parts.append(duanxian_detail)
    return "；".join(parts)


def _render_duanxian_auxiliary(indicators: dict[str, Any]) -> str:
    duanxian = indicators.get("duanxian_auxiliary")
    if not isinstance(duanxian, dict):
        return ""
    labels = {
        "price_support_triangle": "价托",
        "volume_support_triangle": "量托",
        "bullish_sandwich": "多方炮",
        "obv_golden_cross": "OBV金叉",
        "ant_channel_hold": "蚂蚁功",
        "sesame_volume": "芝麻量",
        "bearish_sandwich": "空方炮",
        "triple_dead_cross_top": "三死叉",
        "obv_dead_cross": "OBV死叉",
        "three_line_stop": "三线止损",
    }
    bullish = [labels.get(key, key) for key in duanxian.get("bullish_signals", [])]
    bearish = [labels.get(key, key) for key in duanxian.get("bearish_signals", [])]
    parts = []
    if bullish:
        parts.append(f"短线辅助偏多：{'、'.join(bullish[:5])}")
    if bearish:
        parts.append(f"短线辅助偏空：{'、'.join(bearish[:5])}")
    return "；".join(parts)


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
