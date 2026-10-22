#!/usr/bin/env python3
"""v6: 假设 强度≈资金活跃度×波动 (用户提示'占比'方向) —— 组合特征 CV 排序"""
import pandas as pd, numpy as np, sqlite3, json

con = sqlite3.connect('data/stock.db')
ALL_DATES = ['20260930','20261008','20261009','20261012','20261013','20261014','20261015','20261016','20261019','20261020','20261021']
daily = pd.read_sql_query(
    f"SELECT trade_date, ts_code, pct_chg, amount FROM daily WHERE trade_date IN ({','.join('?'*len(ALL_DATES))})",
    con, params=ALL_DATES)
daily['code6'] = daily['ts_code'].str[:6]
cache = json.load(open('data/ths_member_cache.json'))

# 锚点(复用 v5 的 19 板块 38 锚点)
v5 = pd.read_csv('data/strength_fit_v5.csv')
# 重新计算全统计量(含组合)
idx_map = v5[['bk','N']].drop_duplicates().set_index('bk')['N'].to_dict()
# 从 v5 拿 bk->板块代码: 重新按名字找
idx = pd.read_csv('data/ths_index_all.csv')
name_map = {'AI应用':'AI应用','文化传媒':'文化传媒','机器人概念':'机器人概念','低空经济':'低空经济',
            '军工':'军工','锂电池':'锂电池','汽车零部件':'汽车零部件','体育产业':'体育产业','通信':'通信设备',
            '数字经济':'数字经济','商业航天':'商业航天','算力':'东数西算(算力)','游戏':'游戏','证券':'证券',
            '物流':'物流','智能电网':'智能电网','银行':'银行','芯片':'芯片概念','平台经济':'平台经济','农业':'农业种植'}
rows = []
for bk, g in v5.groupby('bk'):
    code = None
    for c in ['885','886','881']:
        hit = idx[(idx['name']==name_map.get(bk,bk)) & idx['ts_code'].str.startswith(c)]
        if len(hit): code = hit.iloc[0]['ts_code']; break
    if not code or code not in cache: continue
    cons = set(x[:6] for x in cache[code]['cons'])
    sub = daily[daily['code6'].isin(cons)]
    for _, r in g.iterrows():
        d = str(r['date']).zfill(8)
        dd = sub[sub['trade_date']==d]
        if len(dd)==0: continue
        pct = dd['pct_chg']; amt = dd['amount'].sum()/1e5; N = len(dd)
        rows.append(dict(bk=bk, date=d, strength=r['strength'], N=N,
            f_amt_absavg = amt*abs(pct.mean()),
            f_amt_S1N = amt*(pct[pct>0].sum()/N),
            f_amt_absS5N = amt*abs(pct.sum())/N,
            f_amt_std = amt*pct.std(),
            f_amt_maxabs = amt*pct.abs().max(),
            f_amtavg2 = amt*pct.mean()**2,
            f_amtS1 = amt*pct[pct>0].sum()/100,
            f_amtS5 = amt*pct.sum()/100,
            f_wavg_abs = (dd['amount']*pct.abs()).sum()/1e5/ (dd['amount'].sum()/1e5),
            f_wavg = (dd['amount']*pct).sum()/1e5/(dd['amount'].sum()/1e5),
            f_S1_upratio = pct[pct>0].sum()* (pct>0).mean(),
        ))
df = pd.DataFrame(rows)
print(f'n={len(df)}')
print(f'{"特征":<16}{"K中位":>9}{"CV%":>8}')
for c in [c for c in df.columns if c.startswith('f_')]:
    k = df['strength']/df[c].replace(0,np.nan)
    k = k.replace([np.inf,-np.inf],np.nan).dropna()
    if len(k)>10:
        print(f'{c:<16}{k.median():>9.3f}{k.std()/k.mean()*100:>8.1f}')
print('\nAI应用序列(强度 vs f_amt_absavg/f_wavg):')
ai = df[df.bk=='AI应用'].sort_values('date')
print(ai[['date','strength','f_amt_absavg','f_wavg_abs','f_amt_std']].round(2).to_string(index=False))
df.to_csv('data/strength_fit_v6.csv', index=False)
