#!/usr/bin/env python3
"""板块强度精确拟合（tushare ths_member 官方成分 + 本地日线涨幅）
锚点: 蓝海工具截图 6 个交易日 × 前 6 名板块强度值
"""
import tushare as ts, pandas as pd, sqlite3, json, time, statistics, re

tok = open('/root/.qwenpaw/workspaces/default/.secrets/tushare_token_6000').read().strip()
pro = ts.pro_api(tok)
idx = pd.read_csv('data/ths_index_all.csv')

# 蓝海截图锚点: 板块名 -> {YYYYMMDD: 强度值}
ANCHORS = [
    ('机器人概念', {'20261019': 8022, '20261020': 16972}),
    ('低空经济',   {'20261016': 13111, '20261019': 2359}),
    ('军工',      {'20261014': 1434, '20261016': 8496}),
    ('AI应用',    {'20261014': 3542, '20261016': 3947, '20261020': 5289, '20261021': 4840}),
    ('数字经济',   {'20261021': 7300}),
    ('商业航天',   {'20261016': 4022}),
    ('体育产业',   {'20261014': 1090}),
    ('文化传媒',   {'20261014': 2318}),
    ('2026三季报预增', {'20261015': 2927, '20261019': 2260, '20261020': 5302}),
    ('东数西算(算力)', {'20261021': 3649}),
    ('算力租赁',   {'20261021': 3649}),  # 备选映射
    ('游戏',      {'20261014': 1844, '20261015': 258}),
    ('证券',      {'20261015': 831}),
    ('物流',      {'20261015': 1271}),
    ('汽车零部件',  {'20261016': 3775}),
    ('通信设备',   {'20261015': 3125, '20261019': 3976}),
]

def find_code(name):
    hit = idx[idx['name'] == name]
    if len(hit):
        # 优先概念大板块（count 大）
        hit = hit.sort_values('count', ascending=False)
        return hit.iloc[0]['ts_code'], int(hit.iloc[0]['count'])
    return None, 0

con = sqlite3.connect('data/stock.db')
DATES = ['20261014','20261015','20261016','20261019','20261020','20261021']

rows = []
seen_codes = {}
for name, vals in ANCHORS:
    code, cnt = find_code(name)
    if not code:
        print(f'× {name}: ths_index 未找到'); continue
    if code not in seen_codes:
        try:
            m = pro.ths_member(ts_code=code)
            seen_codes[code] = set(m['con_code'].str[:6])
            time.sleep(0.4)
        except Exception as e:
            print(f'× {name}({code}) member FAIL:', str(e)[:80]); continue
    cons = seen_codes[code]
    # 本地涨幅
    q = f"SELECT trade_date, ts_code, pct_chg FROM daily WHERE trade_date IN ({','.join('?'*len(DATES))})"
    daily = pd.read_sql_query(q, con, params=DATES)
    daily['code6'] = daily['ts_code'].str[:6]
    sub = daily[daily['code6'].isin(cons)]
    for d, strength in vals.items():
        dd = sub[sub['trade_date'] == d]
        pct = dd['pct_chg']
        S1 = pct[pct > 0].sum()            # 只算上涨股的涨幅加总
        S5 = pct.sum()                      # 全部加总
        up_n = int((pct > 0).sum())
        limit_n = int((pct >= 9.8).sum())
        rows.append(dict(bk=name, code=code, N=len(cons), date=d, strength=strength,
                         S1=round(S1,1), S5=round(S5,1), up_n=up_n, limit_n=limit_n,
                         K1=round(strength/S1,3) if S1>0 else None))

df = pd.DataFrame(rows)
print(df.to_string(index=False))
K = df['K1'].dropna()
if len(K) > 2:
    print(f'\nK1(强度/S1) 统计: 均值={K.mean():.2f} 中位={K.median():.2f} '
          f'stdev={K.std():.2f} CV={K.std()/K.mean()*100:.1f}%  n={len(K)}')
df.to_csv('data/strength_fit_v1.csv', index=False)
