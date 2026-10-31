#!/usr/bin/env python3
"""市场强度图 · 数据包生成（2026-10-23）

只算数据、不画图：产出 JSON 供 paper-sim 站点与飞书 HTML 共用同一套 canvas 渲染。
- K 线：上证 000001.SH 最近 window 个交易日（默认 125）
- 金牛因果通道：复用 tech_indicators.compute_golden_bull_lines(causal=True)，全历史喂、取窗口段
- 手绘线：逐日取值（水平线=全窗口恒值；斜线=锚点起不外推，锚点日前 → null）
- MACD：12/26/9 国内口径（柱=2*(DIF-DEA)），与 market_lines._macd_series 同一实现

用法：
  python scripts/market_chart.py --date 20261022            # 单日 → stdout JSON
  python scripts/market_chart.py --days 250 --out data/charts  # 批量写文件（一年）

单日 payload 约 12~15 KB，一年 250 日 ≈ 3 MB。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from market_lines import _COLS, _macd_series, _tmap, line_value  # noqa: E402

DB = ROOT / 'data' / 'stock.db'
TS_CODE = '000001.SH'
NAME = '上证指数'
WINDOW = 125


def build_payload(con: sqlite3.Connection, date: str, window: int = WINDOW) -> dict | None:
    """生成某交易日的数据包；数据不足或当日无 K 线返回 None。"""
    rows = con.execute(
        'SELECT trade_date, open, high, low, close FROM index_daily '
        'WHERE ts_code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT ?',
        (TS_CODE, date, window)).fetchall()
    rows = rows[::-1]
    if len(rows) < 30 or rows[-1][0] != date:
        return None

    dates = [r[0] for r in rows]
    tmap = _tmap(con)

    # 金牛因果通道：全历史喂，取窗口段（避免窗口起点边界效应）
    full = con.execute(
        'SELECT high, low FROM index_daily WHERE ts_code=? AND trade_date<=? ORDER BY trade_date',
        (TS_CODE, date)).fetchall()
    gdf = pd.DataFrame(full, columns=['high', 'low']).astype(float)
    sys.path.insert(0, '/home/application/tech-indicators/src')
    from tech_indicators.indicators import compute_golden_bull_lines
    gb = compute_golden_bull_lines(gdf, causal=True)
    off = len(full) - len(rows)
    g_up = [None if pd.isna(v) else round(float(v), 2)
            for v in gb['channel_upper'].values[off:off + len(rows)]]
    g_lo = [None if pd.isna(v) else round(float(v), 2)
            for v in gb['channel_lower'].values[off:off + len(rows)]]

    active = [dict(zip(_COLS, r)) for r in con.execute(
        "SELECT * FROM market_lines WHERE status='active' ORDER BY id").fetchall()]

    lines = []
    for ln in active:
        horizontal = ln['line_type'] == 'horizontal' or ln['slope_per_day'] == 0
        ys = []
        for d in dates:
            v = line_value(ln, d, tmap)
            # 水平线=价格水平位，全窗口恒值；斜线只画锚点之后的段，不外推
            if horizontal:
                ys.append(None if v is None else round(float(v), 2))
            else:
                ys.append(None if (v is None or d < ln['p1_date']) else round(float(v), 2))
        lines.append({
            'id': ln['id'], 'name': ln['name'], 'kind': ln['kind'],
            'line_type': ln['line_type'], 'p1_date': ln['p1_date'],
            'p1_price': ln['p1_price'], 'slope': ln['slope_per_day'],
            'note': ln.get('note') or '', 'ys': ys,
        })

    closes = [r[4] for r in rows]
    dif, dea, bar = _macd_series(closes)

    return {
        'date': date,
        'code': TS_CODE,
        'name': NAME,
        'window': len(rows),
        'kline': [[r[0], round(r[1], 2), round(r[2], 2), round(r[3], 2), round(r[4], 2)]
                  for r in rows],
        'gold_up': g_up,
        'gold_lo': g_lo,
        'lines': lines,
        'macd': {'dif': [round(x, 4) for x in dif],
                 'dea': [round(x, 4) for x in dea],
                 'bar': [round(x, 4) for x in bar]},
        'breadth': build_breadth(con, date),
        'built_at': datetime.now().isoformat(timespec='seconds'),
    }


def build_breadth(con: sqlite3.Connection, date: str) -> dict | None:
    """当日市场宽度（大盘口径）：上涨占比 / 中位涨跌 / 涨停跌停 / 等权位置。

    涨停跌停 = 收盘触板近似（close 贴 high 且 pct_chg≥9.7，跌停同理），非交易所口径。
    """
    row = con.execute(
        'SELECT ret_mean, ret_med, up_ratio, ew_idx, ew_ma60, breadth20, mkt_state '
        'FROM market_state_daily WHERE trade_date=?', (date,)).fetchone()
    lim = con.execute(
        "SELECT SUM(CASE WHEN pct_chg>=9.7 AND close>=high*0.999 THEN 1 ELSE 0 END),"
        "       SUM(CASE WHEN pct_chg<=-9.7 AND close<=low*1.001 THEN 1 ELSE 0 END),"
        "       COUNT(*) FROM daily WHERE trade_date=?", (date,)).fetchone()
    if not row and not lim[2]:
        return None
    out = {'limit_up': int(lim[0] or 0), 'limit_down': int(lim[1] or 0), 'n_stocks': int(lim[2] or 0)}
    if row:
        ret_mean, ret_med, up_ratio, ew, ewma, b20, state = row
        out.update({
            'ret_mean': None if ret_mean is None else round(ret_mean * 100, 2),
            'ret_med': None if ret_med is None else round(ret_med * 100, 2),
            'up_ratio': None if up_ratio is None else round(up_ratio * 100, 1),
            'ew_idx': None if ew is None else round(ew, 4),
            'ew_ma60': None if ewma is None else round(ewma, 4),
            'ew_dev': None if (ew is None or not ewma) else round((ew / ewma - 1) * 100, 2),
            'breadth20': None if b20 is None else round(b20 * 100, 1),
            'mkt_state': state,
        })
    return out


def trade_dates(con: sqlite3.Connection, days: int) -> list[str]:
    """最近 days 个交易日（升序）。"""
    rows = con.execute('SELECT DISTINCT trade_date FROM index_daily WHERE ts_code=? '
                       'ORDER BY trade_date DESC LIMIT ?', (TS_CODE, days)).fetchall()
    return sorted(r[0] for r in rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', help='单日 YYYYMMDD')
    ap.add_argument('--days', type=int, default=0, help='批量：最近 N 个交易日')
    ap.add_argument('--out', help='输出目录（批量时必须）')
    a = ap.parse_args()

    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    if a.date:
        p = build_payload(con, a.date.replace('-', ''))
        print(json.dumps(p, ensure_ascii=False) if p else 'null')
        return
    if not a.days or not a.out:
        ap.error('需要 --date，或 --days N --out DIR')
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ok = skip = 0
    for d in trade_dates(con, a.days):
        p = build_payload(con, d)
        if not p:
            skip += 1
            continue
        (out / f'{d}.json').write_text(json.dumps(p, ensure_ascii=False), encoding='utf-8')
        ok += 1
    print(f'生成 {ok} 个数据包 / 跳过 {skip}，目录 {out}')


if __name__ == '__main__':
    main()
