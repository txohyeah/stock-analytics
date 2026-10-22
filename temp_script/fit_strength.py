#!/usr/bin/env python3
"""板块强度 PoC 拟合：
1) 同花顺板块指数日线（akshare）→ 每日涨跌幅
2) q.10jqka 详情页 → 成分总数 N（page_info x/y）
3) 用 N × 指数涨幅 近似等权平均涨幅，对截图锚点值回归，看比例系数 K 是否稳定
"""
import akshare as ak, warnings, re, time, json, urllib.request
warnings.filterwarnings('ignore')
import logging; logging.disable(logging.CRITICAL)
import pandas as pd

# 锚点: 板块(同花顺code) -> {日期: 强度值}
ANCHORS = {
    '概念': [
        ('机器人概念', '300816', {'20261019': 8022, '20261020': 16972}),
        ('低空经济', '309115', {'20261016': 13111, '20261019': 2359}),
        ('军工', '300082', {'20261014': 1434, '20261016': 8496}),
        ('AI应用', '309264', {'20261014': 3542, '20261016': 3947, '20261020': 5289, '20261021': 4840}),
        ('数字经济', '308870', {'20261021': 7300}),
        ('商业航天', '309130', {'20261016': 4022}),
        ('体育产业', '301605', {'20261014': 1090}),
        ('文化传媒概念', '300806', {'20261014': 2318}),
        ('2026三季报预增', '309274', {'20261015': 2927, '20261019': 2260, '20261020': 5302}),
        ('东数西算(算力)', '308828', {'20261021': 3649}),
    ],
    '行业': [
        ('游戏', '881275', {'20261014': 1844, '20261015': 258}),
        ('证券', '881157', {'20261015': 831}),
        ('物流', '881152', {'20261015': 1271}),
        ('汽车零部件', '881126', {'20261016': 3775}),
        ('通信设备', '881129', {'20261015': 3125, '20261019': 3976}),
    ],
}
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

def fetch_detail_total(code, kind):
    url = (f'http://q.10jqka.com.cn/gn/detail/code/{code}/' if kind == '概念'
           else f'http://q.10jqka.com.cn/thshy/detail/code/{code}/')
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    html = urllib.request.urlopen(req, timeout=15).read().decode('gbk', 'ignore')
    m = re.search(r'page_info">1/(\d+)</span>', html)
    return int(m.group(1)) * 10 if m else None

def idx_daily(name, kind):
    try:
        if kind == '概念':
            df = ak.stock_board_concept_index_ths(symbol=name, start_date='20261012', end_date='20261022')
        else:
            df = ak.stock_board_industry_index_ths(symbol=name, start_date='20261012', end_date='20261022')
    except Exception as e:
        print(f'  指数 FAIL {name}: {str(e)[:80]}')
        return None
    if df is None or len(df) == 0:
        return None
    df = df.sort_values(df.columns[0]).reset_index(drop=True)
    datecol = [c for c in df.columns if '日期' in c or 'date' in c.lower()][0]
    df['pct'] = (df['收盘价'] / df['收盘价'].shift(1) - 1) * 100
    df['date'] = df[datecol].astype(str).str[:10].str.replace('-', '')
    return df[['date', 'pct', '成交额']].set_index('date')

rows = []
for kind, items in ANCHORS.items():
    for name, code, vals in items:
        time.sleep(1.2)
        N = fetch_detail_total(code, kind)
        idx = idx_daily(name, kind)
        print(f'{kind}|{name}: N={N} 指数={"OK" if idx is not None else "FAIL"}', flush=True)
        if idx is None:
            continue
        for d, strength in vals.items():
            if d in idx.index and pd.notna(idx.loc[d, 'pct']):
                pct = float(idx.loc[d, 'pct'])
                if N and abs(pct) > 0.05:
                    K = strength / (N * pct)
                    rows.append((kind, name, d, strength, N, round(pct, 2), round(K, 2)))

print()
print(f'{"类型":<4}{"板块":<12}{"日期":<10}{"强度":>8}{"N":>7}{"指数%":>8}{"K":>8}')
Ks = []
for r in rows:
    print(f'{r[0]:<4}{r[1]:<12}{r[2]:<10}{r[3]:>8}{r[4]:>7}{r[5]:>8}{r[6]:>8}')
    Ks.append(r[6])
if Ks:
    import statistics
    print(f'\nK 均值={statistics.mean(Ks):.2f} 中位={statistics.median(Ks):.2f} '
          f'标准差={statistics.stdev(Ks):.2f} 变异系数={statistics.stdev(Ks)/statistics.mean(Ks)*100:.1f}%')
json.dump(rows, open('/tmp/strength_fit_rows.json', 'w'), ensure_ascii=False, indent=1)
