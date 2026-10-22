#!/usr/bin/env python3
"""板块强度拟合 v3：涨幅口径 + 资金流口径对比"""
import tushare as ts, pandas as pd, sqlite3, statistics, time

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
    ('物流',      {'20261015': 1271}),
    ('汽车零部件',  {'20261016': 3775}),
    ('通信设备',   {'20261015': 3125, '20261019': 3976}),
]
DATES = ['20261014','20261015','20261016','20261019','20261020','20261021']
con = sqlite3.connect('data/stock.db')
daily = pd.read_sql_query(
    f"SELECT trade_date, ts_code, pct_chg, close FROM daily WHERE trade_date IN ({','.join('?'*len(DATES))})", con, params=DATES)
mf = pd.read_sql_query(
    f"SELECT trade_date, ts_code, buy_lg_amount, sell_lg_amount, buy_elg_amount, sell_elg_amount, buy_md_amount, sell_md_amount, net_mf_amount FROM moneyflow WHERE trade_date IN ({','.join('?'*len(DATES))})", con, params=DATES)
daily['code6'] = daily['ts_code'].str[:6]
mf['code6'] = mf['ts_code'].str[:6]
data = daily.merge(mf.drop(columns=['ts_code']), on=['code6','trade_date'], how='left')

def find_code(name):
    hit = idx[(idx['name'] == name) & (idx['type'].isin(['N','I']))].sort_values('count', ascending=False)
    return (hit.iloc[0]['ts_code'], int(hit.iloc[0]['count'])) if len(hit) else (None, 0)

rows, cons_cache = [], {}
for name, vals in ANCHORS:
    code, cnt = find_code(name)
    if not code: print(f'× {name} 未找到'); continue
    if code not in cons_cache:
        m = pro.ths_member(ts_code=code); cons_cache[code] = set(m['con_code'].str[:6]); time.sleep(0.4)
    cons = cons_cache[code]
    sub = data[data['code6'].isin(cons)]
    for d, strength in vals.items():
        dd = sub[sub['trade_date'] == d]
        pct = dd['pct_chg']
        lg = (dd['buy_lg_amount'] - dd['sell_lg_amount']).fillna(0)   # 大单净额 万
        elg = (dd['buy_elg_amount'] - dd['sell_elg_amount']).fillna(0) # 特大单净额 万
        main = lg + elg
        md = (dd['buy_md_amount'] - dd['sell_md_amount']).fillna(0)
        net = dd['net_mf_amount'].fillna(0)
        rows.append(dict(
            bk=name, N=len(cons), date=d, strength=strength,
            S1=round(pct[pct>0].sum(),1),
            main_sum=round(main.sum(),0), main_pos=round(main[main>0].sum(),0),
            net_sum=round(net.sum(),0), elg_sum=round(elg.sum(),0), md_sum=round(md.sum(),0),
        ))
df = pd.DataFrame(rows)
df['K_S1'] = (df.strength/df.S1).round(2)
df['K_main'] = (df.strength/df.main_sum.replace(0,float('nan'))).round(3).astype('Float64')
df['K_mainpos'] = (df.strength/df.main_pos.replace(0,float('nan'))).round(3).astype('Float64')
df['K_net'] = (df.strength/df.net_sum.replace(0,float('nan'))).round(3).astype('Float64')
print(df.to_string(index=False))
for col in ['K_S1','K_main','K_mainpos','K_net']:
    k = pd.to_numeric(df[col], errors='coerce').dropna()
    if len(k)>2:
        print(f'{col}: 均值{k.mean():.2f} 中位{k.median():.2f} CV={k.std()/k.mean()*100:.1f}% n={len(k)}')
df.to_csv('data/strength_fit_v3.csv', index=False)
