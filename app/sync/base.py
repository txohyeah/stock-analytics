"""Sync primitives shared by all datasets: run bookkeeping, strategies and
the common upsert path. Storage is backend-agnostic via RowStore."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import time
from typing import Callable, Iterable

import pandas as pd

from app.config import Settings
from app.db import read_stock_codes, read_trade_dates, upsert_dataframe
from app.providers import FallbackProvider
from app.storage import RowStore
from app.tushare_client import TushareClient

logger = logging.getLogger(__name__)

DEFAULT_INDEX_CODES = (
    "000001.SH",
    "399001.SZ",
    "399006.SZ",
    "000300.SH",
    "000905.SH",
    "000852.SH",
    "000688.SH",
)


SyncFunction = Callable[["SyncContext", "Dataset", str, str, str | None], tuple[int, int]]


@dataclass(frozen=True)
class Dataset:
    name: str
    api_name: str
    table_name: str
    unique_columns: tuple[str, ...]
    strategy: str
    default_params: dict[str, str] | None = None


@dataclass
class SyncContext:
    client: TushareClient
    store: RowStore
    settings: Settings
    fallback_provider: FallbackProvider | None = None
    enable_fallback: bool = True
    # per-stock datasets (income/balancesheet/cashflow/fina_indicator):
    # a batch of codes to sync; None means "whole universe from stock_basic".
    ts_codes: list[str] | None = None


def today_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def lookback_start(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")


def calendar_days(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")
    days = []
    current = start
    while current <= end:
        days.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return days


def open_trade_days_or_calendar(store: RowStore, start_date: str, end_date: str) -> list[str]:
    trade_dates = read_trade_dates(store, start_date, end_date)
    if trade_dates:
        return trade_dates
    return calendar_days(start_date, end_date)


def should_fallback(exc: Exception) -> bool:
    error_text = str(exc)
    return any(
        marker in error_text
        for marker in (
            "Tushare query failed",
            "没有接口",
            "访问权限",
            "无权限",
            "permission",
            "timeout",
            "timed out",
            "Connection",
        )
    )


def is_trade_day_today(store: RowStore, client: TushareClient, settings: Settings) -> bool | None:
    """Return True if today is an open trade day, False otherwise, and None
    when we cannot determine it (local trade_cal missing and remote query
    failed). Used by the crontab entry to skip non-trading days."""
    today = today_yyyymmdd()
    try:
        try:
            known = read_trade_dates(store, today, today)
            if known:
                return True
        except Exception:  # noqa: BLE001 - local calendar not synced yet
            pass
        # local calendar may not cover today yet -> query remote
        frame = client.query("trade_cal", exchange="SSE", start_date=today, end_date=today)
        if not frame.empty:
            is_open = int(frame.iloc[0]["is_open"])
            return bool(is_open)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("cannot determine trade day for %s: %s", today, exc)
        return None


def insert_sync_run(store: RowStore, dataset: str, mode: str, start_date: str, end_date: str) -> int:
    store.execute(
        "INSERT INTO sync_run (dataset, mode, start_date, end_date, status, started_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)" if store.driver == "mysql"
        else "INSERT INTO sync_run (dataset, mode, start_date, end_date, status, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (dataset, mode, start_date, end_date, "running", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    store.commit()
    run_id = store.lastrowid()
    assert run_id is not None
    return int(run_id)


def finish_sync_run(
    store: RowStore,
    run_id: int,
    status: str,
    fetched_rows: int,
    affected_rows: int,
    error_message: str | None = None,
) -> None:
    store.execute(
        "UPDATE sync_run SET status = %s, finished_at = %s, fetched_rows = %s, "
        "affected_rows = %s, error_message = %s WHERE id = %s" if store.driver == "mysql"
        else "UPDATE sync_run SET status = ?, finished_at = ?, fetched_rows = ?, "
        "affected_rows = ?, error_message = ? WHERE id = ?",
        (
            status,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            fetched_rows,
            affected_rows,
            error_message,
            run_id,
        ),
    )
    store.commit()


def upsert(ctx: SyncContext, dataset: Dataset, frame: pd.DataFrame) -> int:
    return upsert_dataframe(
        ctx.store,
        dataset.table_name,
        frame,
        dataset.unique_columns,
        ctx.settings.sync_batch_size,
    )


def upsert_with_source_log(ctx: SyncContext, dataset: Dataset, frame: pd.DataFrame, source: str, reason: str) -> int:
    affected = upsert(ctx, dataset, frame)
    logger.info(
        "%s fallback source=%s reason=%s fetched=%s affected=%s",
        dataset.name,
        source,
        reason,
        len(frame),
        affected,
    )
    return affected


def sync_single_call(ctx: SyncContext, dataset: Dataset, start_date: str, end_date: str, ts_code: str | None) -> tuple[int, int]:
    params = dict(dataset.default_params or {})
    if ts_code:
        params["ts_code"] = ts_code
    if dataset.strategy == "basic":
        frame = ctx.client.query(dataset.api_name, **params)
    else:
        frame = ctx.client.query(dataset.api_name, start_date=start_date, end_date=end_date, **params)
    return len(frame), upsert(ctx, dataset, frame)


def sync_by_trade_date(ctx: SyncContext, dataset: Dataset, start_date: str, end_date: str, ts_code: str | None) -> tuple[int, int]:
    fetched = 0
    affected = 0
    params = dict(dataset.default_params or {})
    if ts_code:
        params["ts_code"] = ts_code
    stock_codes: list[str] | None = None
    for trade_date in open_trade_days_or_calendar(ctx.store, start_date, end_date):
        try:
            frame = ctx.client.query(dataset.api_name, trade_date=trade_date, **params)
        except Exception as exc:
            if dataset.name not in ("daily", "index_daily") or not ctx.enable_fallback or ctx.fallback_provider is None or not should_fallback(exc):
                raise
            if dataset.name == "daily" and stock_codes is None:
                stock_codes = read_stock_codes(ctx.store)
            if dataset.name == "daily":
                fallback = ctx.fallback_provider.fetch_daily_by_trade_date(
                    trade_date=trade_date,
                    ts_code=ts_code,
                    stock_codes=stock_codes or [],
                    reason=str(exc),
                )
            else:
                fallback = ctx.fallback_provider.fetch_index_daily_by_trade_date(
                    trade_date=trade_date,
                    ts_code=ts_code,
                    index_codes=list(DEFAULT_INDEX_CODES),
                    reason=str(exc),
                )
            frame = fallback.frame
            fetched += len(frame)
            affected += upsert_with_source_log(ctx, dataset, frame, fallback.source, fallback.reason)
            logger.info("%s %s fetched=%s affected_total=%s", dataset.name, trade_date, len(frame), affected)
            continue
        if frame.empty and dataset.name in ("daily", "index_daily") and ctx.enable_fallback and ctx.fallback_provider is not None:
            if dataset.name == "daily" and stock_codes is None:
                stock_codes = read_stock_codes(ctx.store)
            if dataset.name == "daily":
                fallback = ctx.fallback_provider.fetch_daily_by_trade_date(
                    trade_date=trade_date,
                    ts_code=ts_code,
                    stock_codes=stock_codes or [],
                    reason="tushare_empty",
                )
            else:
                fallback = ctx.fallback_provider.fetch_index_daily_by_trade_date(
                    trade_date=trade_date,
                    ts_code=ts_code,
                    index_codes=list(DEFAULT_INDEX_CODES),
                    reason="tushare_empty",
                )
            if not fallback.frame.empty:
                frame = fallback.frame
                fetched += len(frame)
                affected += upsert_with_source_log(ctx, dataset, frame, fallback.source, fallback.reason)
                logger.info("%s %s fetched=%s affected_total=%s", dataset.name, trade_date, len(frame), affected)
                continue
        fetched += len(frame)
        affected += upsert(ctx, dataset, frame)
        logger.info("%s %s fetched=%s affected=%s", dataset.name, trade_date, len(frame), affected)
    return fetched, affected


def sync_by_stock(ctx: SyncContext, dataset: Dataset, start_date: str, end_date: str, ts_code: str | None) -> tuple[int, int]:
    codes = ctx.ts_codes or ([ts_code] if ts_code else read_stock_codes(ctx.store))
    if not codes:
        raise RuntimeError("No stock codes found. Run stock_basic sync first or pass --ts-code/--ts-codes.")

    fetched = 0
    affected = 0
    params = dict(dataset.default_params or {})
    for code in codes:
        frame = ctx.client.query(dataset.api_name, ts_code=code, start_date=start_date, end_date=end_date, **params)
        fetched += len(frame)
        affected += upsert(ctx, dataset, frame)
        logger.info("%s %s fetched=%s affected_total=%s", dataset.name, code, len(frame), affected)
    return fetched, affected


def sync_by_stock_no_date(ctx: SyncContext, dataset: Dataset, start_date: str, end_date: str, ts_code: str | None) -> tuple[int, int]:
    """per-stock 全量拉取（不带日期范围）。用于 fina_audit 等接口：
    start_date/end_date 语义不同（公告日期），带日期会漏最新年报审计意见。
    """
    del start_date, end_date
    codes = ctx.ts_codes or ([ts_code] if ts_code else read_stock_codes(ctx.store))
    if not codes:
        raise RuntimeError("No stock codes found. Run stock_basic sync first or pass --ts-code/--ts-codes.")

    fetched = 0
    affected = 0
    params = dict(dataset.default_params or {})
    for code in codes:
        frame = ctx.client.query(dataset.api_name, ts_code=code, **params)
        fetched += len(frame)
        affected += upsert(ctx, dataset, frame)
        logger.info("%s %s fetched=%s affected_total=%s", dataset.name, code, len(frame), affected)
    return fetched, affected


def sync_trade_cal(ctx: SyncContext, dataset: Dataset, start_date: str, end_date: str, ts_code: str | None) -> tuple[int, int]:
    del ts_code
    try:
        frame = ctx.client.query("trade_cal", exchange="SSE", start_date=start_date, end_date=end_date)
    except Exception as exc:
        if not ctx.enable_fallback or ctx.fallback_provider is None or not should_fallback(exc):
            raise
        fallback = ctx.fallback_provider.fetch_trade_cal(start_date, end_date, str(exc))
        return len(fallback.frame), upsert_with_source_log(ctx, dataset, fallback.frame, fallback.source, fallback.reason)
    if frame.empty and ctx.enable_fallback and ctx.fallback_provider is not None:
        fallback = ctx.fallback_provider.fetch_trade_cal(start_date, end_date, "tushare_empty")
        return len(fallback.frame), upsert_with_source_log(ctx, dataset, fallback.frame, fallback.source, fallback.reason)
    return len(frame), upsert(ctx, dataset, frame)


def sync_index_basic(ctx: SyncContext, dataset: Dataset, start_date: str, end_date: str, ts_code: str | None) -> tuple[int, int]:
    del start_date, end_date, ts_code
    fetched = 0
    affected = 0
    for market in ("SSE", "SZSE", "CSI", "CICC", "SW", "MSCI", "OTH"):
        frame = ctx.client.query(dataset.api_name, market=market)
        fetched += len(frame)
        affected += upsert(ctx, dataset, frame)
    return fetched, affected


def sync_fund_basic_paged(ctx: SyncContext, dataset: Dataset, start_date: str, end_date: str, ts_code: str | None) -> tuple[int, int]:
    """fund_basic 全量分页拉取（tushare 单次上限 5000 行，不分页会漏基金）。

    实测 2026-09-13：market='O' status='L' 全量约 2 万+ 行，单次只返回前 15000，
    110022 易方达消费行业股票等老基金被漏掉。用 offset/limit 分页拉全。
    """
    del start_date, end_date, ts_code
    params = dict(dataset.default_params or {})
    fetched = 0
    affected = 0
    offset = 0
    while True:
        frame = ctx.client.query(dataset.api_name, offset=offset, limit=5000, **params)
        fetched += len(frame)
        affected += upsert(ctx, dataset, frame)
        if len(frame) < 5000:
            break
        offset += 5000
    return fetched, affected


def sync_by_period_paged(ctx: SyncContext, dataset: Dataset, start_date: str, end_date: str, ts_code: str | None) -> tuple[int, int]:
    """top10_holders/top10_floatholders：period=end_date 全市场分页拉。

    实测 6000 行/批，全市场约 10 批，6 秒完成（2026-09-12 验证，
    与三大报表必须逐股拉不同，股东表支持按 period 全市场查询）。
    """
    del start_date, ts_code
    period = end_date or today_yyyymmdd()
    fetched = 0
    affected = 0
    offset = 0
    while True:
        frame = ctx.client.query(dataset.api_name, period=period, offset=offset, limit=6000)
        fetched += len(frame)
        affected += upsert(ctx, dataset, frame)
        if len(frame) < 6000:
            break
        offset += 6000
    return fetched, affected


def sync_fund_holdings(ctx: SyncContext, dataset: Dataset, start_date: str, end_date: str, ts_code: str | None) -> tuple[int, int]:
    """天天基金爬虫：主动权益基金前十大重仓股（市场大方向分析用）。

    数据流：fund_basic（tushare）→ 筛选主动权益 → 逐只爬 fundf10 → 幂等 upsert。
    筛选规则（实测 3129 只，2026-09-12）：
      fund_type in (混合型, 股票型)
      剔除 name 含 指数/ETF/联接/QDII
      剔除 name 以 C/E 结尾（份额类别，同持仓只留 A）
    每只基金请求最新 2 个年份（provider 内部处理），合并后取最近 4 个季度。
    限速 2 req/s；断点续爬：已有最新季度数据的基金跳过（fund_code 存纯数字，
    ts_code 带 .OF 后缀，需归一化比较）。
    增量模式：fund_holdings 已有数据时（季度任务场景）只请求最新 1 年，
    请求量减半（约 30-50 分钟），避免 cron 任务超时。
    """
    del start_date, ts_code
    from app.providers.eastmoney_fund import fetch_fund_holdings

    funds = ctx.store.query(
        "SELECT ts_code, name FROM fund_basic "
        "WHERE fund_type IN ('混合型', '股票型') "
        "AND name NOT LIKE '%指数%' AND name NOT LIKE '%ETF%' "
        "AND name NOT LIKE '%联接%' AND name NOT LIKE '%QDII%' "
        "AND name NOT LIKE '%C' AND name NOT LIKE '%E' "
        "ORDER BY ts_code"
    )
    if not funds:
        raise RuntimeError("fund_basic 为空或筛选后无基金。先执行: sync fund_basic")

    # 增量模式：已有数据时只请求最新 1 年（补最新季度）；全量（首次）请求 2 年
    has_data = ctx.store.query("SELECT COUNT(*) FROM fund_holdings")[0][0] > 0
    years = 1 if has_data else 2

    # 断点续爬：已有最新季度数据的基金跳过
    latest_q = ctx.store.query("SELECT MAX(end_date) FROM fund_holdings")
    latest_q = latest_q[0][0] if latest_q and latest_q[0][0] else ""
    done: set[str] = set()
    if latest_q:
        done = {
            row[0].split(".")[0]
            for row in ctx.store.query(
                "SELECT DISTINCT fund_code FROM fund_holdings WHERE end_date = ?", (latest_q,)
            )
        }

    fetched = 0
    affected = 0
    skipped = 0
    failed: list[str] = []
    for fund_code, fund_name in funds:
        if fund_code.split(".")[0] in done:
            skipped += 1
            continue
        try:
            frame = fetch_fund_holdings(fund_code, years=years)
        except Exception as exc:  # noqa: BLE001 - 单只失败不中断，记录后继续
            failed.append(f"{fund_code}: {exc}")
            logger.warning("fund_holdings %s failed: %s", fund_code, exc)
            continue
        if frame.empty:
            skipped += 1
            continue
        fetched += len(frame)
        affected += upsert(ctx, dataset, frame)
        time.sleep(0.5)  # 限速 2 req/s
        if (fetched + skipped) % 200 == 0:
            logger.info("fund_holdings progress: fetched=%s skipped=%s failed=%s", fetched, skipped, len(failed))
    if failed:
        logger.warning("fund_holdings %d 只失败: %s", len(failed), "; ".join(failed[:10]))
    return fetched, affected


STRATEGIES: dict[str, SyncFunction] = {
    "basic": sync_single_call,
    "date_range": sync_single_call,
    "trade_date": sync_by_trade_date,
    "stock": sync_by_stock,
    "stock_no_date": sync_by_stock_no_date,
    "trade_cal": sync_trade_cal,
    "index_basic": sync_index_basic,
    "holders": sync_by_period_paged,
    "fund_basic_paged": sync_fund_basic_paged,
    "fund_holdings": sync_fund_holdings,
}


def run_dataset(ctx: SyncContext, dataset: Dataset, start_date: str, end_date: str, mode: str, ts_code: str | None = None) -> tuple[int, int]:
    run_id = insert_sync_run(ctx.store, dataset.name, mode, start_date, end_date)
    try:
        # daily / adj_factor are normally fetched once per trade date for the
        # whole market.  A specified pool is far smaller and their APIs both
        # support ts_code + date range, so use one request per code instead.
        # Keep --ts-code on the original single-trade-date path for backwards
        # compatibility; only the explicit batch option changes routing.
        strategy = (
            sync_by_stock
            if dataset.name in {"daily", "adj_factor"} and ctx.ts_codes
            else STRATEGIES[dataset.strategy]
        )
        fetched, affected = strategy(ctx, dataset, start_date, end_date, ts_code)
    except Exception as exc:
        finish_sync_run(ctx.store, run_id, "failed", 0, 0, str(exc))
        raise
    finish_sync_run(ctx.store, run_id, "success", fetched, affected)
    return fetched, affected


def run_many(ctx: SyncContext, datasets: Iterable[Dataset], start_date: str, end_date: str, mode: str, ts_code: str | None = None) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """Run a group of datasets, collecting failures without aborting the group."""
    results: dict[str, tuple[int, int]] = {}
    failures: list[str] = []
    for dataset in datasets:
        try:
            fetched, affected = run_dataset(ctx, dataset, start_date, end_date, mode, ts_code)
            results[dataset.name] = (fetched, affected)
        except Exception as exc:  # noqa: BLE001 - fail-soft per dataset
            logger.error("%s failed: %s", dataset.name, exc)
            failures.append(f"{dataset.name}: {exc}")
    return results, failures
