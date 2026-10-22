#!/usr/bin/env python3
"""v8: 换手/量比/振幅/放量特征 + NNLS + OOS(留 AI应用)"""
import pandas as pd, numpy as np, sqlite3, json
from scipy.optimize import nnls

con = sqlite3.connect('data/stock.db')
ALL_DATES = ['20260930','20261008','20261009','20261012','20261013','20261014','20261015','20261016','20261019','20261020','20261021']
ph = ','.join('?'*len(ALL_DATES))
daily = pd.read_sql_query(f"SELECT trade_date, ts_code, pct_chg, amount, vol, high, low, pre_close FROM daily WHERE trade_date IN ({ph})", con, params=ALL_DATES)
dbasic = pd.read_sql_query(f"SELECT trade_date, ts_code, turnover_rate, volume_ratio FROM daily_basic WHERE trade_date IN ({ph})", con, params=ALL_DATES)
daily = daily.merge(dbasic, on=['ts_code','trade_date'], how='left')
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
    # 全市场日成交额(算放量占比)
    for _, r in g.iterrows():
        d = str(r['date']).zfill(8)
        dd = sub[sub['trade_date']==d]
        if len(dd)==0: continue
        pct = dd['pct_chg']; N=len(dd)
        up = pct[pct>0]
        turn = dd['turnover_rate'].fillna(0)
        vr = dd['volume_ratio'].fillna(1)
        amp = ((dd['high']-dd['low'])/dd['pre_close']*100).fillna(0)
        rows.append(dict(bk=bk, date=d, strength=r['strength'], N=N,
            f1_turn_S1   = (turn*pct.clip(lower=0)).sum(),
            f2_turn_upN  = turn[pct>0].sum(),
            f3_vr_S1     = (vr*pct.clip(lower=0)).sum(),
            f4_vr_upN    = vr[pct>0].sum(),
            f5_amp_S1    = (amp*pct.clip(lower=0)).sum(),
            f6_amp_sum   = amp.sum(),
            f7_turn_all  = turn.sum(),
            f8_S1        = up.sum(),
            f9_amt       = dd['amount'].sum()/1e5,
            f10_limit    = int((pct>=9.8).sum()),
            f11_turn_max = turn.max(),
            f12_turnup_mean = turn[pct>0].mean() if (pct>0).any() else 0,
        ))
df = pd.DataFrame(rows)
feats = [c for c in df.columns if c.startswith('f')]
X = df[feats].values; y = df['strength'].values

print('== 全样本单特征 CV ==')
for f in feats:
    k = y/np.where(np.abs(df[f].values)<1e-9, np.nan, df[f].values)
    k = k[np.isfinite(k)]
    if len(k)>10:
        print(f'  {f:<14} CV={k.std()/k.mean()*100:6.1f}%  K={np.median(k):9.2f}')

# OOS: 留 AI应用
is_ai = (df.bk=='AI应用').values
coef, _ = nnls(X[~is_ai], y[~is_ai])
pred_ai = X[is_ai] @ coef
ai = df[is_ai]
print('\n== OOS: 31 锚点训练 -> 预测 AI应用 7 天 ==')
print(f'系数: ' + ', '.join(f'{f}={c:.2f}' for f,c in zip(feats,coef) if c>1e-6))
for (_, r), p in zip(ai.iterrows(), pred_ai):
    print(f'  {r.date}: 锚点{r.strength:8.0f} 预测{p:8.0f} 误差{(p-r.strength)/r.strength*100:+6.1f}%')
mape = np.abs((pred_ai-ai.strength.values)/ai.strength.values).mean()*100
print(f'AI应用 MAPE={mape:.1f}%')
# 全样本
coef2, _ = nnls(X, y)
r2 = 1 - ((y - X@coef2)**2).sum()/((y-y.mean())**2).sum()
print(f'全样本 NNLS R²={r2:.4f}')
df.to_csv('data/strength_fit_v8.csv', index=False)
