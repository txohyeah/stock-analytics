"""Database helpers on top of RowStore (sqlite/mysql)."""
from __future__ import annotations

from typing import Iterable

import pandas as pd

from app.config import Settings
from app.storage import RowStore, create_store

SYNC_RUN_DDL = """
CREATE TABLE IF NOT EXISTS sync_run (
  id {id_type} PRIMARY KEY {auto_inc},
  dataset {str_type} NOT NULL,
  mode {str_type} NOT NULL,
  start_date {str_type},
  end_date {str_type},
  status {str_type} NOT NULL,
  started_at {str_type} NOT NULL,
  finished_at {str_type},
  fetched_rows {int_type} NOT NULL DEFAULT 0,
  affected_rows {int_type} NOT NULL DEFAULT 0,
  error_message TEXT
)
"""


def _sync_run_ddl(driver: str) -> str:
    if driver == "mysql":
        return SYNC_RUN_DDL.format(
            id_type="BIGINT", auto_inc="AUTO_INCREMENT",
            str_type="VARCHAR(64)", int_type="BIGINT",
        ) + " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    return SYNC_RUN_DDL.format(
        id_type="INTEGER", auto_inc="",
        str_type="TEXT", int_type="INTEGER",
    )


def get_store(settings: Settings) -> RowStore:
    store = create_store(settings)
    store.execute(_sync_run_ddl(store.driver))
    store.commit()
    return store


def read_trade_dates(store: RowStore, start_date: str, end_date: str) -> list[str]:
    table = '"trade_cal"'
    if store.driver == "mysql":
        table = "`trade_cal`"
    rows = store.query(
        f"SELECT cal_date FROM {table} "
        "WHERE cal_date BETWEEN %s AND %s AND is_open = 1 ORDER BY cal_date"
        if store.driver == "mysql"
        else f"SELECT cal_date FROM {table} "
        "WHERE cal_date BETWEEN ? AND ? AND is_open = 1 ORDER BY cal_date",
        (start_date, end_date),
    )
    return [str(r[0]) for r in rows]


def read_stock_codes(store: RowStore) -> list[str]:
    table = '"stock_basic"'
    if store.driver == "mysql":
        table = "`stock_basic`"
    rows = store.query(f"SELECT ts_code FROM {table} ORDER BY ts_code")
    return [str(r[0]) for r in rows]


def upsert_dataframe(
    store: RowStore,
    table_name: str,
    df: pd.DataFrame,
    unique_columns: Iterable[str],
    batch_size: int,
) -> int:
    """Ensure the table exists (schema inferred from tushare columns) and
    upsert the given DataFrame idempotently. Returns affected row count."""
    if df.empty:
        return 0

    unique_set = set(unique_columns)
    columns = list(df.columns)
    dtypes = {col: str(df[col].dtype) for col in columns}
    store.ensure_table(table_name, columns, unique_columns, dtypes)

    clean_df = df.astype(object).where(pd.notnull(df), None)
    rows: list[list] = []
    for record in clean_df.to_dict(orient="records"):
        cleaned = []
        for col in columns:
            value = record.get(col)
            if col in unique_set and value is None:
                value = ""
            if isinstance(value, pd.Timestamp):
                value = value.strftime("%Y-%m-%d")
            elif value is not None and not isinstance(value, (str, int, float, bool)):
                value = str(value)
            cleaned.append(value)
        rows.append(cleaned)

    return store.upsert(table_name, columns, rows, list(unique_columns), batch_size)