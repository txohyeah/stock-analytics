"""Tushare stock data sync CLI.

One-shot commands (no daemon). The crontab calls:

    10 20 * * 1-5  run_sync.sh market
    30 21 * * *    run_sync.sh finance

market skips non-trading days automatically (trade_cal). Every dataset is
recorded in sync_run; failures are retried at tushare-client level and a
Feishu summary is sent when any dataset of a group fails and Feishu is
configured.
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from app.config import get_settings
from app.db import get_store
from app.notifier import send_text
from app.providers import FallbackProvider
from app.sync.base import SyncContext, is_trade_day_today, lookback_start, run_dataset, run_many, today_yyyymmdd
from app.sync.registry import DATASETS, DAILY_ORDER, FINANCE_ORDER, datasets_for
from app.tushare_client import TushareClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

GROUPS = {
    "market": DAILY_ORDER,
    "finance": FINANCE_ORDER,
}


def build_context(enable_fallback: bool | None = None) -> SyncContext:
    settings = get_settings()
    store = get_store(settings)
    client = TushareClient(settings)
    fallback_enabled = settings.enable_fallback if enable_fallback is None else enable_fallback
    fallback_provider = FallbackProvider(settings) if fallback_enabled else None
    return SyncContext(
        client=client,
        store=store,
        settings=settings,
        fallback_provider=fallback_provider,
        enable_fallback=fallback_enabled,
    )


def cmd_list(args: argparse.Namespace | None = None) -> None:
    del args
    for name in sorted(DATASETS):
        print(name)
    for group in sorted(GROUPS):
        print(group)
    print("all")


def cmd_check_trade_day(args: argparse.Namespace | None = None) -> int:
    ctx = build_context()
    result = is_trade_day_today(ctx.store, ctx.client, ctx.settings)
    ctx.store.close()
    if result is True:
        print("trade-day")
        return 0
    if result is False:
        print("non-trade-day")
        return 0
    print("unknown")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    ctx = build_context(None if args.fallback is None else args.fallback)
    if args.ts_codes:
        ctx.ts_codes = [c.strip() for c in args.ts_codes.split(",") if c.strip()]
    end = args.end or today_yyyymmdd()
    mode = args.mode or ("history" if args.history else "daily")

    # group-level routing
    group_names = None
    if args.dataset in GROUPS:
        group_names = args.dataset
        datasets = [DATASETS[name] for name in GROUPS[args.dataset]]
        start = args.start or (
            lookback_start(ctx.settings.finance_lookback_days) if args.dataset == "finance" else lookback_start(ctx.settings.sync_lookback_days)
        )
        if args.dataset == "market" and not args.start:
            # skip when today is not an open trade day (crontab runs daily at 20:10)
            trade_today = is_trade_day_today(ctx.store, ctx.client, ctx.settings)
            if trade_today is False:
                logger.info("today is not a trade day, skip market sync")
                ctx.store.close()
                return 0
    elif args.dataset == "all":
        from app.sync.registry import ALL_ORDER

        datasets = [DATASETS[name] for name in ALL_ORDER]
        start = args.start or lookback_start(ctx.settings.sync_lookback_days)
    else:
        datasets = [DATASETS[args.dataset]]
        start = args.start or lookback_start(ctx.settings.sync_lookback_days)

    logger.info(
        "sync start dataset=%s start=%s end=%s mode=%s fallback=%s",
        args.dataset,
        start,
        end,
        mode,
        ctx.enable_fallback,
    )

    try:
        if group_names or args.dataset == "all":
            results = run_many(ctx, datasets, start, end, mode, args.ts_code)
        else:
            dataset = datasets[0]
            results = {dataset.name: run_dataset(ctx, dataset, start, end, mode, args.ts_code)}
        for name, (fetched, affected) in results.items():
            print(f"{name}: fetched={fetched}, affected={affected}")
        ctx.store.close()
        return 0
    except Exception as exc:  # noqa: BLE001 - full group failure
        logger.exception("sync failed: %s", exc)
        send_text(ctx.settings, f"[tushare-sync] {args.dataset} 同步整体失败: {exc}")
        ctx.store.close()
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="Tushare stock data sync tool")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Sync one dataset or a group (market/finance/all/<name>).")
    p_sync.add_argument("dataset", help="Dataset name, or group: market / finance / all.")
    p_sync.add_argument("--start", help="Start date YYYYMMDD.")
    p_sync.add_argument("--end", help="End date YYYYMMDD.")
    p_sync.add_argument("--ts-code", help="Single stock/index code.")
    p_sync.add_argument("--ts-codes", help="Comma-separated stock codes, e.g. 600519.SH,000001.SZ (per-stock datasets).")
    p_sync.add_argument("--mode", choices=["daily", "history"], help="Run mode recorded in sync_run.")
    p_sync.add_argument("--history", action="store_true", help="Shorthand for --mode history.")
    p_sync.add_argument("--fallback", type=lambda v: v.lower() in ("1", "true", "yes"), default=None,
                        help="Enable fallback sources (default: from .env).")
    p_sync.set_defaults(func=cmd_sync)

    p_list = sub.add_parser("list", help="List datasets and groups.")
    p_list.set_defaults(func=cmd_list)

    p_check = sub.add_parser("check-trade-day", help="Print trade-day/non-trade-day for today.")
    p_check.set_defaults(func=cmd_check_trade_day)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())