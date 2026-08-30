from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Safari/605.1.15",
)


@dataclass
class CrawlerGuard:
    sleep_min: float
    sleep_max: float
    max_retries: int
    cooldown_seconds: float
    _cooldown_until: float = 0

    def is_available(self) -> bool:
        return time.time() >= self._cooldown_until

    def mark_rate_limited(self, reason: str) -> None:
        self._cooldown_until = time.time() + self.cooldown_seconds
        logger.warning("Crawler source cooling down for %.0fs: %s", self.cooldown_seconds, reason)

    def before_request(self) -> dict[str, str]:
        if self.sleep_max > 0:
            time.sleep(random.uniform(max(0, self.sleep_min), max(self.sleep_min, self.sleep_max)))
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://quote.eastmoney.com/",
        }


def is_rate_limit_error(exc: Exception) -> bool:
    message = _error_chain_text(exc)
    return any(
        marker in message
        for marker in ("403", "429", "forbidden", "too many requests", "blocked", "banned", "rate limit", "频率", "限制")
    )


def is_remote_disconnect_error(exc: Exception) -> bool:
    message = _error_chain_text(exc)
    return any(
        marker in message
        for marker in (
            "remotedisconnected",
            "remote end closed connection",
            "connection aborted",
            "connection reset",
            "connection refused",
            "max retries exceeded",
            "permissionerror",
            "winerror 10013",
            "proxyerror",
        )
    )


def is_crawler_block_error(exc: Exception) -> bool:
    return is_rate_limit_error(exc) or is_remote_disconnect_error(exc)


def _error_chain_text(exc: Exception) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        parts.append(type(current).__name__)
        parts.append(str(current))
        current = current.__cause__ or current.__context__
    return " ".join(parts).lower()
