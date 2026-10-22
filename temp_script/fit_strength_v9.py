#!/usr/bin/env python3
"""v9: 分级家数模型（主板涨停/20cm涨停分开计权）+ OOS"""
import pandas as pd, numpy as np, sqlite3, json
from scipy.optimize import nnls

con = sqlite3.connect('data/stock.db')
ALL_DATES = ['20260930','20261008','20261009','20261012','20261013','20261014','20261015','20261016','20261019','20261020','20261021']
ph = ','.join('?'*len(ALL_DATES))
daily = pd.read_sql_query(f"SELECT trade_date, ts_code, pct_chg, amount FROM daily WHERE trade_date IN ({ph})", con, params=ALL_DATES)
daily['code6'] = daily['ts_code'].str[:6]
cache = json.load(open('data/ths_member_cache.json'))
v5 = pd.read_csv('data/strength_fit_v5.csv')
idx = pd.read_csv('data/ths_index_all.csv')
name_map = {'AI应用':'AI应用','文化传媒':'文化传媒概念','机器人概念':'机器人概念','低空经济':'低空经济',
            '军工':'军工','锂电池':'锂电池','汽车零部件':'汽车零部件','体育产业':'体育产业','通信':'通信设备',
            '数字经济':'数字经济','商业航天':'商业航天','算力':'东数西算(算力)','游戏':'游戏','证券':'证券',
            '物流':'物流','智能电网':'智能电网','银行':'银行','芯片':'芯片概念','平台经济':'平台经济','农业':'农业种植'}

rows = []
for bk, g in v5.groupby('bk'):
    hit = idx[(idx['name']==name_map.get(bk,bk)) & idx['ts_code'].str.startswith(('885','886','881'))]
    if not len(hit): continue
    code = hit.iloc[0]['ts_code']
    if code not in cache: continue
    cons = set(x[:6] for x in cache[code]['cons'])
    sub = daily[daily['code6'].isin(cons)]
    for _, r in g.iterrows():
        d = str(r['date']).zfill(8)
        dd = sub[sub['trade_date']==d]
        if len(dd)==0: continue
        pct = dd['pct_chg']
        pre = dd['ts_code'].str[:2]
        n_l10 = int(((pct.between(9.8,10.6)) & pre.isin(['60','00'])).sum())
        n_l20 = int((pct.between(19.8,20.6) & pre.isin(['30','68'])).sum())
        n_l30 = int((pct>=29.0).sum())
        n_5p  = int(((pct>=5)&(pct<9.8)).sum() + ((pct>=15)&(pct<19.8)).sum())
        n_3p  = int(((pct>=3)&(pct<5)).sum() + ((pct>=10)&(pct<15)).sum())
        n_0p  = int(((pct>0)&(pct<3)).sum() + ((pct>0)&(pct<10)&pre.isin(['30','68'])).sum())
        rows.append(dict(bk=bk, date=d, strength=r['strength'],
            L10=n_l10, L20=n_l20, L30=n_l30, P5=n_5p, P3=n_3p, P0=n_0p))
df = pd.DataFrame(rows)
F = ['L10','L20','L30','P5','P3','P0']
X = df[F].values.astype(float); y = df['strength'].values.astype(float)

coef, _ = nnls(X, y)
r2 = 1 - ((y-X@coef)**2).sum()/((y-y.mean())**2).sum()
print('== 分级家数 NNLS (全样本) ==')
print('强度 = ' + ' + '.join(f'{c:.1f}×{f}' for f,c in zip(F,coef) if c>1e-6))
print(f'R²={r2:.4f}')

is_ai = (df.bk=='AI应用').values
coef_oos, _ = nnls(X[~is_ai], y[~is_ai])
pred = X[is_ai] @ coef_oos
ai = df[is_ai]
print('\n== OOS: 31 训练 → AI应用 7 天 ==')
print('系数: ' + ', '.join(f'{f}={c:.1f}' for f,c in zip(F,coef_oos) if c>1e-6))
for (_, r), p in zip(ai.iterrows(), pred):
    print(f'  {r.date}: 锚点{r.strength:8.0f} 预测{p:8.0f} 误差{(p-r.strength)/r.strength*100:+6.1f}%')
print(f'AI应用 MAPE={np.abs((pred-ai.strength.values)/ai.strength.values).mean()*100:.1f}%')
# 全体 OOS: leave-one-bank-out 按 MAPE
err_all = []
for bk in df.bk.unique():
    m = df.bk != bk
    c, _ = nnls(X[m.values], y[m.values])
    p = X[~m.values] @ c
    err_all.append(np.abs((p - y[~m.values])/y[~m.values]).mean()*100)
print(f'\nLeave-one-板块-out 平均 MAPE={np.mean(err_all):.1f}%')
df.to_csv('data/strength_fit_v9.csv', index=False)
