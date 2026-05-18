from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


ROOT_DIR = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT_DIR / ".env")


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    tushare_token: str
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str
    mysql_charset: str
    mysql_connect_timeout: int
    sync_batch_size: int
    sync_retry_times: int
    sync_retry_backoff_seconds: float
    sync_request_interval_seconds: float
    sync_lookback_days: int
    enable_fallback: bool
    crawler_sleep_min_seconds: float
    crawler_sleep_max_seconds: float
    crawler_max_retries: int
    crawler_cooldown_seconds: float
    crawler_timeout_seconds: float
    crawler_proxy_url: str
    crawler_http_proxy: str
    crawler_https_proxy: str

    @property
    def mysql_url(self) -> str:
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            f"?charset={self.mysql_charset}"
        )


def get_settings() -> Settings:
    return Settings(
        tushare_token=os.getenv("TUSHARE_TOKEN", ""),
        mysql_host=os.getenv("MYSQL_HOST", "localhost"),
        mysql_port=_int_env("MYSQL_PORT", 3306),
        mysql_user=os.getenv("MYSQL_USER", "root"),
        mysql_password=os.getenv("MYSQL_PASSWORD", ""),
        mysql_database=os.getenv("MYSQL_DATABASE", "stock"),
        mysql_charset=os.getenv("MYSQL_CHARSET", "utf8mb4"),
        mysql_connect_timeout=_int_env("MYSQL_CONNECT_TIMEOUT", 10),
        sync_batch_size=_int_env("SYNC_BATCH_SIZE", 5000),
        sync_retry_times=_int_env("SYNC_RETRY_TIMES", 3),
        sync_retry_backoff_seconds=_float_env("SYNC_RETRY_BACKOFF_SECONDS", 2),
        sync_request_interval_seconds=_float_env("SYNC_REQUEST_INTERVAL_SECONDS", 0.35),
        sync_lookback_days=_int_env("SYNC_LOOKBACK_DAYS", 5),
        enable_fallback=_bool_env("ENABLE_FALLBACK", True),
        crawler_sleep_min_seconds=_float_env("CRAWLER_SLEEP_MIN_SECONDS", 1.5),
        crawler_sleep_max_seconds=_float_env("CRAWLER_SLEEP_MAX_SECONDS", 3.0),
        crawler_max_retries=_int_env("CRAWLER_MAX_RETRIES", 3),
        crawler_cooldown_seconds=_float_env("CRAWLER_COOLDOWN_SECONDS", 300),
        crawler_timeout_seconds=_float_env("CRAWLER_TIMEOUT_SECONDS", 20),
        crawler_proxy_url=os.getenv("CRAWLER_PROXY_URL", ""),
        crawler_http_proxy=os.getenv("CRAWLER_HTTP_PROXY", ""),
        crawler_https_proxy=os.getenv("CRAWLER_HTTPS_PROXY", ""),
    )
