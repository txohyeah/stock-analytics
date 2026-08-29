#!/usr/bin/env python3
"""
将源表数据同步到 tushare_stock_data 的目标表。
映射关系：
  stock_history         -> stock_daily          (日线行情)
  stock_daily_basic     -> stock_daily_basic    (每日指标)
  stock_fina_indicator  -> fina_indicator       (财务指标)

用法：
  python3 sync_to_target_tables.py --start 20260101 --end 20260510 --ts-codes 600519.SH
  python3 sync_to_target_tables.py --start 20260101 --end 20260510
  python3 sync_to_target_tables.py --start 20260101 --end 20260510 --ts-codes 600519.SH --tables stock_daily
"""

import argparse
import json
from typing import Any

import pymysql

# MySQL reserved words that need backtick escaping
RESERVED = {"open", "high", "low", "close", "change"}

def esc(col: str) -> str:
    return f"`{col}`" if col.lower() in RESERVED else col


def load_env(path: str) -> dict:
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


def get_db_conn(env: dict):
    return pymysql.connect(
        host=env["MYSQL_HOST"],
        port=int(env["MYSQL_PORT"]),
        user=env["MYSQL_USER"],
        password=env["MYSQL_PASSWORD"],
        database=env["MYSQL_DATABASE"],
        charset=env.get("MYSQL_CHARSET", "utf8mb4"),
        connect_timeout=10,
    )


def sync_stock_daily(cur, start: str, end: str, ts_codes: list | None) -> dict:
    """stock_history (DATE) -> stock_daily (CHAR(8))"""
    sql = f"""
        INSERT INTO stock_daily
            (ts_code, trade_date, {esc('open')}, {esc('high')}, {esc('low')}, {esc('close')}, pre_close,
             {esc('change')}, pct_chg, vol, amount)
        SELECT
            ts_code,
            DATE_FORMAT(trade_date, '%%Y%%m%%d'),
            {esc('open')}, {esc('high')}, {esc('low')}, {esc('close')}, pre_close,
            {esc('change')}, pct_chg, vol, amount
        FROM stock_history
        WHERE trade_date BETWEEN %s AND %s
    """
    params: list = [start, end]

    if ts_codes:
        placeholders = ",".join(["%s"] * len(ts_codes))
        sql += f" AND ts_code IN ({placeholders})"
        params.extend(ts_codes)

    sql += f"""
        ON DUPLICATE KEY UPDATE
            {esc('open')} = VALUES({esc('open')}), {esc('high')} = VALUES({esc('high')}), {esc('low')} = VALUES({esc('low')}),
            {esc('close')} = VALUES({esc('close')}), pre_close = VALUES(pre_close),
            {esc('change')} = VALUES({esc('change')}), pct_chg = VALUES(pct_chg),
            vol = VALUES(vol), amount = VALUES(amount)
    """

    cur.execute(sql, params)
    return {"status": "success", "affected": cur.rowcount}


def sync_stock_daily_basic(cur, start: str, end: str, ts_codes: list | None) -> dict:
    """stock_daily_basic (DATE) -> stock_daily_basic (CHAR(8))"""
    sql = f"""
        INSERT INTO stock_daily_basic
            (ts_code, trade_date, {esc('close')}, turnover_rate, turnover_rate_f,
             volume_ratio, pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm,
             total_share, float_share, free_share, total_mv, circ_mv)
        SELECT
            ts_code,
            DATE_FORMAT(trade_date, '%%Y%%m%%d'),
            {esc('close')}, turnover_rate, turnover_rate_f,
            volume_ratio, pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm,
            total_share, float_share, free_share, total_mv, circ_mv
        FROM stock_daily_basic
        WHERE trade_date BETWEEN %s AND %s
    """
    params: list = [start, end]

    if ts_codes:
        placeholders = ",".join(["%s"] * len(ts_codes))
        sql += f" AND ts_code IN ({placeholders})"
        params.extend(ts_codes)

    sql += f"""
        ON DUPLICATE KEY UPDATE
            {esc('close')} = VALUES({esc('close')}), turnover_rate = VALUES(turnover_rate),
            turnover_rate_f = VALUES(turnover_rate_f), volume_ratio = VALUES(volume_ratio),
            pe = VALUES(pe), pe_ttm = VALUES(pe_ttm), pb = VALUES(pb),
            ps = VALUES(ps), ps_ttm = VALUES(ps_ttm), dv_ratio = VALUES(dv_ratio),
            dv_ttm = VALUES(dv_ttm), total_share = VALUES(total_share),
            float_share = VALUES(float_share), free_share = VALUES(free_share),
            total_mv = VALUES(total_mv), circ_mv = VALUES(circ_mv)
    """

    cur.execute(sql, params)
    return {"status": "success", "affected": cur.rowcount}


def sync_fina_indicator(cur, start: str, end: str, ts_codes: list | None) -> dict:
    """stock_fina_indicator (DATE) -> fina_indicator (CHAR(8))
    Dynamically finds common columns between source and target.
    """
    cur.execute("DESCRIBE fina_indicator")
    target_cols = [row[0] for row in cur.fetchall()]

    cur.execute("DESCRIBE stock_fina_indicator")
    source_cols = [row[0] for row in cur.fetchall()]

    # Skip auto-generated columns from source
    skip = {"id", "created_at", "updated_at"}
    common = [c for c in target_cols if c in source_cols and c not in skip]

    if not common:
        return {"status": "error", "error": "No common columns found between source and target"}

    date_cols = {"ann_date", "end_date", "f_ann_date"}
    select_parts = []
    for c in common:
        if c in date_cols:
            select_parts.append(f"DATE_FORMAT({c}, '%%Y%%m%%d') AS {c}")
        else:
            select_parts.append(c)

    col_list = ", ".join(esc(c) for c in common)
    select_clause = ", ".join(select_parts)

    # Only update non-PK columns
    pk_cols = {"ts_code", "end_date"}
    update_cols = [c for c in common if c not in pk_cols]
    update_clause = ", ".join(f"{esc(c)} = VALUES({esc(c)})" for c in update_cols)

    sql = f"""
        INSERT INTO fina_indicator ({col_list})
        SELECT {select_clause}
        FROM stock_fina_indicator
        WHERE end_date BETWEEN %s AND %s
    """
    params: list = [start, end]

    if ts_codes:
        placeholders = ",".join(["%s"] * len(ts_codes))
        sql += f" AND ts_code IN ({placeholders})"
        params.extend(ts_codes)

    if update_clause:
        sql += f" ON DUPLICATE KEY UPDATE {update_clause}"

    cur.execute(sql, params)
    return {"status": "success", "affected": cur.rowcount}


def main():
    parser = argparse.ArgumentParser(description="Sync source tables to tushare target tables")
    parser.add_argument("--start", required=True, help="Start date, YYYYMMDD")
    parser.add_argument("--end", required=True, help="End date, YYYYMMDD")
    parser.add_argument("--ts-codes", default="", help="Comma separated stock codes")
    parser.add_argument("--tables", default="all",
                        help="Tables to sync: all, stock_daily, stock_daily_basic, fina_indicator")
    parser.add_argument("--env", default="/home/application/tushare_stock_data/.env", help=".env path")
    args = parser.parse_args()

    env = load_env(args.env)
    ts_codes = [c.strip().upper() for c in args.ts_codes.split(",") if c.strip()] if args.ts_codes else None

    conn = get_db_conn(env)
    cur = conn.cursor()

    result: dict[str, Any] = {
        "status": "success",
        "start_date": args.start,
        "end_date": args.end,
        "ts_codes": ts_codes,
        "synced": {},
        "missing_or_failed": [],
    }

    tables_to_sync = ["stock_daily", "stock_daily_basic", "fina_indicator"] if args.tables == "all" else [t.strip() for t in args.tables.split(",")]

    syncers = {
        "stock_daily": sync_stock_daily,
        "stock_daily_basic": sync_stock_daily_basic,
        "fina_indicator": sync_fina_indicator,
    }

    for tbl in tables_to_sync:
        if tbl not in syncers:
            result["missing_or_failed"].append(tbl)
            result["synced"][tbl] = {"status": "error", "error": f"Unknown table: {tbl}"}
            continue
        try:
            sync_result = syncers[tbl](cur, args.start, args.end, ts_codes)
            result["synced"][tbl] = sync_result
            conn.commit()
        except Exception as e:
            conn.rollback()
            result["synced"][tbl] = {"status": "failed", "error": str(e)}
            result["missing_or_failed"].append(tbl)

    if result["missing_or_failed"]:
        result["status"] = "partial_success" if result["synced"] else "failed"

    conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
