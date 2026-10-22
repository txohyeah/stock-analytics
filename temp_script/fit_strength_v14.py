#!/usr/bin/env python3
"""v14: 候选公式总评(误差+排序) 选出终版口径"""
import pandas as pd, numpy as np
from scipy.stats import spearmanr
from scipy.optimize import minimize

df = pd.read_csv('/home/application/stock-analytics/data/strength_fit_v12.csv')
df = df[df['actual']!=0].reset_index(drop=True)
y = df['actual'].astype(float)

def score(pv, name):
    pv = np.asarray(pv, float)
    ok = pv>0
    e = np.abs(np.log(pv[ok]/y[ok]))
    rhos=[]
    for d,g in df.groupby('date'):
        if len(g)>=4 and pv[g.index].std()>0:
            rhos.append(spearmanr(pv[g.index], g['actual']).correlation)
    print(f'{name:<34} 中位±{(np.exp(np.median(e))-1)*100:5.0f}%  ±30%内{(e<np.log(1.3)).mean()*100:5.1f}%  ±50%内{(e<np.log(1.5)).mean()*100:5.1f}%  ρ中位{np.nanmedian(rhos):5.2f}')
    return np.median(e), np.nanmedian(rhos)

lim, big, s1, A, B = df['lim'].values.astype(float), df['big'].values.astype(float), df['s1'].values.astype(float), df['A'].values.astype(float), df['B'].values.astype(float)

score(A, 'A: Σ涨×5')
score(B, 'B原始: 570/100/5')
score(B/2.35, 'B原始 整体缩放÷2.35')
score(lim*1000, 'D: 涨停×1000')
score(s1*8, 'Σ涨×8')

# 对数空间最小二乘(U型误差最小化): 最小化 Σ(ln p - ln y)^2, p = w1*lim + w2*big
def logloss(theta):
    w1, w2 = theta
    p = w1*lim + w2*big
    if (p<=0).any(): return 1e9
    return np.mean((np.log(p)-np.log(y))**2)
from scipy.optimize import minimize
r = minimize(logloss, [300,50], method='Nelder-Mead')
w1,w2 = r.x
print(f'\n对数拟合系数: 涨停×{w1:.0f} + 涨5%以上×{w2:.0f}  (最小化中位对数误差)')
score(w1*lim+w2*big, f'终版BG: {w1:.0f}×涨停 + {w2:.0f}×涨5%以上')

# 三因子(加 其余上涨涨幅)
def logloss3(t):
    a,b,c = t
    p = a*lim + b*big + c*(s1-big*0)
    if (p<=0).any(): return 1e9
    return np.mean((np.log(p)-np.log(y))**2)
r3 = minimize(logloss3, [300,40,1], method='Nelder-Mead', options={'maxiter':5000})
a,b,c = r3.x
print(f'三因子对数拟合: 涨停×{a:.0f} + 涨5%×{b:.0f} + Σ涨×{c:.2f}')
score(a*lim+b*big+c*s1, '三因子版')

print('\n阈值换算(终版BG口径):')
for tg in (8000,13000,17000):
    print(f'  {tg}: 纯涨停={tg/w1:.0f}家 | 纯涨5%以上={tg/w2:.0f}家')
print('\n各锚点终版BG实测值:')
df['BG'] = w1*lim+w2*big
print(df[['date','bk','actual','lim','big','BG']].sort_values(['date','actual'],ascending=[True,False]).to_string(index=False))
