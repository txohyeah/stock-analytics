#!/usr/bin/env python3
"""v7: 特征矩阵 + 非负最小二乘 / 逐步单特征精搜"""
import pandas as pd, numpy as np, sqlite3, json
from scipy.optimize import nnls

con = sqlite3.connect('data/stock.db')
ALL_DATES = ['20260930','20261008','20261009','20261012','20261013','20261014','20261015','20261016','20261019','20261020','20261021']
daily = pd.read_sql_query(
    f"SELECT trade_date, ts_code, pct_chg, amount FROM daily WHERE trade_date IN ({','.join('?'*len(ALL_DATES))})",
    con, params=ALL_DATES)
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
        N = len(dd)
        n_ge9 = int((pct>=9.8).sum()); n_19 = int((pct>=19.0).sum())
        n_ge5 = int(((pct>=5)&(pct<9.8)).sum()) + n_19
        n_ge3 = int(((pct>=3)&(pct<5)).sum())
        n_ge0 = int((pct>0).sum())
        amt = dd['amount'].sum()/1e5
        rows.append(dict(bk=bk, date=d, strength=r['strength'], N=N,
            x_ge10=n_ge9, x_ge5=n_ge5, x_ge3=n_ge3, x_up=n_ge0,
            x_S1=pct[pct>0].sum(), x_up_ratio=n_ge0/N, x_limit_ratio=n_ge9/N,
            x_amt=amt, x_amtN=amt/N, x_S1N=pct[pct>0].sum()/N,
            x_up_x_5=n_ge0*5, x_ge5_x3=n_ge5*3, x_S1_div_N2=pct[pct>0].sum()/np.sqrt(N),
        ))
df = pd.DataFrame(rows)
feats = ['x_ge10','x_ge5','x_ge3','x_up','x_S1','x_up_ratio','x_limit_ratio','x_amt','x_amtN','x_S1N','x_up_x_5','x_ge5_x3','x_S1_div_N2']
X = df[feats].values.astype(float); y = df['strength'].values.astype(float)

# NNLS
coef, err = nnls(X, y)
pred = X @ coef
ss = 1 - ((y-pred)**2).sum()/((y-y.mean())**2).sum()
print('== NNLS 线性 ==')
for f, c in zip(feats, coef):
    if c > 1e-6: print(f'  {f}: {c:.4f}')
print(f'R²={ss:.4f}')

# log-log OLS（弹性模型）
Xl = np.log(X + 1); yl = np.log(y)
coefl, *_ = np.linalg.lstsq(Xl, yl, rcond=None)
predl = np.exp(Xl @ coefl)
ssl = 1 - ((yl-np.log(predl))**2).sum()/((yl-yl.mean())**2).sum()
print('== log-log OLS ==')
for f, c in sorted(zip(feats, coefl), key=lambda t: -abs(t[1]))[:6]:
    print(f'  {f}: {c:.3f}')
print(f'R²(log空间)={ssl:.4f}')

# 残差大头
res = pd.DataFrame({'bk':df.bk,'date':df.date,'strength':y,'pred':pred.round(0)})
res['err%'] = ((res.pred-res.strength)/res.strength*100).round(1)
print('\n误差最差的 8 行:')
print(res.reindex(res['err%'].abs().sort_values(ascending=False).index).head(8).to_string(index=False))
df.to_csv('data/strength_fit_v7.csv', index=False)
