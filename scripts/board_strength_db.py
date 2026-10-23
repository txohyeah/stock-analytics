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
import uuid
from concurrent.futures import ThreadPoolExecutor
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
        source TEXT,
        PRIMARY KEY(date, name));
    CREATE INDEX IF NOT EXISTS idx_bs_name ON board_strength(name);
    CREATE INDEX IF NOT EXISTS idx_bs_date ON board_strength(date);
    CREATE TABLE IF NOT EXISTS board_alias(alias TEXT PRIMARY KEY, canonical TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS capture_log(
        ts TEXT, action TEXT, days INTEGER, rows INTEGER, note TEXT);
    ''')
    # 老库迁移：source 列标识数据来源（rotat/live/direct）
    cols = {r[1] for r in con.execute('PRAGMA table_info(board_strength)').fetchall()}
    if 'source' not in cols:
        con.execute('ALTER TABLE board_strength ADD COLUMN source TEXT')
        con.execute("UPDATE board_strength SET source='rotat' WHERE scope='rotat' AND source IS NULL")
        con.execute("UPDATE board_strength SET source='live'  WHERE scope='live'  AND source IS NULL")
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


# ---------- L2 备份通道：直连开盘啦（与短线侠同源同值） ----------
# 实测 2026-10-22：240 个板块全量对账，强度值 238/240 完全一致（99.2%），
# rank 差异全部由「振兴东北」一个异常值引起的连续位移。
# 只在 L1（短线侠）失败时触发；单板块一次请求，240 个约 80 秒。
# 返回 List 语义：[0]当日全市场排名 [1]强度 [2]成交额 [3]主力净额
#                [4]涨跌幅% [5]疑连板家数(未确证) [6][7]未确证(不采用)
KQJ = 'https://apphis.longhuvip.com/w1/api/index.php'
KQJ_HEAD = {
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'User-Agent': 'Dalvik/2.1.0 (Linux; U; Android 9; SHARK PRS-A0 Build/PQ3A.190605.01141736)',
    'Host': 'apphis.longhuvip.com',
    'Connection': 'Keep-Alive',
    'Accept-Encoding': 'gzip',
}


def _kaipan_direct(code, date, timeout=20):
    """单板块直连取 (rank, strength)；失败抛异常。证书正常，不关校验。"""
    d = {'PhoneOSNew': '1', 'DeviceID': str(uuid.uuid4()), 'VerSion': '5.21.0.2',
         'apiv': 'w42', 'Date': date,
         'a': 'GetPlate_Info_QJ', 'c': 'ZhiShuRanking', 'PlateID': code}
    r = requests.post(KQJ, params={'apiv': 'w42', 'PhoneOSNew': '1', 'VerSion': '5.21.0.2'},
                      data=d, headers=KQJ_HEAD, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    lst = j.get('List') or []
    if str(j.get('errcode')) == '1020':
        raise RuntimeError('NO_DATA(1020) 该日强度尚未生成')
    if str(j.get('errcode')) != '0' or len(lst) < 2:
        raise RuntimeError(f'errcode={j.get("errcode")} len={len(lst)}')
    return int(lst[0]), int(lst[1])


def direct_snapshot(date=None, workers=4):
    """L2：按最近一次 live 快照的板块清单逐板块直连（小并发，默认 4 路）。"""
    con = init_db()
    date = date or latest_trade_date()
    last = con.execute("SELECT max(date) FROM board_strength WHERE scope='live'").fetchone()[0]
    codes = con.execute('SELECT code,name FROM board_strength '
                        'WHERE date=? AND scope="live" ORDER BY rank', (last,)).fetchall()
    if not codes:
        raise RuntimeError('库里没有 live 快照，拿不到板块清单，L2 无法启动')
    # 探针：1020 = 该日强度尚未生成（16:00 常见），直接放弃，等 20:30 复核
    try:
        _kaipan_direct(codes[0][0], date)
    except Exception as e:                          # noqa: BLE001
        if 'NO_DATA' in str(e):
            print(f'[L2] 开盘啦 {date} 尚未生成强度数据（1020），本次跳过，等下一次复核')
            con.execute('INSERT INTO capture_log VALUES(?,?,?,?,?)',
                        (datetime.now().isoformat(timespec='seconds'), 'direct', 1, 0,
                         f'date={date};nodata1020'))
            con.commit()
            con.close()
            return []

    def fetch(item):
        code, nm = item
        try:
            rk, val = _kaipan_direct(code, date)
            return (date, nm, val, None, rk, code, 'live', 'direct'), None
        except Exception as e:                      # noqa: BLE001
            return None, f'{nm}:{str(e)[:40]}'

    rows, fails = [], []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for row, err in ex.map(fetch, codes):
            if row:
                rows.append(row)
            else:
                fails.append(err)

    if len(rows) < len(codes) * 0.9:
        raise RuntimeError(f'L2 只成功 {len(rows)}/{len(codes)}，低于 90% 不写库：'
                           + '; '.join(fails[:3]))
    con.executemany(
        'INSERT INTO board_strength(date,name,value,zt_count,rank,code,scope,source) '
        'VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(date,name) DO UPDATE SET '
        'value=excluded.value, rank=excluded.rank, code=excluded.code, source=excluded.source',
        rows)
    con.execute('INSERT INTO capture_log VALUES(?,?,?,?,?)',
                (datetime.now().isoformat(timespec='seconds'), 'direct', 1, len(rows),
                 f'date={date};basis={last};fails={len(fails)};workers={workers}'))
    con.commit()
    print(f'[L2] 直连开盘啦入库: {date} 共 {len(rows)}/{len(codes)} 个板块'
          f'（清单取自 {last}），失败 {len(fails)} 个')
    for f in fails[:5]:
        print('    失败:', f)
    con.close()
    return rows


def latest_trade_date():
    """只依赖 trade_cal（日历是静态的），不依赖 20:10 才同步的 daily 表；
    收盘后(>=15:05)当日即算交易日，否则回退到上一个交易日。
    防御：15:05 后若当日不在日历（日历滞后），工作日直接归属当日——
    该时点接口返回的实时值必属当日，绝不能静默写到昨日（2026-10-23 事故）。"""
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
            if d is None or d < today:
                has_row = con.execute('SELECT is_open FROM trade_cal WHERE cal_date=?',
                                      (today,)).fetchone()
                if has_row is None and now.weekday() < 5:
                    print(f'[WARN] trade_cal 缺 {today}（日历滞后），15:05 后强制归属当日')
                    d = today
        con.close()
        return f'{d[:4]}-{d[4:6]}-{d[6:]}' if d else now.strftime('%Y-%m-%d')
    except Exception:
        return now.strftime('%Y-%m-%d')


def backfill(user_days=400):
    con = init_db()
    recs = fetch_rotat(days=user_days)
    con.executemany(
        'INSERT INTO board_strength(date,name,value,zt_count,rank,code,scope,source) '
        'VALUES(?,?,?,NULL,?,NULL,?,"rotat") ON CONFLICT(date,name) DO UPDATE SET '
        'value=excluded.value, rank=excluded.rank, source=excluded.source',
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
    try:
        live = fetch_live()
        if len(live) < 100:
            raise RuntimeError(f'live 只返回 {len(live)} 个板块，疑似接口异常')
    except Exception as e:                          # noqa: BLE001
        con.close()
        print(f'[L1] 短线侠 live 接口失败：{e} → 切 L2 直连开盘啦')
        return direct_snapshot(date)
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
        'INSERT INTO board_strength(date,name,value,zt_count,rank,code,scope,source) '
        'VALUES(?,?,?,?,?,?,?,"live") ON CONFLICT(date,name) DO UPDATE SET '
        'value=excluded.value, zt_count=excluded.zt_count, rank=excluded.rank, '
        'code=excluded.code, scope=excluded.scope, source=excluded.source', rows)
    delta_note = ''
    if changed:
        big = max(changed, key=lambda x: abs(x[2] - x[1]))
        delta_note = f'changed={len(changed)};max_delta={big[2]-big[1]:+d}({big[0]})'
        if abs(big[2] - big[1]) > 10000:
            delta_note += ';SUSPICIOUS'
            print(f'[WARN] {big[0]} 单日变动 {big[2]-big[1]:+d} 超过 10000，疑似日期归属错误或极端行情，请人工核对')
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
    ap.add_argument('--direct', action='store_true', help='只走 L2 直连开盘啦（备份通道，强制触发便于演练）')
    ap.add_argument('--peek', action='store_true', help='只读实时前十 + 市场宽度，不写库')
    ap.add_argument('--report', action='store_true')
    ap.add_argument('--date', help='指定交易日（L2 演练/补跑用，格式 YYYY-MM-DD）')
    ap.add_argument('--workers', type=int, default=4, help='L2 并发数（默认 4）')
    a = ap.parse_args()
    if a.backfill:
        backfill(a.backfill)
    if a.direct:
        direct_snapshot(a.date, a.workers)
    if a.snapshot:
        snapshot()
    if a.peek:
        peek()
    if a.report:
        report()


if __name__ == '__main__':
    main()
