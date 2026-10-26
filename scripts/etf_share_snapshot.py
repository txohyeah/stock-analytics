#!/usr/bin/env python3
"""当日 ETF 份额快照；仅交易日调用，行情全部解析完成后串行写入。"""
from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.sync.base import is_trade_day_today, today_yyyymmdd
from app.sync.registry import ETF_SENTINEL_CODES
from app.tushare_client import TushareClient


class _CalendarStore:
    driver = "sqlite"
    # 复用调用者连接，避免第二个 SQLite 写连接。
    def __init__(self, con):
        self.con = con

    def query(self, sql, params=()):
        return self.con.execute(sql, params).fetchall()


class _CalendarClient:
    def __init__(self, settings):
        self.settings = settings

    def query(self, *args, **kwargs):
        return TushareClient(self.settings).query(*args, **kwargs)


def _number(value):
    try:
        n = float(str(value).replace(',', ''))
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def snapshot_today(con, date=None) -> dict:
    """返回已采集 {ts_code: shares}；拒绝历史日期，防止当日快照写成历史。"""
    today = today_yyyymmdd()
    date = date or today
    if date != today:
        raise ValueError("份额快照只支持今天，不能回补历史日期")
    settings = get_settings()
    if is_trade_day_today(_CalendarStore(con), _CalendarClient(settings), settings) is not True:
        return {}
    import akshare as ak

    # 外呼失败或格式错误时尚未建表/写行；异常信息不回显第三方响应。
    try:
        frame = ak.fund_etf_spot_em()
        required = {'代码', '最新份额', '最新价'}
        if not required.issubset(frame.columns):
            raise ValueError('快照字段不完整')
        by_symbol = {c.split('.')[0]: c for c in ETF_SENTINEL_CODES}
        rows = []
        now = datetime.now().isoformat(timespec='seconds')
        for _, r in frame.iterrows():
            code = by_symbol.get(str(r['代码']).strip().zfill(6))
            shares = _number(r['最新份额'])
            if code and shares is not None and shares > 0:
                rows.append((code, date, shares, _number(r['最新价']), now))
        if not rows:
            raise ValueError('白名单无有效份额')
    except Exception:
        raise RuntimeError('akshare ETF 快照失败或无有效数据') from None
    with con:
        con.execute('''CREATE TABLE IF NOT EXISTS etf_share_daily(
            ts_code TEXT, trade_date TEXT, shares REAL, close REAL, captured_at TEXT,
            PRIMARY KEY(ts_code,trade_date))''')
        con.executemany('''INSERT INTO etf_share_daily VALUES(?,?,?,?,?)
            ON CONFLICT(ts_code,trade_date) DO UPDATE SET
            shares=COALESCE(excluded.shares,etf_share_daily.shares),
            close=COALESCE(excluded.close,etf_share_daily.close),
            captured_at=excluded.captured_at''', rows)
    return {r[0]: r[2] for r in rows}


def main():
    try:
        with sqlite3.connect(get_settings().db_sqlite_path) as con:
            result = snapshot_today(con)
        print(json.dumps(dict(ok=True, success=len(result), failed=[c for c in ETF_SENTINEL_CODES if c not in result]), ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps(dict(ok=False, success=0, failed=list(ETF_SENTINEL_CODES), error='ETF 份额快照失败'), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
