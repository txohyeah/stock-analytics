#!/usr/bin/env python3
"""板块强度公式系统识别 v5：锚点扩容至 45+（含 AI应用 7 日序列），统计量矩阵 + 单变量 CV 排序"""
import tushare as ts, pandas as pd, sqlite3, numpy as np, json

tok = open('/root/.qwenpaw/workspaces/default/.secrets/tushare_token_6000').read().strip()
idx = pd.read_csv('data/ths_index_all.csv')

# (锚点板块名, 同花顺板块候选名列表)
ANCHORS = [
    ('AI应用',    [('AI应用', 'N')], {'20261009': 14962, '20261012': 8161, '20261013': 4916,
                                      '20261014': 3542, '20261016': 3947, '20261020': 5289, '20261021': 4840}),
    ('文化传媒',   [('文化传媒概念','N'), ('文化传媒','I')], {'20261009': 6758, '20261012': 5829, '20261014': 2318}),
    ('机器人概念',  [('机器人概念','N')], {'20260930': 2053, '20261019': 8022, '20261020': 16972, '20261021': 4688}),
    ('低空经济',   [('低空经济','N')], {'20261016': 13111, '20261019': 2359}),
    ('军工',     [('军工','N')], {'20261014': 1434, '20261016': 8496}),
    ('锂电池',    [('锂电池','N')], {'20260930': 2850, '20261008': 6792, '20261009': 5338}),
    ('汽车零部件',  [('汽车零部件','I')], {'20260930': 2154, '20261016': 3775}),
    ('化工',     [('化工','I'), ('基础化工','I')], {'20260930': 2286, '20261013': 2171}),
    ('体育产业',   [('体育产业','N')], {'20261013': 5893, '20261014': 1090}),
    ('通信',     [('通信设备','I'), ('通信服务','I'), ('通信行业','I')], {'20261013': 6780, '20261015': 3125, '20261019': 3976}),
    ('数字经济',   [('数字经济','N')], {'20261021': 7300}),
    ('商业航天',   [('商业航天','N')], {'20261016': 4022}),
    ('算力',     [('东数西算(算力)','N')], {'20261021': 3649}),
    ('游戏',     [('游戏','I')], {'20261014': 1844, '20261015': 258}),
    ('证券',     [('证券','I')], {'20261009': 3331, '20261015': 831}),
    ('物流',     [('物流','I')], {'20261015': 1271}),
    ('智能电网',   [('智能电网','N')], {'20261012': 2232}),
    ('并购重组',   [('并购重组','N')], {'20261008': 1377, '20261012': 2360}),
    ('银行',     [('银行','I')], {'20261012': 1498}),
    ('芯片',     [('芯片概念','N'), ('中芯国际概念','N')], {'20261013': 3776}),
    ('股权转让',   [('股权转让','N')], {'20261013': 2436}),
    ('平台经济',   [('平台经济','N')], {'20261012': 1270}),
    ('石油石化',   [('石油行业','I'), ('石油石化','I')], {'20261008': 3210}),
    ('服装家纺',   [('纺织服装','I'), ('服装家纺','I')], {'20261008': 1281}),
    ('电源',     [('电源设备','N'), ('电源','I')], {'20261008': 1376}),
    ('生物质能',   [('生物质能','N')], {'20261008': 1210}),
    ('农业',     [('生态农业','N'), ('农业种植','N')], {'20261009': 2799}),
    ('酿酒',     [('酿酒行业','I')], {'20260930': 2129}),
]
ALL_DATES = ['20260930','20261008','20261009','20261012','20261013','20261014','20261015','20261016','20261019','20261020','20261021']

con = sqlite3.connect('data/stock.db')
daily = pd.read_sql_query(
    f"SELECT trade_date, ts_code, pct_chg, amount FROM daily WHERE trade_date IN ({','.join('?'*len(ALL_DATES))})",
    con, params=ALL_DATES)
daily['code6'] = daily['ts_code'].str[:6]
daily['is_st'] = daily['ts_code'].str.contains('ST', na=False)

cache = json.load(open('data/ths_member_cache.json'))
def find_code(cands):
    for name, typ in cands:
        hit = idx[(idx['name'] == name) & (idx['type'] == typ)].sort_values('count', ascending=False)
        if len(hit):
            return hit.iloc[0]['ts_code']
    return None

rows = []
for label, cands, vals in ANCHORS:
    code = find_code(cands)
    if not code or code not in cache:
        print(f'× {label}: 找不到成分 {cands}')
        continue
    cons = set(c[:6] for c in cache[code]['cons'])
    sub = daily[daily['code6'].isin(cons)]
    for d, strength in vals.items():
        dd = sub[sub['trade_date'] == d].copy()
        if len(dd) == 0:
            continue
        pct = dd['pct_chg']
        up = pct[pct > 0]
        # 涨停口径: 主板9.9+/创业板科创板19.8+/北交所29.8+
        lim = dd[(dd['pct_chg'] >= 19.8)] if True else None
        lim_main = dd[(dd['pct_chg'].between(9.8, 10.5)) & (dd['ts_code'].str[:2].isin(['60','00']))]
        lim_n = len(lim) + len(lim_main)
        rows.append(dict(
            bk=label, N=len(cons), date=d, strength=strength,
            up_n=len(up), up_ratio=len(up)/len(dd),
            S1=up.sum(), S1N=up.sum()/len(dd),
            avg_all=pct.mean(),
            limit_n=lim_n, limit_ratio=lim_n/len(dd),
            amt=dd['amount'].sum()/1e5, amtN=dd['amount'].sum()/1e5/len(dd),
            S5=pct.sum(),
        ))
df = pd.DataFrame(rows)
print(f'锚点数: {len(df)}  板块数: {df.bk.nunique()}')
# 单变量 CV: strength = K * feature
print(f'\n{"特征":<14}{"K中位":>10}{"CV%":>8}{"n":>4}')
res = []
for feat in ['S1','S1N','avg_all','up_n','up_ratio','limit_n','limit_ratio','amt','amtN','S5']:
    k = df['strength'] / df[feat].replace(0, np.nan)
    k = k.replace([np.inf,-np.inf], np.nan).dropna()
    if len(k) > 5:
        cv = k.std()/k.mean()*100
        res.append((feat, k.median(), cv, len(k)))
        print(f'{feat:<14}{k.median():>10.2f}{cv:>8.1f}{len(k):>4}')
# 多元回归（log-log）
feats = ['S1','amt','up_ratio','limit_n','N']
X = np.column_stack([np.log(df[f].clip(lower=1e-6)) for f in feats] + [np.ones(len(df))])
y = np.log(df['strength'].values)
coef, *_ = np.linalg.lstsq(X, y, rcond=None)
pred = np.exp(X @ coef)
r2 = 1 - ((y-np.log(pred))**2).sum()/((y-y.mean())**2).sum()
print(f'\nlog-log 回归 R²={r2:.4f}  系数: ' + ', '.join(f'{f}={c:.2f}' for f,c in zip(feats+["const"], coef)))
# AI应用日间序列对照
print('\nAI应用序列(锚点 vs 各口径):')
ai = df[df.bk=='AI应用'].sort_values('date')
print(ai[['date','strength','S1','S1N','amt','up_ratio','limit_n']].round(2).to_string(index=False))
df.to_csv('data/strength_fit_v5.csv', index=False)
