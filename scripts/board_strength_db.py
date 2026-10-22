#!/usr/bin/env python3
"""开盘啦「板块强度」本地库：日线级别落库 + 400 日历史回补

表结构 (data/board_strength.db)
  board_strength(date, name, value, zt_count, rank, code, scope)
      scope='rotat' → 历史回补（接口每日只给 Top10）
      scope='live'  → 当日全量 240 板块快照（含涨停家数）
  board_alias(alias, canonical)  → 两个接口板块名不同（机器人 / 机器人概念），自动对齐
  capture_log(ts, action, days, rows, note)

用法
  python scripts/board_strength_db.py --backfill 400      # 回补历史 Top10
  python scripts/board_strength_db.py --snapshot          # 抓当日全量 240 板块
  python scripts/board_strength_db.py --report            # 看库内最新状态
  python scripts/board_strength_db.py --snapshot --push  # 抓完顺便打印当日榜
"""
import argparse
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / 'data' / 'board_strength.db'
STOCK_DB = ROOT / 'data' / 'stock.db'

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Version/17.0 Safari/605.1.15')
ROTAT = 'https://duanxianxia.com/api/getPlateRotatData'
LIVE = 'https://duanxianxia.cn/api/getLiveByStrong'


def _h_rotat():
    return {'User-Agent': UA,
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'Origin': 'https://duanxianxia.com',
            'Referer': 'https://duanxianxia.com/web/platerotat/',
            'X-Requested-With': 'XMLHttpRequest'}


def _h_live():
    return {'User-Agent': UA, 'Accept': 'application/json',
            'Origin': 'https://duanxianxia.cn',
            'Referer': 'https://duanxianxia.cn/web/qxlive',
            'X-Requested-With': 'XMLHttpRequest'}


def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS board_strength(
        date TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL,
        zt_count INTEGER, rank INTEGER, code TEXT, scope TEXT NOT NULL,
        PRIMARY KEY(date, name));
    CREATE INDEX IF NOT EXISTS idx_bs_name ON board_strength(name);
    CREATE INDEX IF NOT EXISTS idx_bs_date ON board_strength(date);
    CREATE TABLE IF NOT EXISTS board_alias(alias TEXT PRIMARY KEY, canonical TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS capture_log(
        ts TEXT, action TEXT, days INTEGER, rows INTEGER, note TEXT);
    ''')
    con.commit()
    return con


def _post(url, data, headers, timeout=25, tries=4):
    """带重试的 POST（两个域名偶发 RemoteDisconnected）"""
    last = None
    for i in range(tries):
        try:
            r = requests.post(url, data=data, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:          # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f'{url} 请求失败: {last}')


def fetch_rotat(days=400, timeout=60):
    """历史每日 Top10 → [(date, name, value, rank, col_idx)]"""
    r = _post(ROTAT, {'from': 'kaipan', 'days': days, 'dates': ''}, _h_rotat(), timeout)
    html = r.json().get('html', '')
    rows = re.findall(r'<tr>(.*?)</tr>', html, re.S)
    if not rows:
        raise RuntimeError('rotat 接口返回空')
    cell = lambda row: [re.sub(r'<[^>]+>', '', c).replace('&nbsp;', '').strip()
                        for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
    head = cell(rows[0])
    dates = head[1:]
    out = []
    for row in rows[1:]:
        cs = cell(row)
        rank = int(cs[0]) if cs[0].isdigit() else None
        for i, c in enumerate(cs[1:]):
            m = re.match(r'^(.*?)(-?\d+)$', c)
            if m and i < len(dates):
                out.append((dates[i], m.group(1), int(m.group(2)), rank, i + 1))
    return out


def fetch_live(timeout=25):
    """当日全量 240 板块 → [(code, name, value, zt)]"""
    r = _post(LIVE, {'platetype': 'strong', 'platelist': ''}, _h_live(), timeout)
    plates = r.json().get('plates', {})
    if not plates:
        raise RuntimeError('live 接口返回空')
    out = []
    for k, v in plates.items():
        try:
            out.append((v.get('code', ''), v.get('name', ''), int(v.get('val')), int(v.get('ztcount', 0))))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: -x[2])
    return out


def latest_trade_date():
    """只依赖 trade_cal（日历是静态的），不依赖 20:10 才同步的 daily 表；
    收盘后(>=15:05)当日即算交易日，否则回退到上一个交易日。"""
    now = datetime.now()
    today = now.strftime('%Y%m%d')
    try:
        con = sqlite3.connect(f'file:{STOCK_DB}?mode=ro', uri=True)
        if now.hour * 60 + now.minute < 15 * 60 + 5:
            d = con.execute('SELECT max(cal_date) FROM trade_cal WHERE is_open=1 AND cal_date<?',
                            (today,)).fetchone()[0]
        else:
            d = con.execute('SELECT max(cal_date) FROM trade_cal WHERE is_open=1 AND cal_date<=?',
                            (today,)).fetchone()[0]
        con.close()
        return f'{d[:4]}-{d[4:6]}-{d[6:]}' if d else now.strftime('%Y-%m-%d')
    except Exception:
        return now.strftime('%Y-%m-%d')


def backfill(user_days=400):
    con = init_db()
    recs = fetch_rotat(days=user_days)
    con.executemany(
        'INSERT INTO board_strength(date,name,value,zt_count,rank,code,scope) '
        'VALUES(?,?,?,NULL,?,NULL,?) ON CONFLICT(date,name) DO UPDATE SET '
        'value=excluded.value, rank=excluded.rank',
        [(d, nm, v, rk, 'rotat') for d, nm, v, rk, _ in recs])
    days = len(set(r[0] for r in recs))
    con.execute('INSERT INTO capture_log VALUES(?,?,?,?,?)',
                (datetime.now().isoformat(timespec='seconds'), 'backfill', days, len(recs), ''))
    con.commit()
    print(f'回补完成: {days} 个交易日 / {len(recs)} 条 → {DB.name}')
    rep = con.execute('SELECT min(date), max(date), count(DISTINCT date), count(*) FROM board_strength').fetchone()
    print(f'库内现状: {rep[0]} ~ {rep[1]} 共 {rep[2]} 天 / {rep[3]} 行, {DB.stat().st_size/1024:.0f} KB')
    con.close()


def snapshot():
    con = init_db()
    date = latest_trade_date()
    live = fetch_live()
    # 与当日 rotat 行按数值对齐名称（机器人 ↔ 机器人概念）
    rot = con.execute('SELECT name,value FROM board_strength WHERE date=? AND scope="rotat"', (date,)).fetchall()
    alias = {}
    for code, nm, val, zt in live:
        for rname, rval in rot:
            if abs(rval - val) <= 2 and rname != nm:
                alias[nm] = rname
                break
    if alias:
        con.executemany('INSERT INTO board_alias(alias,canonical) VALUES(?,?) '
                        'ON CONFLICT(alias) DO UPDATE SET canonical=excluded.canonical',
                        list(alias.items()))
    known = dict(con.execute('SELECT alias, canonical FROM board_alias'))
    prev = dict(con.execute('SELECT name,value FROM board_strength WHERE date=?', (date,)).fetchall())
    rows = []
    for i, (code, nm, val, zt) in enumerate(live, 1):
        rows.append((date, known.get(nm, nm), val, zt, i, code, 'live'))
    changed = [(r[1], prev[r[1]], r[2]) for r in rows if r[1] in prev and prev[r[1]] != r[2]]
    con.executemany(
        'INSERT INTO board_strength(date,name,value,zt_count,rank,code,scope) '
        'VALUES(?,?,?,?,?,?,?) ON CONFLICT(date,name) DO UPDATE SET '
        'value=excluded.value, zt_count=excluded.zt_count, rank=excluded.rank, '
        'code=excluded.code, scope=excluded.scope', rows)
    delta_note = ''
    if changed:
        big = max(changed, key=lambda x: abs(x[2] - x[1]))
        delta_note = f'changed={len(changed)};max_delta={big[2]-big[1]:+d}({big[0]})'
    con.execute('INSERT INTO capture_log VALUES(?,?,?,?,?)',
                (datetime.now().isoformat(timespec='seconds'), 'snapshot', 1, len(rows),
                 f'date={date};alias={len(alias)};{delta_note}'))
    con.commit()
    print(f'快照入库: {date} 共 {len(rows)} 个板块, 新增名称对齐 {len(alias)} 个'
          + (f', 数值变动 {len(changed)} 个（{delta_note}）' if changed else ', 数值无变动'))
    print(f'库内现状: {DB.stat().st_size/1024:.0f} KB')
    con.close()
    return date, live


def peek(top=10):
    """只读盘中/盘后快照：打印前 top 名 + 市场宽度，不写库"""
    live = fetch_live()
    pos = [x for x in live if x[2] > 0]
    over8k = [x for x in live if x[2] >= 8000]
    over13k = [x for x in live if x[2] >= 13000]
    over17k = [x for x in live if x[2] >= 17000]
    print(f'开盘啦板块强度实时快照（{datetime.now().strftime("%H:%M")}）')
    print(f'{"#":>2} {"板块":<14}{"强度":>9}{"涨停":>5}')
    for i, (code, nm, val, zt) in enumerate(live[:top], 1):
        print(f'{i:>2} {nm:<14}{val:>9}{zt:>5}')
    topv = live[0][2]
    tier = '进攻' if topv >= 17000 else '正常' if topv >= 13000 else '正常偏弱' if topv >= 8000 else '防守'
    print(f'\n第一名 {live[0][1]} {topv:,} → {tier}')
    print(f'市场宽度: {len(live)} 个板块中 正强度 {len(pos)} 个 / ≥8000 {len(over8k)} 个 / '
          f'≥1.3万 {len(over13k)} 个 / ≥1.7万 {len(over17k)} 个')
    return live


def report(top=10):
    con = init_db()
    d = con.execute('SELECT max(date) FROM board_strength').fetchone()[0]
    rows = con.execute('SELECT rank,name,value,zt_count,code,scope FROM board_strength '
                       'WHERE date=? ORDER BY value DESC LIMIT ?', (d, top)).fetchall()
    print(f'{d} 板块强度 Top{top}（库内）')
    for rk, nm, v, zt, code, sc in rows:
        print(f'{rk or 0:>2} {nm:<12}{v:>7}  涨停{(zt if zt is not None else "-"):>3}  {code or "":<8}{sc}')
    n = con.execute('SELECT scope,count(*) FROM board_strength WHERE date=? GROUP BY scope', (d,)).fetchall()
    print('  当日入库类型:', dict(n))
    print(f'  库大小 {DB.stat().st_size/1024:.0f} KB / 总行数 '
          f'{con.execute("SELECT count(*) FROM board_strength").fetchone()[0]}')
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backfill', type=int, nargs='?', const=400)
    ap.add_argument('--snapshot', action='store_true')
    ap.add_argument('--peek', action='store_true', help='只读实时前十 + 市场宽度，不写库')
    ap.add_argument('--report', action='store_true')
    a = ap.parse_args()
    if a.backfill:
        backfill(a.backfill)
    if a.snapshot:
        snapshot()
    if a.peek:
        peek()
    if a.report:
        report()


if __name__ == '__main__':
    main()
