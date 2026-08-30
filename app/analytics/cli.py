"""A股分析 CLI（T2：并入 stock-analytics 的分析命令面）。

用法：
  ./venv/bin/python -m app.analytics.cli <command> ...
  或挂载到统一入口 ./venv/bin/python -m app.cli <command> ...（与 sync 同 CLI）

命令：
  list-strategies / inspect-strategy / validate
  query basic|stock-name|fina|daily-basic|history|industries|industry|index
  screen / market-review / market-period-review / account-review / baolei / chart
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import traceback
from typing import Any, Callable

from tech_indicators.chart import run_chart
from tech_indicators.errors import TechIndicatorsError, UserInputError as TiUserInputError
from tech_indicators.strategies import get_strategy, load_strategies

from .account import run_account_review
from .baolei import run_baolei
from .errors import AnalyticsError, UserInputError
from .inputs import normalize_code
from .lhb import run_lhb
from .market_period_review import run_market_period_review
from .market_review import run_market_review
from .repository import SqliteRepository
from .screen import run_screen

CORE_RULES_PATH = None  # CORE_RULES.md 属于研报/策略知识库（T3），T2 不校验


def _stock_repository(database: str | None) -> SqliteRepository:
    return SqliteRepository(database)


def _db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", help="Sqlite database path override (default: data/stock.db)")


def _query(args: argparse.Namespace) -> dict[str, object]:
    repository = _stock_repository(args.database)
    command = args.query_command

    if command == "basic":
        code = normalize_code(args.code)
        data = repository.fetch_stock_basic_info(code.ts_code)
        return {"ok": True, "query": command, "code": code.ts_code, "found": data is not None, "data": data}
    if command == "stock-name":
        rows = repository.fetch_stocks_by_name(args.name, args.limit)
        exact_count = repository.count_stocks_by_exact_name(args.name)
        exact = [row for row in rows if row["match_type"] == "exact"]
        resolved = exact_count == 1 and len(exact) == 1
        return {
            "ok": True,
            "query": command,
            "name": args.name.strip(),
            "count": len(rows),
            "exact_count": exact_count,
            "resolved": resolved,
            "resolved_code": exact[0]["ts_code"] if resolved else None,
            "data": rows,
        }
    if command == "fina":
        code = normalize_code(args.code)
        data = repository.fetch_fina_indicator(code.ts_code, args.period)
        return {"ok": True, "query": command, "code": code.ts_code, "period": args.period, "found": data is not None, "data": data}
    if command == "daily-basic":
        code = normalize_code(args.code)
        rows = repository.fetch_daily_basic(code.ts_code, args.start, args.end)
        return {"ok": True, "query": command, "code": code.ts_code, "count": len(rows), "data": rows}
    if command == "history":
        from .inputs import parse_codes_arg as _pc
        codes, invalid = _pc(",".join(args.code))
        if invalid:
            raise UserInputError(f"Invalid codes: {', '.join(invalid)}")
        if args.lookback_days <= 0:
            raise UserInputError("--lookback-days must be greater than 0")
        if args.start:
            start_date = args.start
            end_date = args.end or repository.resolve_trade_date(args.date)
        else:
            end_date = repository.resolve_trade_date(args.end or args.date)
            start_date = repository.resolve_start_date(end_date, args.lookback_days)
        rows = repository.fetch_stock_history([code.ts_code for code in codes], start_date, end_date)
        return {
            "ok": True,
            "query": command,
            "codes": [code.ts_code for code in codes],
            "start_date": start_date.replace("-", ""),
            "end_date": end_date.replace("-", ""),
            "count": len(rows),
            "data": rows,
        }
    if command == "industries":
        rows = repository.list_industries()
        return {"ok": True, "query": command, "count": len(rows), "data": rows}
    if command == "industry":
        rows = repository.fetch_stocks_by_industry(args.name)
        return {"ok": True, "query": command, "industry": args.name, "count": len(rows), "data": rows}
    if command == "index":
        rows = repository.fetch_index_daily(args.code, args.start, args.end)
        return {"ok": True, "query": command, "code": args.code, "count": len(rows), "data": rows}
    raise UserInputError(f"Unsupported query command: {command}")


def _list_strategies() -> dict[str, object]:
    strategies = load_strategies()
    return {
        "ok": True,
        "strategies": [
            {"name": item.name, "display_name": item.display_name, "category": item.category, "executable": item.executable}
            for item in strategies.values()
        ],
    }


def _inspect_strategy(name: str) -> dict[str, object]:
    item = get_strategy(name)
    return {
        "ok": True,
        "strategy": {
            "name": item.name,
            "display_name": item.display_name,
            "description": item.description,
            "category": item.category,
            "core_rules": item.core_rules,
            "executable": item.executable,
            "path": str(item.path.resolve()),
        },
    }


def _validate(args: argparse.Namespace) -> dict[str, object]:
    if args.strategy:
        get_strategy(args.strategy)
    repository = _stock_repository(args.database)
    repository.validate()
    return {"ok": True, "database": str(repository.db_path), "data_source": "sqlite", "strategy": args.strategy}


def _screen(args: argparse.Namespace) -> dict[str, object]:
    strategy = get_strategy(args.strategy)
    result = run_screen(
        strategy=strategy,
        input_path=args.input,
        codes_arg=args.codes,
        universe=args.universe,
        positions_path=args.positions,
        include_bj=args.include_bj,
        include_st=args.include_st,
        output_path=args.output,
        db_path=args.database,
        requested_date=args.date,
        lookback_days=args.lookback_days,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        run_record_path=args.run_record,
        min_circ_mv_e=args.min_circ_mv_e,
        max_price=args.max_price,
        return_evaluations=bool(args.eval_json),
    )
    if args.eval_json:
        evaluations = result.pop("evaluations", [])
        Path(args.eval_json).write_text(
            json.dumps(evaluations, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    return result


def _market_review(args: argparse.Namespace) -> int:
    repository = _stock_repository(args.database)
    result = run_market_review(repository, requested_date=args.date, output_path=args.output)
    if args.output:
        return _print_ok(result)
    return 0


def _market_period_review(args: argparse.Namespace) -> int:
    if args.start and not args.end:
        raise UserInputError("--end is required when --start is provided")
    if args.period and args.end:
        raise UserInputError("--end can only be used with --start")
    repository = _stock_repository(args.database)
    result = run_market_period_review(
        repository,
        period=args.period,
        requested_date=args.date,
        start=args.start,
        end=args.end,
        output_path=args.output,
    )
    if args.output:
        return _print_ok(result)
    return 0


def _account_review(args: argparse.Namespace) -> int:
    repository = _stock_repository(args.database)
    result = run_account_review(
        repository,
        positions_path=args.positions,
        requested_date=args.date,
        output_path=args.output,
        lookback_days=args.lookback_days,
    )
    if args.output:
        return _print_ok(result)
    return 0


def _baolei(args: argparse.Namespace) -> dict[str, object]:
    return run_baolei(
        all_mode=args.all,
        codes=args.codes,
        self_test=args.self_test,
        report=args.report,
        db_path=args.database,
    )


def _lhb(args: argparse.Namespace) -> dict[str, object]:
    return run_lhb(trade_date=args.trade_date, db_path=args.database)


def _chart(args: argparse.Namespace) -> dict[str, object]:
    repository = _stock_repository(args.database)
    return run_chart(
        repository,
        code=args.code,
        requested_date=args.date,
        lookback_days=args.lookback_days,
        indicators=args.indicators,
        output_path=args.output,
    )


def add_analytics_subparsers(sub) -> None:
    """把分析命令挂在 argparse subparsers 上（供 app.cli main 复用）。"""

    sub.add_parser("list-strategies").set_defaults(func=lambda args: _list_strategies(), _json=True)

    inspect_parser = sub.add_parser("inspect-strategy")
    inspect_parser.add_argument("--strategy", required=True)
    inspect_parser.set_defaults(func=lambda args: _inspect_strategy(args.strategy), _json=True)

    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--strategy")
    _db_arg(validate_parser)
    validate_parser.set_defaults(func=_validate, _json=True)

    query_parser = sub.add_parser("query")
    _db_arg(query_parser)
    query_sub = query_parser.add_subparsers(dest="query_command", required=True)

    q_basic = query_sub.add_parser("basic")
    q_basic.add_argument("--code", required=True)
    q_fina = query_sub.add_parser("fina")
    q_fina.add_argument("--code", required=True)
    q_fina.add_argument("--period", default="latest")
    q_name = query_sub.add_parser("stock-name")
    q_name.add_argument("--name", required=True)
    q_name.add_argument("--limit", type=int, default=10)
    q_db = query_sub.add_parser("daily-basic")
    q_db.add_argument("--code", required=True)
    q_db.add_argument("--start")
    q_db.add_argument("--end")
    q_hist = query_sub.add_parser("history")
    q_hist.add_argument("--code", action="append", required=True)
    q_hist.add_argument("--start")
    q_hist.add_argument("--end")
    q_hist.add_argument("--date", default="latest")
    q_hist.add_argument("--lookback-days", type=int, default=120)
    query_sub.add_parser("industries")
    q_ind = query_sub.add_parser("industry")
    q_ind.add_argument("--name", required=True)
    q_idx = query_sub.add_parser("index")
    q_idx.add_argument("--code", required=True)
    q_idx.add_argument("--start")
    q_idx.add_argument("--end")
    query_parser.set_defaults(func=_query, _json=True)

    screen_parser = sub.add_parser("screen")
    screen_parser.add_argument("--strategy", required=True)
    screen_parser.add_argument("--input")
    screen_parser.add_argument("--codes")
    screen_parser.add_argument("--positions")
    screen_parser.add_argument("--universe", choices=["a_share"])
    screen_parser.add_argument("--include-bj", action="store_true")
    screen_parser.add_argument("--include-st", action="store_true")
    screen_parser.add_argument("--output")
    _db_arg(screen_parser)
    screen_parser.add_argument("--date", default="latest")
    screen_parser.add_argument("--lookback-days", type=int, default=120)
    screen_parser.add_argument("--batch-size", type=int, default=500)
    screen_parser.add_argument("--min-circ-mv-e", type=float)
    screen_parser.add_argument("--max-price", type=float)
    screen_parser.add_argument("--dry-run", action="store_true")
    screen_parser.add_argument("--run-record")
    screen_parser.add_argument("--eval-json", help="将 StrategyEvaluation 明细写入 JSON 文件（invest-research pool-rate 使用）")
    screen_parser.set_defaults(func=_screen, _json=True)

    mr_parser = sub.add_parser("market-review")
    mr_parser.add_argument("--date", default="latest")
    mr_parser.add_argument("--output")
    _db_arg(mr_parser)
    mr_parser.set_defaults(func=_market_review)

    mpr_parser = sub.add_parser("market-period-review")
    period_input = mpr_parser.add_mutually_exclusive_group(required=True)
    period_input.add_argument("--period", choices=["week", "month"])
    period_input.add_argument("--start")
    mpr_parser.add_argument("--end")
    mpr_parser.add_argument("--date", default="latest")
    mpr_parser.add_argument("--output")
    _db_arg(mpr_parser)
    mpr_parser.set_defaults(func=_market_period_review)

    ar_parser = sub.add_parser("account-review")
    ar_parser.add_argument("--positions", required=True)
    ar_parser.add_argument("--date", default="latest")
    ar_parser.add_argument("--lookback-days", type=int, default=120)
    ar_parser.add_argument("--output")
    _db_arg(ar_parser)
    ar_parser.set_defaults(func=_account_review)

    baolei_parser = sub.add_parser("baolei")
    baolei_parser.add_argument("--all", action="store_true")
    baolei_parser.add_argument("--codes", type=str, default="")
    baolei_parser.add_argument("--self-test", action="store_true")
    baolei_parser.add_argument("--report", type=str, default="")
    _db_arg(baolei_parser)
    baolei_parser.set_defaults(func=_baolei, _json=True)

    lhb_parser = sub.add_parser("lhb", help="龙虎榜超买/超卖信号（top_list）")
    lhb_parser.add_argument("--trade-date", type=str, default=None, help="交易日 YYYYMMDD（默认最近有数据交易日）")
    _db_arg(lhb_parser)
    lhb_parser.set_defaults(func=_lhb, _json=True)

    chart_parser = sub.add_parser("chart")
    chart_parser.add_argument("--code", required=True)
    chart_parser.add_argument("--date", default="latest")
    chart_parser.add_argument("--lookback-days", type=int, default=120)
    chart_parser.add_argument("--indicators", default=None)
    chart_parser.add_argument("--output")
    _db_arg(chart_parser)
    chart_parser.set_defaults(func=_chart, _json=True)


def _print_ok(payload: dict[str, object]) -> int:
    payload.setdefault("ok", True)
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    return 0


def _print_error(exc: AnalyticsError) -> int:
    payload = {"ok": False, "error_code": exc.error_code, "message": str(exc), "hint": exc.hint}
    payload.update(exc.payload)
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    return exc.exit_code


def _dispatch(args: argparse.Namespace) -> int:
    func = getattr(args, "func", None)
    if func is None:
        raise UserInputError("Missing command")
    result = func(args)
    if isinstance(result, int):
        return result
    if isinstance(result, dict):
        return _print_ok(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.analytics.cli", description="A股分析 CLI（数据源 sqlite）")
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    add_analytics_subparsers(sub)
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except AnalyticsError as exc:
        return _print_error(exc)
    except TiUserInputError as exc:
        return _print_error(UserInputError(str(exc)))
    except TechIndicatorsError as exc:
        return _print_error(UserInputError(str(exc), hint=hint_of(exc)))
    except Exception as exc:  # defensive CLI boundary
        if getattr(args, "debug", False):
            traceback.print_exc(file=sys.stderr)
        return _print_error(AnalyticsError(str(exc)))


def hint_of(exc: TechIndicatorsError) -> str:
    return getattr(exc, "hint", "") or ""


if __name__ == "__main__":
    sys.exit(main())