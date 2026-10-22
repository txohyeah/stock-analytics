#!/usr/bin/env python3
"""v10: 决定性验证 —— 强度 = 最近 10 个交易日累计涨幅统计？
用 09-28 港口(-31)/电力(57)/石油石化(129) 等小值锚点精确校准窗口与公式
"""
import pandas as pd, numpy as np, sqlite3, json

con = sqlite3.connect('data/stock.db')
cal = pd.read_sql_query("SELECT cal_date, is_open FROM trade_cal WHERE cal_date BETWEEN '20260901' AND '20261022' ORDER BY cal_date", con)
tds = cal[cal['is_open']==1]['cal_date'].tolist()
ALL = tds  # 全部交易日
ph = ','.join('?'*len(ALL))
daily = pd.read_sql_query(f"SELECT trade_date, ts_code, pct_chg FROM daily WHERE trade_date IN ({ph})", con, params=ALL)
daily['code6'] = daily['ts_code'].str[:6]
cache = json.load(open('data/ths_member_cache.json'))
idx = pd.read_csv('data/ths_index_all.csv')

def find(name_kw_list):
    for kw, typ in name_kw_list:
        hit = idx[(idx['name']==kw) & (idx['type']==typ)].sort_values('count', ascending=False)
        if len(hit): return hit.iloc[0]['ts_code']
    # 模糊
    for kw, typ in name_kw_list:
        hit = idx[idx['name'].str.contains(kw, na=False) & (idx['type']==typ)].sort_values('count', ascending=False)
        if len(hit): return hit.iloc[0]['ts_code']
    return None

# 锚点: (板块, 日期, 强度)
TEST = [
    ('港口',    [('港口','I'),('航运港口','I'),('港口航运','I')], '20260928', -31),
    ('电力',    [('电力','I'),('电力行业','I')], '20260928', 57),
    ('石油石化', [('石油石化','I'),('石油行业','I'),('油气开采','I')], '20260928', 129),
    ('酿酒',    [('酿酒行业','I')], '20260928', 431),
    ('AI应用',  [('AI应用','N')], '20260928', 2898),
    ('AI应用',  [('AI应用','N')], '20261012', 8161),
    ('AI应用',  [('AI应用','N')], '20261009', 14962),
    ('机器人概念',[('机器人概念','N')], '20261020', 16972),
    ('机器人概念',[('机器人概念','N')], '20261019', 8022),
    ('低空经济', [('低空经济','N')], '20261016', 13111),
    ('医药',    [('医药商业','I'),('化学制药','I')], '20260930', 7249),  # 医药映射不可靠,仅参考
]
WND = 10  # 最近10个交易日(含当日)

print(f'{"板块":<8}{"日期":<10}{"锚点":>8} | 窗口起点 | S5累计×5  S1累计×5  S1累计×10')
for bk, cands, d, strength in TEST:
    code = find(cands)
    if not code or code not in cache:
        print(f'{bk:<8}{d} 找不到板块成分 ({cands})'); continue
    cons = set(c[:6] for c in cache[code]['cons'])
    sub = daily[daily['code6'].isin(cons)]
    i = tds.index(d)
    win = tds[max(0, i-WND+1): i+1]
    w = sub[sub['trade_date'].isin(win)]
    daily_sum = w.groupby('trade_date')['pct_chg'].sum()
    s5_win = daily_sum.sum()          # 全成分每日涨幅和,窗口累计(含跌)
    s1_win = w[w['pct_chg']>0].groupby('trade_date')['pct_chg'].sum().sum()  # 只算上涨,窗口累计
    start = win[0]
    print(f'{bk:<8}{d}  {strength:>8} | {start} | {s5_win*5:>9.0f} {s1_win*5:>9.0f} {s1_win*10:>9.0f}')
