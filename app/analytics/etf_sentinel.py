"""宽基 ETF 放量信号；仅描述疑似行为，evaluate 只读且无网络调用。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import math

import numpy as np

from app.sync.registry import ETF_SENTINEL_CODES, ETF_CORE_CODES, ETF_WATCH_CODES
from .errors import DataInsufficientError, UserInputError

RATIO20_THRESHOLD = 3.0
P90_QUANTILE = 0.90
CONTEXT_THRESHOLD = 0.5
SHARE_CHANGE_THRESHOLD = 0.01
AMOUNT_PER_YI = 100000.0  # fund_daily.amount 千元 → 亿元
FOOTNOTE = "放量≠实锤：场内成交为双边口径，份额与官方公告为更确凿证据"


def _table_exists(con, table):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _missing(message, reason):
    raise DataInsufficientError(message, payload={"reason": reason})


def _share(con, code, date):
    # 返回 (日变化率, 展示文本, 份额绝对值亿份)；绝对值供下游趋势图使用
    if not _table_exists(con, "etf_share_daily"):
        return None, "份额基线积累中（第 0 天）", None
    rows = con.execute("SELECT trade_date, shares FROM etf_share_daily WHERE ts_code=? AND trade_date<=? AND shares IS NOT NULL ORDER BY trade_date DESC LIMIT 2", (code, date)).fetchall()
    n = con.execute("SELECT COUNT(*) FROM etf_share_daily WHERE ts_code=? AND trade_date<=? AND shares IS NOT NULL", (code, date)).fetchone()[0]
    if len(rows) < 2 or rows[0][0] != date or rows[1][1] <= 0:
        return None, f"份额基线积累中（第 {n} 天）", (rows[0][1] / 1e8 if rows and rows[0][1] else None)
    change = rows[0][1] / rows[1][1] - 1
    text = f"{change:+.2%}"
    if abs(change) >= SHARE_CHANGE_THRESHOLD:
        text = f"份额异动 {change:+.2%}（一级申购/赎回嫌疑）"
    return change, text, rows[0][1] / 1e8


def evaluate(con, date=None) -> dict:
    """读取本地日线，前序窗口严格排除当日；缺数明确报错，不输出平静。"""
    for table in ("fund_daily", "index_daily"):
        if not _table_exists(con, table):
            _missing(f"DATA_INSUFFICIENT: 表不存在 {table}", "TABLE_MISSING")
    if date is None:
        marks = ','.join('?' for _ in ETF_SENTINEL_CODES)
        date = con.execute(f"SELECT MAX(trade_date) FROM fund_daily WHERE ts_code IN ({marks})", tuple(ETF_SENTINEL_CODES)).fetchone()[0]
    if not date:
        _missing("DATA_INSUFFICIENT: fund_daily 无数据", "NO_DATA")
    try:
        if len(date) != 8:
            raise ValueError
        datetime.strptime(date, "%Y%m%d")
    except (ValueError, TypeError):
        raise UserInputError("日期须为 YYYYMMDD")
    idx = con.execute("SELECT pct_chg FROM index_daily WHERE ts_code='000001.SH' AND trade_date=?", (date,)).fetchone()
    if not idx or idx[0] is None or not math.isfinite(float(idx[0])):
        _missing(f"DATA_INSUFFICIENT: index_daily 无 {date} 上证数据", "NO_DATA")
    pct = float(idx[0])
    context = "down" if pct <= -CONTEXT_THRESHOLD else "up" if pct >= CONTEXT_THRESHOLD else "flat"
    items = []
    for code, name in ETF_SENTINEL_CODES.items():
        rows = con.execute("SELECT trade_date, amount FROM fund_daily WHERE ts_code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 121", (code, date)).fetchall()
        if not rows or rows[0][0] != date or rows[0][1] is None:
            if code in ETF_CORE_CODES:
                # 核心家族缺数会让共振判断失真，必须报错，不许静默
                _missing(f"DATA_INSUFFICIENT: fund_daily 无 {date} {code} 数据", "NO_DATA")
            continue   # 观察池缺数（如 A500 上市前）跳过该只，其余照常出信号
        history = [float(r[1]) for r in rows[1:] if r[1] is not None and math.isfinite(float(r[1])) and r[1] >= 0]
        amount = float(rows[0][1])
        if not history or not math.isfinite(amount) or amount < 0 or np.mean(history[:20]) <= 0:
            _missing(f"DATA_INSUFFICIENT: {code} 无有效历史基线", "BASELINE_MISSING")
        ratio = amount / float(np.mean(history[:20]))
        p90 = float(np.quantile(history, P90_QUANTILE))
        change, share_text, share_yi = _share(con, code, date)
        items.append(dict(ts_code=code, name=name, amount_yi=amount / AMOUNT_PER_YI,
                          baseline_yi=float(np.mean(history[:20])) / AMOUNT_PER_YI,
                          ratio20=ratio, p90_120=p90, p90_hit=amount >= p90,
                          abnormal=ratio >= RATIO20_THRESHOLD or amount >= p90,
                          share_chg=change, share_yi=share_yi, share_text=share_text))
    core = [r for r in items if r['ts_code'] in ETF_CORE_CODES]
    # 观察池返回全量行（各带 abnormal 标记），展示端自行过滤异动——下游同步/画图需要全量
    watch = [r for r in items if r['ts_code'] in ETF_WATCH_CODES]
    abnormal = [r for r in core if r['abnormal']]
    n = len(abnormal)
    rating = "平静"
    if n:
        if context == 'down':
            rating = "疑似托底（强）" if n >= 2 else "疑似托底（中）"
        elif context == 'up':
            rating = "疑似压盘（弱）" if n >= 2 else "冲高巨量"
        else:
            rating = "异常放量"
    basis = '、'.join(f"{r['ts_code']} {r['ratio20']:.2f}倍" for r in abnormal) or "核心家族无异常放量"
    conclusion = f"{rating}：{basis}；上证 {pct:+.2f}%"
    return dict(ok=True, date=date, rating=rating, context=context, index_pct=pct,
                core=core, watch=watch, abnormal_codes=[r['ts_code'] for r in items if r['abnormal']],
                conclusion=conclusion, footnote=FOOTNOTE)


def markdown(result):
    lines = [f"🛡️ 国家队 ETF 监控 {result['date']}｜{result['rating']}", "",
             "|代码|简称|成交额亿|20日倍数|P90 标记|份额变化|", "|---|---|---:|---:|---|---|"]
    for r in result['core']:
        lines.append(f"|{r['ts_code']}|{r['name']}|{r['amount_yi']:.2f}|{r['ratio20']:.2f}|{'≥P90' if r['p90_hit'] else '—'}|{r['share_text']}|")
    watch_ab = [r for r in result['watch'] if r['abnormal']]
    lines += ["", "观察池：" + ('；'.join(f"{r['name']}（{r['ts_code']}）异常放量 {r['ratio20']:.2f}倍" for r in watch_ab) or "无异常放量"), "", result['conclusion'], "", FOOTNOTE]
    return '\n'.join(lines) + '\n'


def run(args):
    import sqlite3
    from .repository import DEFAULT_DB_PATH
    database = Path(args.database or DEFAULT_DB_PATH).resolve()
    con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        result = evaluate(con, args.date)
    finally:
        con.close()
    path = Path(args.output or f"/tmp/etf_sentinel_{result['date']}.md")
    path.write_text(markdown(result), encoding='utf-8')
    return dict(ok=True, date=result['date'], rating=result['rating'], abnormal_codes=result['abnormal_codes'], md_path=str(path))
