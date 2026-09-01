"""A股分析存储层：sqlite 数据访问（表名与结构跟随 tushare 接口）。

从 stock-research data.py 的 MySQLRepository 迁移并改写：
- 表名直接使用 tushare 原生（daily / daily_basic / stock_basic / trade_cal / index_daily ...）
- 摆脱 MySQL/SQLAlchemy，仅依赖 Python 标准库 sqlite3 + pandas
- fina_indicator 按需同步（sqlite 无表或空数据时抛 DataInsufficientError 并给出同步命令）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Protocol

import pandas as pd

from .errors import DatabaseConnectionError, DataInsufficientError, UserInputError
from .models import StockCode

REQUIRED_TABLES = ("daily", "trade_cal", "stock_basic")

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "stock.db"


@dataclass(frozen=True)
class StockHistory:
    meta: dict[str, object]
    bars: pd.DataFrame


class StockDataRepository(Protocol):
    def validate(self) -> None: ...
    def resolve_trade_date(self, requested: str) -> str: ...
    def resolve_start_date(self, trade_date: str, lookback_days: int) -> str: ...
    def resolve_previous_trade_date(self, trade_date: str) -> str | None: ...
    def list_open_trade_dates(self, start_date: str, end_date: str) -> list[str]: ...
    def fetch_histories_batch(self, ts_codes: list[str], start_date: str, trade_date: str) -> dict[str, StockHistory]: ...
    def fetch_universe_codes(self, universe: str, **kwargs) -> list[StockCode]: ...


class SqliteRepository:
    """Sqlite-backed repository over tushare-native tables."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def validate(self) -> None:
        if not self.db_path.exists():
            raise DatabaseConnectionError(
                f"Sqlite database not found: {self.db_path}",
                hint="Set DB_SQLITE_PATH or pass --database with a valid .db path",
            )
        try:
            with self._connect() as conn:
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        missing = [table for table in REQUIRED_TABLES if table not in tables]
        if missing:
            raise UserInputError(
                f"Missing required tables: {', '.join(missing)}",
                hint="Run sync first: ./venv/bin/python -m app.cli sync market",
            )

    def resolve_trade_date(self, requested: str) -> str:
        requested = str(requested)
        if requested != "latest" and not re.fullmatch(r"\d{8}", requested):
            raise UserInputError(f"Invalid trade date format: {requested}")
        today = datetime.now().strftime("%Y%m%d")
        target = today if requested == "latest" else requested
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT MAX(cal_date) AS d FROM trade_cal WHERE cal_date <= ? AND is_open = 1",
                    (target,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        if not row or not row["d"]:
            raise UserInputError(f"No open trade date found on or before: {target}")
        return str(row["d"])

    def resolve_start_date(self, trade_date: str, lookback_days: int) -> str:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT MIN(cal_date) AS d FROM (
                      SELECT cal_date FROM trade_cal
                      WHERE cal_date <= ? AND is_open = 1
                      ORDER BY cal_date DESC LIMIT ?
                    )
                    """,
                    (trade_date, lookback_days),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return str(row["d"]) if row and row["d"] else trade_date

    def resolve_previous_trade_date(self, trade_date: str) -> str | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT MAX(cal_date) AS d FROM trade_cal WHERE cal_date < ? AND is_open = 1",
                    (trade_date,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return str(row["d"]) if row and row["d"] else None

    def list_open_trade_dates(self, start_date: str, end_date: str) -> list[str]:
        start_date = _compact_date(start_date)
        end_date = _compact_date(end_date)
        if start_date > end_date:
            raise UserInputError("Start date must not be after end date")
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT cal_date FROM trade_cal
                    WHERE cal_date BETWEEN ? AND ? AND is_open = 1
                    ORDER BY cal_date
                    """,
                    (start_date, end_date),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return [str(row["cal_date"]) for row in rows]

    def fetch_histories(
        self,
        ts_codes: list[str],
        trade_date: str,
        lookback_days: int,
        *,
        batch_size: int = 500,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, StockHistory]:
        if not ts_codes:
            return {}
        if batch_size <= 0:
            raise UserInputError("--batch-size must be greater than 0")
        start_date = self.resolve_start_date(trade_date, lookback_days)
        histories: dict[str, StockHistory] = {}
        batches = list(_chunks(ts_codes, batch_size))
        total_batches = len(batches)
        for idx, batch in enumerate(batches, start=1):
            if progress:
                progress(f"fetching histories batch {idx}/{total_batches} ({len(batch)} stocks)")
            histories.update(self.fetch_histories_batch(batch, start_date, trade_date))
        return histories

    def fetch_histories_batch(
        self,
        ts_codes: list[str],
        start_date: str,
        trade_date: str,
    ) -> dict[str, StockHistory]:
        if not ts_codes:
            return {}
        metas = self._fetch_meta_many(ts_codes)
        bars_by_code = self._fetch_bars_many(ts_codes, start_date, trade_date)
        return {
            ts_code: StockHistory(
                meta=metas.get(ts_code) or {"ts_code": ts_code, "symbol": ts_code.split(".")[0], "name": ts_code},
                bars=bars_by_code.get(ts_code, pd.DataFrame()),
            )
            for ts_code in ts_codes
        }

    def fetch_universe_codes(
        self,
        universe: str,
        *,
        include_bj: bool = False,
        include_st: bool = False,
        trade_date: str | None = None,
        start_date: str | None = None,
        min_history_days: int | None = None,
        min_circ_mv: float | None = None,
        max_price: float | None = None,
    ) -> list[StockCode]:
        if universe != "a_share":
            raise UserInputError(f"Unsupported universe: {universe}")

        params: list[Any] = []
        joins = ""
        filters = [
            "(s.list_status = 'L' OR s.list_status IS NULL OR s.list_status = '')",
            "(s.ts_code LIKE '%.SH' OR s.ts_code LIKE '%.SZ' OR s.ts_code LIKE '%.BJ')",
        ]
        if not include_bj:
            filters.append("s.ts_code NOT LIKE '%.BJ'")
        if trade_date:
            joins += """
                JOIN daily latest
                  ON latest.ts_code = s.ts_code AND latest.trade_date = ?
            """
            params.append(trade_date)
        if start_date and trade_date and min_history_days:
            joins += f"""
                JOIN (
                  SELECT ts_code, COUNT(*) AS bar_count
                  FROM daily
                  WHERE trade_date BETWEEN ? AND ?
                  GROUP BY ts_code
                  HAVING COUNT(*) >= ?
                ) history
                  ON history.ts_code = s.ts_code
            """
            params.extend([start_date, trade_date, int(min_history_days)])
        if min_circ_mv is not None:
            if not trade_date:
                raise UserInputError("--min-circ-mv-e requires a resolved trade date")
            joins += """
                JOIN daily_basic latest_basic
                  ON latest_basic.ts_code = s.ts_code AND latest_basic.trade_date = ?
            """
            params.append(trade_date)
            filters.append("latest_basic.circ_mv >= ?")
            params.append(float(min_circ_mv))
        if max_price is not None:
            if not trade_date:
                raise UserInputError("--max-price requires a resolved trade date")
            filters.append("latest.close <= ?")
            params.append(float(max_price))
        if not include_st:
            filters.extend(["name NOT LIKE 'ST%'", "name NOT LIKE '*ST%'", "name NOT LIKE '%退%'"])

        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT s.ts_code, s.symbol
                    FROM stock_basic s
                    {joins}
                    WHERE {" AND ".join(filters)}
                    ORDER BY s.ts_code
                    """,
                    params,
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return [
            StockCode(raw=str(row["ts_code"]), code=str(row["symbol"]), ts_code=str(row["ts_code"]))
            for row in rows
            if row["ts_code"] and row["symbol"]
        ]

    def fetch_stock_basic_info(self, ts_code: str) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT ts_code, symbol, name, area, industry, fullname, market, exchange, list_date
                    FROM stock_basic WHERE ts_code = ?
                    """,
                    (ts_code,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return dict(row) if row else None

    def fetch_stocks_by_name(self, name: str, limit: int = 10) -> list[dict[str, Any]]:
        query = name.strip()
        if not query:
            raise UserInputError("--name must not be empty")
        if limit <= 0:
            raise UserInputError("--limit must be greater than 0")
        pattern = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT ts_code, symbol, name, fullname, industry, market, exchange, list_status,
                           CASE
                               WHEN name = ? OR fullname = ? THEN 'exact'
                               WHEN name LIKE ? ESCAPE '!' OR fullname LIKE ? ESCAPE '!' THEN 'prefix'
                               ELSE 'contains'
                           END AS match_type
                    FROM stock_basic
                    WHERE name = ? OR fullname = ?
                       OR name LIKE ? ESCAPE '!' OR fullname LIKE ? ESCAPE '!'
                    ORDER BY
                        CASE
                            WHEN name = ? OR fullname = ? THEN 0
                            WHEN name LIKE ? ESCAPE '!' OR fullname LIKE ? ESCAPE '!' THEN 1
                            ELSE 2
                        END,
                        CASE WHEN list_status = 'L' THEN 0 ELSE 1 END,
                        ts_code
                    LIMIT ?
                    """,
                    (query, query, f"{pattern}%", f"{pattern}%", query, query,
                     f"%{pattern}%", f"%{pattern}%", query, query, f"{pattern}%", f"{pattern}%", limit),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return [dict(row) for row in rows]

    def count_stocks_by_exact_name(self, name: str) -> int:
        query = name.strip()
        if not query:
            raise UserInputError("--name must not be empty")
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM stock_basic WHERE name = ? OR fullname = ?",
                    (query, query),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return int(row["c"] or 0)

    def fetch_fina_indicator(self, ts_code: str, period: str = "latest") -> dict[str, Any] | None:
        if period != "latest" and not _valid_date_value(period):
            raise UserInputError("--period must be latest, YYYYMMDD, or YYYY-MM-DD")
        table = "fina_indicator"
        try:
            with self._connect() as conn:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    raise DataInsufficientError(
                        f"{table} 表未同步（sqlite 无此表）",
                        hint=(
                            f"先按需同步：./venv/bin/python -m app.cli sync fina_indicator "
                            f"--ts-codes {ts_code}"
                        ),
                    )
                if period == "latest":
                    row = conn.execute(
                        f"SELECT * FROM {table} WHERE ts_code = ? ORDER BY end_date DESC LIMIT 1",
                        (ts_code,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        f"SELECT * FROM {table} WHERE ts_code = ? AND end_date = ? LIMIT 1",
                        (ts_code, _compact_date(period)),
                    ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return dict(row) if row else None

    def fetch_fina_indicator_recent(self, ts_code: str, n: int) -> list[dict[str, Any]]:
        """取最近 n 个报告期（end_date 倒序），n<=0 报错。用于纵向看增速/趋势。"""
        if n <= 0:
            raise UserInputError("--recent must be >= 1")
        table = "fina_indicator"
        try:
            with self._connect() as conn:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    raise DataInsufficientError(
                        f"{table} 表未同步（sqlite 无此表）",
                        hint=(
                            f"先按需同步：./venv/bin/python -m app.cli sync fina_indicator "
                            f"--ts-codes {ts_code}"
                        ),
                    )
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE ts_code = ? ORDER BY end_date DESC LIMIT ?",
                    (ts_code, n),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return [dict(row) for row in rows]

    def fetch_daily_basic(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        start_date = _compact_date(start_date) if start_date else None
        end_date = _compact_date(end_date) if end_date else None
        params: list[Any] = [ts_code]
        filters = ["ts_code = ?"]
        if start_date:
            filters.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            filters.append("trade_date <= ?")
            params.append(end_date)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT ts_code, trade_date, close, turnover_rate, turnover_rate_f,
                           volume_ratio, pe, pe_ttm, pb, ps, total_mv, circ_mv
                    FROM daily_basic
                    WHERE {" AND ".join(filters)}
                    ORDER BY trade_date
                    """,
                    params,
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return [dict(row) for row in rows]

    def fetch_stock_history(
        self,
        ts_codes: list[str],
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        if not ts_codes:
            raise UserInputError("--code must include at least one stock code")
        start_date = _compact_date(start_date) if start_date else None
        end_date = _compact_date(end_date) if end_date else None
        placeholders = ", ".join(["?"] * len(ts_codes))
        params: list[Any] = list(ts_codes)
        filters = [f"d.ts_code IN ({placeholders})"]
        if start_date:
            filters.append("d.trade_date >= ?")
            params.append(start_date)
        if end_date:
            filters.append("d.trade_date <= ?")
            params.append(end_date)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT
                      d.ts_code, s.symbol, s.name,
                      d.trade_date, d.open, d.high, d.low, d.close,
                      d.pre_close, d.pct_chg, d.vol, d.amount
                    FROM daily d
                    LEFT JOIN stock_basic s ON s.ts_code = d.ts_code
                    WHERE {" AND ".join(filters)}
                    ORDER BY d.ts_code, d.trade_date
                    """,
                    params,
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return [dict(row) for row in rows]

    def list_industries(self) -> list[str]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT industry FROM stock_basic
                    WHERE industry IS NOT NULL AND industry != ''
                    ORDER BY industry
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return [str(row["industry"]) for row in rows if row["industry"]]

    def fetch_stocks_by_industry(self, industry: str) -> list[dict[str, Any]]:
        if not industry.strip():
            raise UserInputError("--name must not be empty")
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT ts_code, symbol, name, area, market
                    FROM stock_basic WHERE industry = ?
                    ORDER BY ts_code
                    """,
                    (industry.strip(),),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return [dict(row) for row in rows]

    def fetch_index_daily(
        self,
        index_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        code = _normalize_index_code(index_code)
        start_date = _compact_date(start_date) if start_date else None
        end_date = _compact_date(end_date) if end_date else None
        params: list[Any] = [code]
        filters = ["ts_code = ?"]
        if start_date:
            filters.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            filters.append("trade_date <= ?")
            params.append(end_date)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT ts_code, trade_date, open, high, low, close, pre_close,
                           "change", pct_chg, vol, amount
                    FROM index_daily
                    WHERE {" AND ".join(filters)}
                    ORDER BY trade_date
                    """,
                    params,
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return [dict(row) for row in rows]

    def fetch_market_daily(self, trade_date: str) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT
                      d.ts_code, d.trade_date, d.open, d.high, d.low, d.close,
                      d.pre_close, d.pct_chg, d.vol, d.amount,
                      s.symbol, s.name, s.industry, s.market, s.exchange
                    FROM daily d
                    LEFT JOIN stock_basic s ON s.ts_code = d.ts_code
                    WHERE d.trade_date = ?
                      AND (d.ts_code LIKE '%.SH' OR d.ts_code LIKE '%.SZ' OR d.ts_code LIKE '%.BJ')
                    ORDER BY d.ts_code
                    """,
                    (trade_date,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return [dict(row) for row in rows]

    def fetch_market_period_returns(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        start_date = _compact_date(start_date)
        end_date = _compact_date(end_date)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT
                      start_day.ts_code,
                      s.name, s.industry, s.market, s.exchange,
                      start_day.pre_close AS start_pre_close,
                      end_day.close AS end_close,
                      ((end_day.close / start_day.pre_close) - 1) * 100 AS period_return
                    FROM daily start_day
                    JOIN daily end_day
                      ON end_day.ts_code = start_day.ts_code
                     AND end_day.trade_date = ?
                    LEFT JOIN stock_basic s ON s.ts_code = start_day.ts_code
                    WHERE start_day.trade_date = ?
                      AND start_day.pre_close IS NOT NULL
                      AND start_day.pre_close > 0
                      AND (start_day.ts_code LIKE '%.SH'
                           OR start_day.ts_code LIKE '%.SZ'
                           OR start_day.ts_code LIKE '%.BJ')
                    ORDER BY start_day.ts_code
                    """,
                    (end_date, start_date),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return [dict(row) for row in rows]

    def _fetch_meta_many(self, ts_codes: list[str]) -> dict[str, dict[str, object]]:
        if not ts_codes:
            return {}
        placeholders = ", ".join(["?"] * len(ts_codes))
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT ts_code, symbol, name, industry, market, exchange, list_status
                    FROM stock_basic WHERE ts_code IN ({placeholders})
                    """,
                    ts_codes,
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return {str(row["ts_code"]): dict(row) for row in rows}

    def _fetch_bars_many(self, ts_codes: list[str], start_date: str, trade_date: str) -> dict[str, pd.DataFrame]:
        if not ts_codes:
            return {}
        placeholders = ", ".join(["?"] * len(ts_codes))
        params: list[Any] = list(ts_codes) + [start_date, trade_date]
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT
                      d.ts_code, d.trade_date, d.open, d.high, d.low, d.close,
                      d.pre_close, d.pct_chg, d.vol, d.amount,
                      b.turnover_rate, b.circ_mv
                    FROM daily d
                    LEFT JOIN daily_basic b
                      ON b.ts_code = d.ts_code AND b.trade_date = d.trade_date
                    WHERE d.ts_code IN ({placeholders})
                      AND d.trade_date BETWEEN ? AND ?
                    ORDER BY d.ts_code, d.trade_date
                    """,
                    params,
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        if not rows:
            return {}
        data = pd.DataFrame([dict(row) for row in rows])
        return {str(ts_code): frame.reset_index(drop=True) for ts_code, frame in data.groupby("ts_code", sort=False)}

    def _fetch_meta(self, ts_code: str) -> dict[str, object]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT ts_code, symbol, name, industry, market, exchange, list_status
                    FROM stock_basic WHERE ts_code = ?
                    """,
                    (ts_code,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return dict(row) if row else {"ts_code": ts_code, "symbol": ts_code.split(".")[0], "name": ts_code}

    def _fetch_bars(self, ts_code: str, trade_date: str, lookback_days: int) -> pd.DataFrame:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM (
                      SELECT
                        d.ts_code, d.trade_date, d.open, d.high, d.low, d.close,
                        d.pre_close, d.pct_chg, d.vol, d.amount,
                        b.turnover_rate, b.circ_mv
                      FROM daily d
                      LEFT JOIN daily_basic b
                        ON b.ts_code = d.ts_code AND b.trade_date = d.trade_date
                      WHERE d.ts_code = ? AND d.trade_date <= ?
                      ORDER BY d.trade_date DESC
                      LIMIT ?
                    ) x
                    ORDER BY trade_date ASC
                    """,
                    (ts_code, trade_date, lookback_days),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseConnectionError(str(exc)) from exc
        return pd.DataFrame([dict(row) for row in rows])


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _compact_date(value: str) -> str:
    if not _valid_date_value(value):
        raise UserInputError("Date must use YYYYMMDD or YYYY-MM-DD format")
    return value.replace("-", "")


def _valid_date_value(value: str) -> bool:
    return bool(re.fullmatch(r"\d{8}", value) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))


def _normalize_index_code(index_code: str) -> str:
    code = index_code.strip().upper()
    if not code:
        raise UserInputError("--code must not be empty")
    if "." in code:
        return code
    if code.startswith("0"):
        return f"{code}.SH"
    if code.startswith("3"):
        return f"{code}.SZ"
    return code