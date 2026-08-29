"""Storage backends: sqlite (stdlib only) and mysql (pymysql).

Design:
  * Tables follow tushare API naming and structure 1:1. A table is created on
    first write with columns inferred from the pandas DataFrame dtypes, plus a
    UNIQUE constraint on the dataset's unique_columns for idempotent upserts.
  * If tushare later returns additional fields, ensure_table() runs
    ALTER TABLE ADD COLUMN automatically so old rows are preserved.
  * Upserts use:
      sqlite: INSERT ... ON CONFLICT(...) DO UPDATE SET ...
      mysql : INSERT ... ON DUPLICATE KEY UPDATE ...
    Both are idempotent and safe to re-run.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)


def _now_string() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _quote_ident(driver: str, name: str) -> str:
    if driver == "mysql":
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def _sql_type(driver: str, dtype: str) -> str:
    """Map a pandas dtype name to a portable SQL column type."""
    if dtype == "int64":
        return "BIGINT"
    if dtype == "float64":
        return "DOUBLE"
    if dtype == "bool":
        return "TINYINT"
    # object / datetime64 / others -> text
    return "TEXT" if driver == "sqlite" else "LONGTEXT"


class RowStore:
    """Minimal interface shared by SqliteStore and MysqlStore."""

    driver: str = ""

    def connect(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        raise NotImplementedError

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        raise NotImplementedError

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> tuple | None:
        raise NotImplementedError

    def commit(self) -> None:
        raise NotImplementedError

    def lastrowid(self) -> int | None:
        raise NotImplementedError

    # --- shared behaviour -------------------------------------------------

    def column_types(self, table: str) -> dict[str, str]:
        """Return {column_name -> sql_type} for an existing table."""
        raise NotImplementedError

    def ensure_table(
        self,
        table: str,
        columns: Iterable[str],
        unique_columns: Sequence[str],
        dtypes: dict[str, str],
    ) -> None:
        columns = list(columns)
        unique_columns = list(unique_columns)
        existing = self.column_types(table)
        if not existing:
            self._create_table(table, columns, unique_columns, dtypes)
            return
        # add missing columns (tushare schema may evolve)
        for col in columns:
            if col not in existing:
                self.execute(
                    f"ALTER TABLE {_quote_ident(self.driver, table)} "
                    f"ADD COLUMN {_quote_ident(self.driver, col)} {_sql_type(self.driver, dtypes.get(col, 'object'))}"
                )
        self.commit()

    def upsert(
        self,
        table: str,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        unique_columns: Sequence[str],
        batch_size: int,
    ) -> int:
        if not rows:
            return 0
        unique_set = set(unique_columns)
        update_cols = [c for c in columns if c not in unique_set]
        # Keep per-statement variable count under safe limits (sqlite/mysql both
        # cap placeholders; adapt sub-batch size to the column count).
        per_exec = max(800 // len(columns), 1)
        step = max(min(batch_size, per_exec), 1)
        affected = 0
        for i in range(0, len(rows), step):
            chunk = rows[i : i + step]
            affected += self._upsert_chunk(table, columns, chunk, unique_columns, update_cols)
        self.commit()
        return affected

    def _create_table(
        self,
        table: str,
        columns: Sequence[str],
        unique_columns: Sequence[str],
        dtypes: dict[str, str],
    ) -> None:
        col_defs = [
            f"{_quote_ident(self.driver, c)} {_sql_type(self.driver, dtypes.get(c, 'object'))}"
            for c in columns
        ]
        if unique_columns:
            uniq = ",".join(_quote_ident(self.driver, c) for c in unique_columns)
            if self.driver == "mysql":
                col_defs.append(f"UNIQUE KEY `uk_{table}` ({uniq})")
            else:
                col_defs.append(f"UNIQUE ({uniq})")
        ddl = (
            f"CREATE TABLE IF NOT EXISTS {_quote_ident(self.driver, table)} "
            f"({', '.join(col_defs)})"
        )
        if self.driver == "mysql":
            ddl += " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        self.execute(ddl)
        self.commit()
        logger.info("created table %s (%s cols, unique=%s)", table, len(columns), unique_columns or "-")

    def _upsert_chunk(
        self,
        table: str,
        columns: Sequence[str],
        chunk: Sequence[Sequence[Any]],
        unique_columns: Sequence[str],
        update_cols: Sequence[str],
    ) -> int:  # pragma: no cover - interface
        raise NotImplementedError


class SqliteStore(RowStore):
    driver = "sqlite"

    def __init__(self, path: str) -> None:
        import sqlite3

        self._path = path
        self._conn = sqlite3.connect(path, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    def connect(self) -> None:
        pass

    def close(self) -> None:
        self._conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        cur = self._conn.execute(sql, params)
        return cur

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> tuple | None:
        return self._conn.execute(sql, params).fetchone()

    def commit(self) -> None:
        self._conn.commit()

    def lastrowid(self) -> int | None:
        return self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def column_types(self, table: str) -> dict[str, str]:
        rows = self.query(f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")')
        return {r[1]: r[2].upper() for r in rows}

    def _upsert_chunk(
        self,
        table: str,
        columns: Sequence[str],
        chunk: Sequence[Sequence[Any]],
        unique_columns: Sequence[str],
        update_cols: Sequence[str],
    ) -> int:
        col_list = ",".join(_quote_ident(self.driver, c) for c in columns)
        placeholders = ",".join("?" for _ in columns)
        sql = f"INSERT INTO {_quote_ident(self.driver, table)} ({col_list}) VALUES ({placeholders})"
        if update_cols:
            uniq = ",".join(_quote_ident(self.driver, c) for c in unique_columns)
            setters = ",".join(
                f"{_quote_ident(self.driver, c)} = excluded.{_quote_ident(self.driver, c)}"
                for c in update_cols
            )
            sql += f" ON CONFLICT ({uniq}) DO UPDATE SET {setters}"
        cur = self._conn.executemany(sql, [list(r) for r in chunk])
        rc = cur.rowcount
        return rc if rc and rc > 0 else 0


class MysqlStore(RowStore):
    driver = "mysql"

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        charset: str,
        connect_timeout: int,
    ) -> None:
        import pymysql

        self._conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            charset=charset,
            connect_timeout=connect_timeout,
            ssl_disabled=True,
            autocommit=False,
        )

    def connect(self) -> None:
        pass

    def close(self) -> None:
        self._conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        cur = self._conn.cursor()
        cur.execute(sql, list(params))
        return cur

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        cur = self._conn.cursor()
        cur.execute(sql, list(params))
        return list(cur.fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> tuple | None:
        cur = self._conn.cursor()
        cur.execute(sql, list(params))
        return cur.fetchone()

    def commit(self) -> None:
        self._conn.commit()

    def lastrowid(self) -> int | None:
        return self._conn.cursor().lastrowid

    def column_types(self, table: str) -> dict[str, str]:
        rows = self.query(
            "SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table,),
        )
        return {r[0]: r[1].upper() for r in rows}

    def _upsert_chunk(
        self,
        table: str,
        columns: Sequence[str],
        chunk: Sequence[Sequence[Any]],
        unique_columns: Sequence[str],
        update_cols: Sequence[str],
    ) -> int:
        col_list = ",".join(_quote_ident(self.driver, c) for c in columns)
        placeholders = ",".join("%s" for _ in columns)
        sql = f"INSERT INTO {_quote_ident(self.driver, table)} ({col_list}) VALUES ({placeholders})"
        if update_cols:
            setters = ",".join(
                f"{_quote_ident(self.driver, c)} = VALUES({_quote_ident(self.driver, c)})"
                for c in update_cols
            )
            sql += f" ON DUPLICATE KEY UPDATE {setters}"
        cur = self._conn.cursor()
        cur.executemany(sql, [list(r) for r in chunk])
        return cur.rowcount or 0


def create_store(settings: Any) -> RowStore:
    if settings.db_driver == "mysql":
        logger.info("storage backend: mysql (%s:%s/%s)", settings.mysql_host, settings.mysql_port, settings.mysql_database)
        return MysqlStore(
            settings.mysql_host,
            settings.mysql_port,
            settings.mysql_user,
            settings.mysql_password,
            settings.mysql_database,
            settings.mysql_charset,
            settings.mysql_connect_timeout,
        )
    logger.info("storage backend: sqlite (%s)", settings.db_sqlite_path)
    from pathlib import Path

    Path(settings.db_sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    return SqliteStore(settings.db_sqlite_path)