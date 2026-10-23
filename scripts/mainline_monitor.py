#!/usr/bin/env python3
"""Daily board state monitor. All windows count observed trading sessions.

Historical replay uses frozen rotat rows; run additionally uses live rank <= 10.
Survival and cooldown advance only for boards on the current list, as frozen.
Replay always starts at the first rotat session,
so reruns and out-of-order runs do not depend on previously persisted snapshots.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

DB = Path(__file__).resolve().parents[1] / 'data/board_strength.db'
EVENTS = set('三季报增长 一季报增长 中报增长 年报增长 并购重组 股权转让 实控人变更 次新股 高送转 ST板块 破净股 低价股 送转填权 年报预增 业绩增长 再融资 举牌 可转债'.split())
SCHEMA = '''CREATE TABLE IF NOT EXISTS mainline_state(
 date TEXT, board TEXT, role TEXT, signal_kind TEXT,
 trig_date TEXT, trig_value REAL, days_in_role INTEGER, active_peak REAL,
 status TEXT, updated_at TEXT, PRIMARY KEY(date, board, role))'''


def load_days(con, target=None, live=False):
    query = "SELECT date,name,value FROM board_strength WHERE (scope='rotat'"
    if live:
        query += " OR (scope='live' AND rank BETWEEN 1 AND 10)"
    query += ')'
    args = []
    if target:
        query += ' AND date<=?'
        args.append(target)
    query += ' ORDER BY date'
    days = {}
    for day, board, value in con.execute(query, args):
        if datetime.strptime(day, '%Y-%m-%d').date().isoformat() != day:
            raise ValueError('Invalid board date: ' + day)
        days.setdefault(day, {})[board] = float(value)
    if not days:
        raise ValueError('No board sessions available')
    if target and target not in days:
        raise ValueError('No board session for ' + target)
    return days


def replay(days):
    """Frozen reference transitions; snapshot metadata does not drive transitions."""
    cal = sorted(days)
    didx = {d: i for i, d in enumerate(cal)}
    st, sigs, snapshots = {}, [], {}
    metadata = {}

    def fresh(day):
        return {"on": [], "seg": day, "peak": -1e18, "miss": 0,
                "conf": None, "surv": None, "tv": None, "cd": 0}

    for i, d in enumerate(cal):
        on = {n: v for n, v in days[d].items() if n not in EVENTS}
        rows = []

        # 1) Append and update peak before every transition.
        for n, v in on.items():
            s = st.setdefault(n, fresh(d))
            s["on"].append(d)
            s["peak"] = max(s["peak"], v)
            s["miss"] = 0
            metadata.setdefault(n, {})

        # 2) Retain an empty segment starting on the reset session.
        for n, s in st.items():
            if n not in on and s["on"] and i - didx[s["on"][-1]] > 5:
                st[n] = fresh(d)
                metadata[n] = {}

        # 2.5) V2 exit rule: a confirmed board absent for 3 straight sessions exits.
        for n, s in st.items():
            if s["conf"] and n not in on:
                s["miss"] = s.get("miss", 0) + 1
                if s["miss"] >= 3:
                    meta = metadata.get(n) or {}
                    rows.append((d, n, 'EXIT', meta.get('kind'), meta.get('trig_date'),
                                 meta.get('trig_value'), i - meta.get('role_start', i) + 1,
                                 s['peak'], 'EXITED'))
                    st[n] = fresh(d)
                    metadata[n] = {}

        def mainline_row(n, status):
            s, meta = st[n], metadata[n]
            rows.append((d, n, 'MAINLINE', meta['kind'], meta['trig_date'],
                         meta['trig_value'], i - meta['role_start'] + 1,
                         s['peak'], status))

        # 3) Reference order: survival, cooldown, then FAST/A/B.
        for n, v in on.items():
            s = st[n]
            if s["conf"] and s["surv"] is not None:
                s["surv"] -= 1
                if s["surv"] <= 0:
                    on5 = [x for x in s["on"] if didx[x] > i - 5]
                    if len(on5) >= 3 and v >= s["tv"] * 0.6:
                        s["surv"] = None
                    else:
                        mainline_row(n, 'REVOKED')
                        s["conf"] = None
                        s["tv"] = None
                        s["cd"] = 10
            if s["cd"] > 0:
                s["cd"] -= 1
                continue
            if s["conf"]:
                continue
            seg_len = i - didx[s["seg"]]
            on10 = sum(1 for x in s["on"] if didx[x] > i - 10)
            kind = None
            if v >= 17000:
                kind = "FAST"
            elif v >= 12000 and on10 >= 3:
                kind = "A"
            elif seg_len >= 11 and on10 >= 4:
                prevs = [days[x][n] for x in s["on"] if x != d]
                if prevs and v > max(prevs):
                    kind = "B"
            if kind:
                s["conf"] = kind
                s["tv"] = v
                s["surv"] = 5
                metadata[n].update(kind=kind, trig_date=d,
                                   trig_value=v, role_start=i)
                metadata[n].pop('prep_start', None)
                sigs.append({"date": d, "board": n, "kind": kind})

        # 4) PREP follows all confirmation decisions, including cooldown boards.
        for n, v in on.items():
            s = st[n]
            on5 = [x for x in s["on"] if didx[x] > i - 5]
            if len(on5) >= 2 and v >= 3000 and not s["conf"]:
                sigs.append({"date": d, "board": n, "kind": "PREP"})
                start = metadata[n].setdefault('prep_start', i)
                rows.append((d, n, 'PREP', 'PREP', d, v,
                             i - start + 1, s['peak'], 'ACTIVE'))
            else:
                metadata[n].pop('prep_start', None)

        # Snapshots include confirmed boards absent today; absence does not
        # decrement survival/cooldown. V2 待验证：峰值比例退出规则。
        for n, s in st.items():
            if s['conf']:
                mainline_row(n, 'ACTIVE')
            if n not in on:
                metadata[n].pop('prep_start', None)
        snapshots[d] = rows
    return sigs, snapshots


def report(con, n):
    days = [r[0] for r in con.execute(
        'SELECT DISTINCT date FROM board_strength WHERE date <= '
        '(SELECT MAX(date) FROM mainline_state) ORDER BY date DESC LIMIT ?', (n,))]
    print('| 日期 | 板块 | 角色 | 档位 | 角色天数 | 活跃峰值 | 状态 |')
    print('|---|---|---|---|---:|---:|---|')
    for day in days:
        for r in con.execute('SELECT date,board,role,signal_kind,days_in_role,active_peak,status '
                             'FROM mainline_state WHERE date=? ORDER BY board,role', (day,)):
            print('| ' + ' | '.join(str(v).replace('|', '\\|').replace('\n', ' ') for v in r) + ' |')


def main():
    parser = argparse.ArgumentParser(description='板块主线状态监控')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--run', nargs='?', const='', metavar='DATE', help='更新指定日状态，默认最新日')
    group.add_argument('--backtest', action='store_true', help='历史信号 JSON')
    group.add_argument('--report', nargs='?', const=10, type=int, metavar='N', help='最近 N 日状态')
    parser.add_argument('--db', type=Path, default=DB, help='SQLite 路径')
    args = parser.parse_args()
    if args.report is not None and args.report < 1:
        parser.error('N must be positive')
    if not args.db.is_file():
        parser.error('Database does not exist')
    try:
        with sqlite3.connect(str(args.db)) as con:
            con.execute(SCHEMA)
            now = datetime.now(timezone.utc).isoformat(timespec='seconds')
            count = sessions = 0
            if args.report is not None:
                report(con, args.report)
            else:
                target = args.run or None
                if target:
                    if datetime.strptime(target, '%Y-%m-%d').date().isoformat() != target:
                        raise ValueError('DATE must be YYYY-MM-DD')
                days = load_days(con, target, live=not args.backtest)
                signals, snapshots = replay(days)
                sessions = len(days)
                if args.backtest:
                    print(json.dumps({'signals': signals}, ensure_ascii=False, indent=2))
                    count = len(signals)
                else:
                    day = list(days)[-1]
                    # Replace replayed prefix atomically; invalidate later stale snapshots.
                    con.execute('DELETE FROM mainline_state')
                    rows = [r + (now,) for rs in snapshots.values() for r in rs]
                    con.executemany('INSERT INTO mainline_state VALUES(?,?,?,?,?,?,?,?,?,?)', rows)
                    count = len(rows)
                    print(json.dumps({'date': day, 'states': len(snapshots[day])}, ensure_ascii=False))
            con.execute('INSERT INTO capture_log(ts,action,days,rows,note) VALUES(?,?,?,?,?)',
                        (now, 'mainline', sessions, count,
                         'backtest' if args.backtest else 'report' if args.report else 'run'))
    except (ValueError, sqlite3.Error) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
