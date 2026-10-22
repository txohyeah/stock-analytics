#!/usr/bin/env python3
"""v13: B版系数标定(NNLS) + 留一天交叉验证 + 量纲系数收敛"""
import pandas as pd, numpy as np
from scipy.optimize import nnls

df = pd.read_csv('/home/application/stock-analytics/data/strength_fit_v12.csv')
df = df[(df['actual']!=0)].copy()

# 特征: 涨停家数, 涨5%以上家数(含涨停), 其余上涨股涨幅和, 成分数(规模项)
X = np.column_stack([df['lim'], df['big'], df['s1']-df['big'].clip(lower=0)*0, df['n']]).astype(float)
X[:,2] = df['s1']  # 全部上涨涨幅和(与big共线, 用NNLS自动分配)
y = df['actual'].astype(float)

def fit(Xm, ym):
    w,_ = nnls(Xm, ym)
    return w

w_all = fit(X, y)
pred = X@w_all
r = pred/y
print('全样本 NNLS 系数: 涨停={:.1f}  涨5%以上={:.1f}  Σ涨={:.2f}  成分数={:.2f}'.format(*w_all))
print('  全样本中位|ln误差| = {:.3f} → ±{:.0f}%'.format(np.median(np.abs(np.log(r))), (np.exp(np.median(np.abs(np.log(r))))-1)*100))
print('  R2 = {:.4f}'.format(1-((y-pred)**2).sum()/((y-y.mean())**2).sum()))

# 留一天交叉验证
days = df['date'].unique()
errs=[]; rows=[]
for d in days:
    tr = df['date']!=d; te = ~tr
    Xtr, ytr, Xte, yte = X[tr.values], y[tr.values], X[te.values], y[te.values]
    w = fit(Xtr, ytr)
    p = Xte@w
    e = np.abs(np.log(p/yte))
    errs.extend(e.tolist())
    for i,(dte,rv) in enumerate(zip(df.loc[te,'bk'], yte)):
        rows.append((d if False else d, rv, p[i]))
    print(f'  LODO {d}: 系数 涨停={w[0]:6.0f} 涨5%={w[1]:6.0f} Σ涨={w[2]:5.2f} 规模={w[3]:5.2f} | 该日中位误差 {np.median(e)*100:5.1f}%')
errs=np.array(errs)
print('\nLODO 总体: 中位|ln误差|={:.3f} → ±{:.0f}%   ±30%内占比 {:.1f}%   ±50%内 {:.1f}%'.format(
    np.median(errs), (np.exp(np.median(errs))-1)*100, (errs<np.log(1.3)).mean()*100, (errs<np.log(1.5)).mean()*100))

# 简化双因子版(涨停 + Σ涨), 排除规模项, 看是否更稳
w2 = fit(X[:, [0,2]], y)
p2 = X[:,[0,2]]@w2
print('\n简化版(涨停×{:.0f} + Σ涨×{:.1f}): 中位|ln误差|={:.3f} → ±{:.0f}%'.format(
    w2[0], w2[1], np.median(np.abs(np.log(p2/y))), (np.exp(np.median(np.abs(np.log(p2/y))))-1)*100))

# 排序能力对比: 同日 Spearman
from scipy.stats import spearmanr
print('\n同日排序一致性(ρ):')
for name, col in [('A:Σ涨×5','A'), ('B原始','B'), ('人均C','C')]:
    rhos=[]
    for d in days:
        g = df[df['date']==d]
        if len(g)>=4 and g[col].nunique()>1:
            rhos.append(spearmanr(g[col], g['actual']).correlation)
    print(f'  {name:<10} ρ中位 {np.nanmedian(rhos):.2f}')
for name, pv in [('B标定NNLS', pred), ('简化2因子', p2)]:
    t = df.copy(); t['p']=pv
    rhos=[spearmanr(g['p'], g['actual']).correlation for d,g in t.groupby('date') if len(g)>=4 and g['p'].nunique()>1]
    print(f'  {name:<10} ρ中位 {np.nanmedian(rhos):.2f}')

print('\n阈值换算(简化2因子 反推):')
for tg in (8000, 13000, 17000):
    print(f'  强度 {tg}: 若全由涨停贡献 = {tg/w2[0]:.0f} 家涨停; 若纯涨幅贡献 = {tg/w2[1]:.0f} 点Σ涨幅')
