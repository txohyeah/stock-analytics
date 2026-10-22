#!/usr/bin/env python3
"""拟合 v4：假设 强度 = 板块成交额 × K"""
import tushare as ts, pandas as pd, sqlite3, time

tok = open('/root/.qwenpaw/workspaces/default/.secrets/tushare_token_6000').read().strip()
pro = ts.pro_api(tok)
idx = pd.read_csv('data/ths_index_all.csv')

ANCHORS = [
    ('机器人概念', {'20261019': 8022, '20261020': 16972}),
    ('低空经济',   {'20261016': 13111, '20261019': 2359}),
    ('军工',      {'20261014': 1434, '20261016': 8496}),
    ('AI应用',    {'20261014': 3542, '20261016': 3947, '20261020': 5289, '20261021': 4840}),
    ('数字经济',   {'20261021': 7300}),
    ('商业航天',   {'20261016': 4022}),
    ('体育产业',   {'20261014': 1090}),
    ('文化传媒',   {'20261014': 2318}),
    ('东数西算(算力)', {'20261021': 3649}),
    ('游戏',      {'20261014': 1844, '20261015': 258}),
    ('证券',      {'20261015': 831}),
    ('汽车零部件',  {'20261016': 3775}),
    ('通信设备',   {'20261015': 3125, '20261019': 3976}),
]
DATES = ['20261014','20261015','20261016','20261019','20261020','20261021']
con = sqlite3.connect('data/stock.db')
daily = pd.read_sql_query(
    f"SELECT trade_date, ts_code, pct_chg, amount FROM daily WHERE trade_date IN ({','.join('?'*len(DATES))})",
    con, params=DATES)
daily['code6'] = daily['ts_code'].str[:6]

def find_code(name):
    hit = idx[(idx['name'] == name) & (idx['type'].isin(['N','I']))].sort_values('count', ascending=False)
    return (hit.iloc[0]['ts_code'], int(hit.iloc[0]['count'])) if len(hit) else (None, 0)

rows, cons_cache = [], {}
for name, vals in ANCHORS:
    code, cnt = find_code(name)
    if not code: continue
    if code not in cons_cache:
        m = pro.ths_member(ts_code=code); cons_cache[code] = set(m['con_code'].str[:6]); time.sleep(0.4)
    sub = daily[daily['code6'].isin(cons_cache[code])]
    for d, strength in vals.items():
        dd = sub[sub['trade_date'] == d]
        amt_yi = dd['amount'].sum() / 100000.0   # 千元→亿
        rows.append(dict(bk=name, N=len(cons_cache[code]), date=d, strength=strength,
                         amt_yi=round(amt_yi, 1), K_amt=round(strength/amt_yi, 3) if amt_yi > 0 else None,
                         up_n=int((dd['pct_chg']>0).sum())))
df = pd.DataFrame(rows)
print(df.to_string(index=False))
k = df['K_amt'].dropna()
print(f'\nK_amt(强度/成交额亿): 均值={k.mean():.2f} 中位={k.median():.2f} stdev={k.std():.2f} CV={k.std()/k.mean()*100:.1f}% n={len(k)}')
df.to_csv('data/strength_fit_v4.csv', index=False)
