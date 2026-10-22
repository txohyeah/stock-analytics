#!/usr/bin/env python3
"""批量拉取同花顺概念+行业板块成分 → 缓存 json（断点续拉，限速 0.32s）"""
import tushare as ts, pandas as pd, json, os, time, sys

tok = open('/root/.qwenpaw/workspaces/default/.secrets/tushare_token_6000').read().strip()
pro = ts.pro_api(tok)
CACHE = 'data/ths_member_cache.json'
cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}

idx = pd.read_csv('data/ths_index_all.csv')
targets = idx[idx['type'].isin(['N', 'I'])][['ts_code', 'name', 'count']].sort_values('count', ascending=False)
todo = [(r['ts_code'], r['name']) for _, r in targets.iterrows() if r['ts_code'] not in cache]
batch = todo[:int(sys.argv[1])] if len(sys.argv) > 1 else todo
print(f'总板块 {len(targets)} 已缓存 {len(cache)} 本批 {len(batch)}', flush=True)
fail = []
for i, (code, name) in enumerate(batch):
    for attempt in range(3):
        try:
            m = pro.ths_member(ts_code=code)
            cache[code] = {'name': name, 'cons': sorted(set(m['con_code'].astype(str)))}
            break
        except Exception as e:
            if attempt == 2:
                fail.append(code); print(f'FAIL {code} {name}: {str(e)[:60]}', flush=True)
            time.sleep(1.5)
    time.sleep(0.32)
    if (i+1) % 100 == 0:
        json.dump(cache, open(CACHE, 'w'))
        print(f'  {i+1}/{len(batch)} 累计缓存 {len(cache)}', flush=True)
json.dump(cache, open(CACHE, 'w'))
print(f'完成: 缓存 {len(cache)} 失败 {len(fail)}', flush=True)
