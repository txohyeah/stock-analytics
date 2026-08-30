from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import requests

from app.config import Settings
from app.providers.rate_limit import CrawlerGuard, is_crawler_block_error, is_rate_limit_error

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FallbackResult:
    frame: pd.DataFrame
    source: str
    reason: str


class FallbackProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.guard = CrawlerGuard(
            sleep_min=settings.crawler_sleep_min_seconds,
            sleep_max=settings.crawler_sleep_max_seconds,
            max_retries=settings.crawler_max_retries,
            cooldown_seconds=settings.crawler_cooldown_seconds,
        )
        self.session = requests.Session()
        self.proxies = build_proxy_config(settings)
        if self.proxies:
            logger.info("Eastmoney fallback proxy enabled: %s", redact_proxy_url(next(iter(self.proxies.values()))))

    def fetch_trade_cal(self, start_date: str, end_date: str, reason: str) -> FallbackResult:
        frame = _build_trade_calendar(start_date, end_date)
        return FallbackResult(frame=frame, source="exchange_calendars", reason=reason)

    def fetch_daily_by_trade_date(self, trade_date: str, ts_code: str | None, stock_codes: list[str], reason: str) -> FallbackResult:
        return self._fetch_kline_by_trade_date("daily", trade_date, ts_code, stock_codes, reason)

    def fetch_index_daily_by_trade_date(self, trade_date: str, ts_code: str | None, index_codes: list[str], reason: str) -> FallbackResult:
        return self._fetch_kline_by_trade_date("index_daily", trade_date, ts_code, index_codes, reason)

    def _fetch_kline_by_trade_date(
        self,
        dataset: str,
        trade_date: str,
        ts_code: str | None,
        codes: list[str],
        reason: str,
    ) -> FallbackResult:
        if not self.guard.is_available():
            logger.warning("Eastmoney fallback is in cooldown; skip %s trade_date=%s", dataset, trade_date)
            return FallbackResult(pd.DataFrame(), "eastmoney", "cooldown")

        target_codes = [ts_code] if ts_code else codes
        frames: list[pd.DataFrame] = []
        for code in target_codes:
            try:
                frame = self._fetch_eastmoney_kline(code, trade_date, trade_date)
            except Exception as exc:  # noqa: BLE001 - fallback should keep the sync loop resilient
                if is_crawler_block_error(exc):
                    self.guard.mark_rate_limited(str(exc))
                    break
                logger.warning("Eastmoney %s fallback failed code=%s date=%s: %s", dataset, code, trade_date, exc)
                continue
            if not frame.empty:
                frames.append(frame)

        result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        return FallbackResult(result, "eastmoney", reason)

    def _fetch_eastmoney_kline(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        code = normalize_ts_code(ts_code)
        secid = eastmoney_secid(code)
        last_error: Exception | None = None
        for attempt in range(1, self.settings.crawler_max_retries + 1):
            try:
                headers = self.guard.before_request()
                response = self.session.get(
                    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                    params={
                        "secid": secid,
                        "fields1": "f1,f2,f3,f4,f5,f6",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                        "klt": "101",
                        "fqt": "0",
                        "beg": start_date,
                        "end": end_date,
                    },
                    headers=headers,
                    proxies=self.proxies or None,
                    timeout=self.settings.crawler_timeout_seconds,
                )
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
                data = payload.get("data") or {}
                klines = data.get("klines") or []
                return parse_eastmoney_klines(ts_code, klines)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if is_rate_limit_error(exc) or is_crawler_block_error(exc):
                    raise
                if attempt < self.settings.crawler_max_retries:
                    time_to_sleep = min(2 ** attempt, 8)
                    logger.warning(
                        "Eastmoney daily retry code=%s attempt=%s/%s after %ss: %s",
                        ts_code,
                        attempt,
                        self.settings.crawler_max_retries,
                        time_to_sleep,
                        exc,
                    )
                    import time

                    time.sleep(time_to_sleep)
        if last_error and is_crawler_block_error(last_error):
            raise RuntimeError(f"Eastmoney kline blocked or disconnected for {ts_code}") from last_error
        raise RuntimeError(f"Eastmoney kline failed for {ts_code}") from last_error


def build_proxy_config(settings: Settings) -> dict[str, str]:
    if settings.crawler_proxy_url:
        return {"http": settings.crawler_proxy_url, "https": settings.crawler_proxy_url}

    proxies: dict[str, str] = {}
    if settings.crawler_http_proxy:
        proxies["http"] = settings.crawler_http_proxy
    if settings.crawler_https_proxy:
        proxies["https"] = settings.crawler_https_proxy
    return proxies


def redact_proxy_url(proxy_url: str) -> str:
    parsed = urlsplit(proxy_url)
    if not parsed.username:
        return proxy_url
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, f"***:***@{host}", parsed.path, parsed.query, parsed.fragment))


def _build_trade_calendar(start_date: str, end_date: str) -> pd.DataFrame:
    days = list_calendar_days(start_date, end_date)
    open_dates = _xshg_open_dates(start_date, end_date)
    rows = []
    previous_trade_date: str | None = None
    for day in days:
        is_open = 1 if day in open_dates else 0
        rows.append(
            {
                "exchange": "SSE",
                "cal_date": day,
                "is_open": is_open,
                "pretrade_date": previous_trade_date,
            }
        )
        if is_open:
            previous_trade_date = day
    return pd.DataFrame(rows)


def _xshg_open_dates(start_date: str, end_date: str) -> set[str]:
    try:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XSHG")
        sessions = calendar.sessions_in_range(
            pd.Timestamp(datetime.strptime(start_date, "%Y%m%d")),
            pd.Timestamp(datetime.strptime(end_date, "%Y%m%d")),
        )
        return {session.strftime("%Y%m%d") for session in sessions}
    except Exception as exc:  # noqa: BLE001
        logger.warning("exchange_calendars unavailable, fallback to weekday calendar: %s", exc)
        return {
            day
            for day in list_calendar_days(start_date, end_date)
            if datetime.strptime(day, "%Y%m%d").weekday() < 5
        }


def list_calendar_days(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")
    days = []
    current = start
    while current <= end:
        days.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return days


def normalize_ts_code(ts_code: str) -> str:
    raw = ts_code.strip().upper()
    if "." in raw:
        return raw
    if raw.startswith(("6", "5", "9")):
        return f"{raw}.SH"
    if raw.startswith(("0", "2", "3")):
        return f"{raw}.SZ"
    if raw.startswith(("4", "8")):
        return f"{raw}.BJ"
    return raw


def eastmoney_secid(ts_code: str) -> str:
    code, suffix = ts_code.split(".", 1)
    market = {"SH": "1", "SZ": "0", "BJ": "0"}.get(suffix)
    if market is None:
        raise ValueError(f"Unsupported A-share code for Eastmoney fallback: {ts_code}")
    return f"{market}.{code}"


def parse_eastmoney_klines(ts_code: str, klines: list[str]) -> pd.DataFrame:
    rows = []
    normalized = normalize_ts_code(ts_code)
    for item in klines:
        parts = item.split(",")
        if len(parts) < 11:
            continue
        trade_date = parts[0].replace("-", "")
        change = _to_float(parts[9])
        close = _to_float(parts[2])
        pre_close = close - change if close is not None and change is not None else None
        amount = _to_float(parts[6])
        rows.append(
            {
                "ts_code": normalized,
                "trade_date": trade_date,
                "open": _to_float(parts[1]),
                "close": close,
                "high": _to_float(parts[3]),
                "low": _to_float(parts[4]),
                "pre_close": pre_close,
                "change": change,
                "pct_chg": _to_float(parts[8]),
                "vol": _to_float(parts[5]),
                "amount": amount / 1000 if amount is not None else None,
            }
        )
    return pd.DataFrame(rows)


def _to_float(value: str) -> float | None:
    if value in {"", "-", "None", "null"}:
        return None
    return float(value)
