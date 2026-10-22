#!/usr/bin/env python3
"""全市场扫描同花顺个股概念标签（basic.10jqka.com.cn，无 cookie）
构建 概念 -> 成分股 倒排表，用于复现蓝海复盘笔记的"板块强度"。
输出: data/ths_concepts_scan.json = {concept_name: [code,...], ...}
      data/ths_stock_tags.json    = {code: [concept,...]}
"""
import sqlite3, json, re, time, random, os, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

BASE = '/home/application/stock-analytics'
OUT_TAGS = os.path.join(BASE, 'data/ths_stock_tags.json')
OUT_INV = os.path.join(BASE, 'data/ths_concepts_scan.json')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
TAG_RE = re.compile(r'href="javascript:void\(0\);"[^>]*>([^<]{2,16})<')

def get_codes():
    con = sqlite3.connect(os.path.join(BASE, 'data/stock.db'))
    rows = con.execute("select substr(ts_code,1,6) from stock_basic where list_status='L'").fetchall()
    con.close()
    return sorted(set(r[0] for r in rows))

def fetch_tags(code, session):
    url = f'http://basic.10jqka.com.cn/{code}/concept.html'
    for attempt in range(3):
        try:
            r = session.get(url, headers={'User-Agent': UA, 'Referer': f'http://basic.10jqka.com.cn/{code}/'},
                            timeout=12)
            if r.status_code == 200:
                html = r.content.decode('gbk', 'ignore')
                tags = [t.strip() for t in TAG_RE.findall(html) if t.strip()]
                return code, tags
            if r.status_code == 404:
                return code, []
        except Exception:
            pass
        time.sleep(0.5 + attempt * 0.8 + random.random() * 0.3)
    return code, None  # 彻底失败

def main():
    codes = get_codes()
    print(f'待扫描: {len(codes)} 只', flush=True)
    stock_tags, failed = {}, []
    done = 0
    
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=16) as pool:
        # 每线程独立 session
        def work(code):
            s = requests.Session()
            return fetch_tags(code, s)
        futs = {pool.submit(work, c): c for c in codes}
        for fut in as_completed(futs):
            code, tags = fut.result()
            done += 1
            if tags is None:
                failed.append(code)
            else:
                stock_tags[code] = tags
            if done % 500 == 0:
                print(f'  进度 {done}/{len(codes)} 失败{len(failed)} 用时{time.time()-t0:.0f}s', flush=True)
    print(f'完成: 成功{len(stock_tags)} 失败{len(failed)} 用时{time.time()-t0:.0f}s', flush=True)
    if failed:
        print('失败代码:', failed[:50], flush=True)
    # 倒排
    inv = {}
    for code, tags in stock_tags.items():
        for t in tags:
            inv.setdefault(t, []).append(code)
    json.dump(stock_tags, open(OUT_TAGS, 'w', encoding='utf-8'), ensure_ascii=False)
    json.dump(inv, open(OUT_INV, 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'概念总数: {len(inv)}', flush=True)
    # 快速 sanity check
    for c in ['机器人概念', '低空经济', '物理AI', '端侧AI', '储气罐', '数字经济', 'AI应用', '三季报增长']:
        n = len(inv.get(c, []))
        print(f'  {c}: {n} 只', flush=True)

if __name__ == '__main__':
    main()
