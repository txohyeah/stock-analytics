#!/usr/bin/env python3
"""补齐 ths_member_cache: 884 段(同花顺二级行业 259 个) 成分"""
import json, time, sys
import pandas as pd

sys.path.insert(0, '/home/application/stock-analytics')
import tushare as ts

ROOT = '/home/application/stock-analytics'
tok = open('/root/.qwenpaw/workspaces/default/.secrets/tushare_token_6000').read().strip()
pro = ts.pro_api(tok)
cache = json.load(open(f'{ROOT}/data/ths_member_cache.json'))
idx = pd.read_csv(f'{ROOT}/data/ths_index_all.csv')
codes = idx[idx.ts_code.str.startswith('884')].ts_code.tolist()
todo = [c for c in codes if c not in cache]
print(f'884 段 {len(codes)} 个, 待拉 {len(todo)}')
ok = fail = 0
for i, c in enumerate(todo, 1):
    for attempt in range(3):
        try:
            df = pro.ths_member(ts_code=c)
            if len(df):
                nm = idx.loc[idx.ts_code == c, 'name'].iloc[0]
                cache[c] = {'name': nm, 'cons': df['con_code'].tolist(),
                            'con_names': df['con_name'].tolist()}
                ok += 1
            break
        except Exception as e:
            time.sleep(1.5)
    else:
        fail += 1
        print('FAIL', c)
    if i % 50 == 0:
        print(f'  {i}/{len(todo)} ok={ok} fail={fail}')
        json.dump(cache, open(f'{ROOT}/data/ths_member_cache.json', 'w'), ensure_ascii=False)
    time.sleep(0.12)
json.dump(cache, open(f'{ROOT}/data/ths_member_cache.json', 'w'), ensure_ascii=False)
print(f'完成 ok={ok} fail={fail} 缓存总数={len(cache)}')
