from __future__ import annotations

import time
import logging
from typing import Any

import pandas as pd

from app.config import Settings

logger = logging.getLogger(__name__)

# Tushare answers a permission or a bad-parameter problem with these wordings;
# both are deterministic, so retrying only burns quota.
_NO_PERMISSION_MARKERS = (
    "没有接口",
    "访问权限",
    "无权限",
    "permission",
    "必填参数",
    "参数错误",
    "invalid",
)

# Tushare rate-limits each interface per minute (2000 credits -> 200 calls/min
# for a given API) and reports it as "抱歉，您每分钟最多访问该接口200次".
_RATE_LIMIT_MARKERS = (
    "每分钟最多访问",
    "每分钟最多",
    "频率超限",
    "请求过于频繁",
    "too many requests",
    "rate limit",
    "ratelimit",
)


def is_rate_limit_error(error_text: str) -> bool:
    """True when *error_text* looks like a Tushare per-minute rate limit."""
    lowered = error_text.lower()
    return any(marker.lower() in lowered for marker in _RATE_LIMIT_MARKERS)


class TushareClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.tushare_token:
            raise RuntimeError("TUSHARE_TOKEN is required. Please set it in .env.")
        import tushare as ts

        self._pro = ts.pro_api(settings.tushare_token)
        self._retry_times = settings.sync_retry_times
        self._backoff_seconds = settings.sync_retry_backoff_seconds
        self._interval_seconds = settings.sync_request_interval_seconds
        self._rate_limit_wait_seconds = settings.sync_rate_limit_wait_seconds
        self._rate_limit_retry_times = settings.sync_rate_limit_retry_times

    def query(self, api_name: str, **params: Any) -> pd.DataFrame:
        """Query one Tushare interface, honouring the per-interface rate cap.

        Retries fall into two classes: a rate limit ("每分钟最多访问该接口N次")
        waits out the full minute window, every other transient failure uses the
        short escalating backoff. Rate-limit waits have their own budget so a
        throttled interface cannot silently eat the normal retry allowance.
        """
        cleaned_params = {key: value for key, value in params.items() if value not in (None, "")}
        last_error: Exception | None = None
        attempt = 1
        rate_limit_hits = 0

        while True:
            try:
                time.sleep(self._interval_seconds)
                logger.debug("Query Tushare api=%s params=%s", api_name, cleaned_params)
                result = self._pro.query(api_name, **cleaned_params)
                if result is None:
                    return pd.DataFrame()
                return result
            except Exception as exc:  # noqa: BLE001 - surface Tushare error after retries
                last_error = exc
                error_text = str(exc)

                if any(marker in error_text for marker in _NO_PERMISSION_MARKERS):
                    break

                if is_rate_limit_error(error_text):
                    if rate_limit_hits >= self._rate_limit_retry_times:
                        break
                    rate_limit_hits += 1
                    wait = self._rate_limit_wait_seconds
                    logger.warning(
                        "Tushare rate limited api=%s (%s/%s): %s. cooling down %ss "
                        "to clear the per-minute window",
                        api_name,
                        rate_limit_hits,
                        self._rate_limit_retry_times,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                if attempt >= self._retry_times:
                    break
                sleep_seconds = self._backoff_seconds * attempt
                logger.warning(
                    "Tushare query failed api=%s attempt=%s/%s: %s. retry in %ss",
                    api_name,
                    attempt,
                    self._retry_times,
                    exc,
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)
                attempt += 1

        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"Tushare query failed: {api_name}{detail}") from last_error
