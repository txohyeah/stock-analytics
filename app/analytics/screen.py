"""策略筛选（screen）——从 stock-research runner.py 的 run_screen 迁移。

改动：
- 数据源改为 sqlite（SqliteRepository），去掉 Tushare/MySQL 选择逻辑
- 研报（research）集成移除：研报知识库是独立原始层（T3），screen 不调用
- 计算引擎来自公共包 tech-indicators（compute_indicators / RuleEvaluator / Strategy）
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys
from uuid import uuid4

from tech_indicators.indicators import compute_indicators
from tech_indicators.models import StrategyEvaluation
from tech_indicators.strategies import RuleEvaluator, Strategy

from .errors import DataInsufficientError, UserInputError
from .inputs import parse_codes_arg, parse_input_file, parse_positions_file
from .models import MissingDataContract, MissingDataItem, StockCode
from .report import write_markdown_report
from .repository import SqliteRepository


def collect_input_codes(
    input_path: str | None,
    codes_arg: str | None,
    universe: str | None = None,
    positions_path: str | None = None,
) -> tuple[list[StockCode], list[str], str, dict[str, dict[str, object]]]:
    selected_sources = sum(bool(item) for item in [input_path, codes_arg, universe, positions_path])
    if selected_sources != 1:
        raise UserInputError("Provide exactly one of --input, --codes, --universe, or --positions")
    if universe:
        return [], [], f"universe:{universe}", {}
    if positions_path:
        codes, invalid, contexts = parse_positions_file(positions_path)
        return codes, invalid, str(positions_path), contexts
    if input_path:
        codes, invalid = parse_input_file(input_path)
        return codes, invalid, str(input_path), {}
    codes, invalid = parse_codes_arg(codes_arg)
    return codes, invalid, "codes", {}


def run_screen(
    *,
    strategy: Strategy,
    input_path: str | None,
    codes_arg: str | None,
    output_path: str | None,
    db_path: str | None,
    requested_date: str,
    lookback_days: int,
    batch_size: int,
    dry_run: bool,
    run_record_path: str | None,
    universe: str | None = None,
    positions_path: str | None = None,
    include_bj: bool = False,
    include_st: bool = False,
    min_circ_mv_e: float | None = None,
    max_price: float | None = None,
    return_evaluations: bool = False,
) -> dict[str, object]:
    started_at = datetime.now()
    run_id = uuid4().hex[:12]
    codes, invalid_codes, input_source, position_contexts = collect_input_codes(
        input_path,
        codes_arg,
        universe,
        positions_path,
    )
    if batch_size <= 0:
        raise UserInputError("--batch-size must be greater than 0")
    if min_circ_mv_e is not None and min_circ_mv_e < 0:
        raise UserInputError("--min-circ-mv-e must be greater than or equal to 0")
    if max_price is not None and max_price <= 0:
        raise UserInputError("--max-price must be greater than 0")

    repository = SqliteRepository(db_path)
    repository.validate()
    trade_date = repository.resolve_trade_date(requested_date)
    start_date = repository.resolve_start_date(trade_date, lookback_days)
    min_history_days = int((strategy.executable_rules or {}).get("min_history_days") or 1)

    if universe:
        _progress(f"loading universe {universe}")
        codes = repository.fetch_universe_codes(
            universe,
            include_bj=include_bj,
            include_st=include_st,
            trade_date=trade_date,
            start_date=start_date,
            min_history_days=min_history_days,
            min_circ_mv=_circ_mv_e_to_wan(min_circ_mv_e),
            max_price=max_price,
        )
        _progress(f"universe loaded: {len(codes)} stocks")
    if not codes:
        raise UserInputError("No valid stock codes provided")

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "strategy": strategy.name,
            "trade_date": trade_date,
            "input_count": len(codes),
            "invalid_count": len(invalid_codes),
            "position_context_count": len(position_contexts),
            "batch_size": batch_size,
            "database": str(repository.db_path),
            "data_source": "sqlite",
            "universe_filters": _universe_filters_payload(
                min_history_days=min_history_days,
                min_circ_mv_e=min_circ_mv_e,
                max_price=max_price,
            ),
        }

    if not output_path:
        raise UserInputError("--output is required unless --dry-run is used")

    evaluator = RuleEvaluator()
    evaluations: list[StrategyEvaluation] = []
    missing_data: list[str] = []
    missing_items: list[MissingDataItem] = []
    batches = _chunks(codes, batch_size)
    analyzed_count = 0
    for batch_index, batch in enumerate(batches, start=1):
        _progress(f"fetching histories batch {batch_index}/{len(batches)} ({len(batch)} stocks)")
        histories = repository.fetch_histories_batch([item.ts_code for item in batch], start_date, trade_date)
        _progress(f"analyzing batch {batch_index}/{len(batches)}")
        for code in batch:
            analyzed_count += 1
            if analyzed_count == 1 or analyzed_count % 1000 == 0 or analyzed_count == len(codes):
                _progress(f"analyzing {analyzed_count}/{len(codes)}")
            history = histories.get(code.ts_code)
            if history is None or history.bars.empty:
                missing_data.append(code.ts_code)
                missing_items.append(
                    MissingDataItem(
                        dataset="daily",
                        ts_code=code.ts_code,
                        start_date=start_date,
                        end_date=trade_date,
                        reason="数据库中没有可用于分析的日线行情",
                        actual_count=0,
                        required_count=min_history_days,
                    )
                )
                continue
            latest_bar_date = str(history.bars.iloc[-1].get("trade_date"))
            if latest_bar_date != trade_date:
                missing_data.append(code.ts_code)
                missing_items.append(
                    MissingDataItem(
                        dataset="daily",
                        ts_code=code.ts_code,
                        start_date=start_date,
                        end_date=trade_date,
                        reason=f"日线数据未同步到目标交易日 {trade_date}，当前最新为 {latest_bar_date}",
                        actual_count=len(history.bars),
                        required_count=min_history_days,
                    )
                )
                continue
            if len(history.bars) < min_history_days:
                missing_data.append(code.ts_code)
                missing_items.append(
                    MissingDataItem(
                        dataset="daily",
                        ts_code=code.ts_code,
                        start_date=start_date,
                        end_date=trade_date,
                        reason=f"需要至少 {min_history_days} 个交易日，当前只有 {len(history.bars)} 个",
                        actual_count=len(history.bars),
                        required_count=min_history_days,
                    )
                )
                continue
            indicators = compute_indicators(history.bars, position_context=position_contexts.get(code.ts_code))
            meta = {
                "code": code.code,
                "ts_code": code.ts_code,
                "symbol": history.meta.get("symbol") or code.code,
                "name": history.meta.get("name") or code.ts_code,
            }
            evaluations.append(evaluator.evaluate(strategy, meta, indicators))

    missing_contract = _build_missing_data_contract(
        strategy_name=strategy.name,
        ts_codes=[item.ts_code for item in codes],
        start_date=start_date,
        end_date=trade_date,
        missing_items=missing_items,
    )

    if not evaluations:
        raise DataInsufficientError(
            "No stock has enough data to analyze",
            payload=missing_contract.to_dict() if missing_contract else None,
        )

    write_markdown_report(
        output_path,
        strategy=strategy,
        trade_date=trade_date,
        input_count=len(codes),
        invalid_codes=invalid_codes,
        evaluations=evaluations,
        missing_data=missing_data,
    )

    selected_count = sum(1 for item in evaluations if item.bucket == "selected")
    warning_count = len({warning for item in evaluations for warning in item.warnings}) + len(invalid_codes) + len(missing_data)
    finished_at = datetime.now()
    run_record = {
        "run_id": run_id,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "strategy": strategy.name,
        "trade_date": trade_date,
        "database": str(repository.db_path),
        "data_source": "sqlite",
        "input_source": input_source,
        "input_count": len(codes),
        "position_context_count": len(position_contexts),
        "batch_size": batch_size,
        "analyzed_count": len(evaluations),
        "selected_count": selected_count,
        "excluded_count": sum(1 for item in evaluations if item.bucket == "excluded"),
        "warning_count": warning_count,
        "missing_data": missing_data,
        "universe_filters": _universe_filters_payload(
            min_history_days=min_history_days,
            min_circ_mv_e=min_circ_mv_e,
            max_price=max_price,
        ),
        "missing_data_contract": missing_contract.to_dict() if missing_contract else None,
        "report_path": str(Path(output_path).resolve()),
    }
    _write_run_record(run_record, output_path, run_record_path)
    result = {
        "ok": True,
        "report_path": str(Path(output_path).resolve()),
        "strategy": strategy.name,
        "trade_date": trade_date,
        "data_source": "sqlite",
        "input_count": len(codes),
        "position_context_count": len(position_contexts),
        "batch_size": batch_size,
        "selected_count": selected_count,
        "warning_count": warning_count,
        "universe_filters": _universe_filters_payload(
            min_history_days=min_history_days,
            min_circ_mv_e=min_circ_mv_e,
            max_price=max_price,
        ),
        "missing_data_contract": missing_contract.to_dict() if missing_contract else None,
    }
    if return_evaluations:
        result["evaluations"] = [_evaluation_payload(item) for item in evaluations]
    return result


def _evaluation_payload(item: StrategyEvaluation) -> dict[str, object]:
    return {
        "code": item.code,
        "ts_code": item.ts_code,
        "name": item.name,
        "close": item.close,
        "pct_chg": item.pct_chg,
        "indicators": item.indicators,
        "warnings": item.warnings,
    }


def _write_run_record(record: dict[str, object], output_path: str, run_record_path: str | None) -> None:
    path = Path(run_record_path) if run_record_path else Path(output_path).with_suffix(".run.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _progress(message: str) -> None:
    print(f"[stock-analytics] {message}", file=sys.stderr, flush=True)


def _chunks(items: list[StockCode], size: int) -> list[list[StockCode]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _circ_mv_e_to_wan(value: float | None) -> float | None:
    return None if value is None else value * 10000


def _universe_filters_payload(
    *,
    min_history_days: int,
    min_circ_mv_e: float | None,
    max_price: float | None,
) -> dict[str, float | int | None]:
    return {
        "min_history_days": min_history_days,
        "min_circ_mv_e": min_circ_mv_e,
        "max_price": max_price,
    }


def _build_missing_data_contract(
    *,
    strategy_name: str,
    ts_codes: list[str],
    start_date: str,
    end_date: str,
    missing_items: list[MissingDataItem],
) -> MissingDataContract | None:
    if not missing_items:
        return None
    datasets = sorted({"daily", "stock_basic", "trade_cal"})
    missing_ts_codes = sorted({item.ts_code for item in missing_items if item.ts_code})
    return MissingDataContract(
        status="missing_data",
        analysis_type=strategy_name,
        required={
            "ts_codes": ts_codes,
            "datasets": datasets,
            "start_date": start_date,
            "end_date": end_date,
        },
        missing=missing_items,
        reason="daily 数据缺失或长度不足，无法完成当前策略分析。",
        retryable=True,
        suggested_command={
            "project": "stock-analytics",
            "module": "app.cli",
            "args": {
                "command": "sync",
                "dataset": "daily",
                "ts_codes": missing_ts_codes,
                "start": start_date,
                "end": end_date,
            },
        },
    )