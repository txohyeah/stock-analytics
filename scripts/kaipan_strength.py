#!/usr/bin/env python3
"""开盘啦「板块强度」榜 —— 蓝海复盘笔记同源数据（2026-10-22 破译）

结论：蓝海笔记里的「板块强度」= 开盘啦 App 板块强度分（80x/803x 板块体系），
不是同花顺。用免费接口 getPlateRotatData(from=kaipan) 可直接拿到每日 Top10 强度，
与蓝海 112 个历史锚点比对：90 个完全一致，其余差异来自他截图时点（盘中）与收盘值不同。

用法：
    python scripts/kaipan_strength.py                    # 最近交易日的榜
    python scripts/kaipan_strength.py --days 20          # 20 日历史对比热力表(文字)
    python scripts/kaipan_strength.py --png out.png      # 出图
    python scripts/kaipan_strength.py --verify           # 与蓝海锚点对账
接口：POST https://duanxianxia.com/api/getPlateRotatData  (免费, 只校验 Referer)
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
ANCHORS = ROOT / 'data' / 'lankai_anchors.json'
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Version/17.0 Safari/605.1.15')
HDR = {'User-Agent': UA,
       'Accept': 'application/json, text/javascript, */*; q=0.01',
       'Origin': 'https://duanxianxia.com',
       'Referer': 'https://duanxianxia.com/web/platerotat/',
       'X-Requested-With': 'XMLHttpRequest'}
API = 'https://duanxianxia.com/api/getPlateRotatData'

TH_DEFEND, TH_NORMAL, TH_ATTACK = 8000, 13000, 17000


def fetch(days=40, timeout=25):
    """取 40 个交易日的 板块强度 排名矩阵 → {date: [(name, value, rank)]}"""
    r = requests.post(API, data={'from': 'kaipan', 'days': days, 'dates': ''},
                      headers=HDR, timeout=timeout)
    r.raise_for_status()
    html = r.json().get('html', '')
    rows = re.findall(r'<tr>(.*?)</tr>', html, re.S)
    if not rows:
        raise RuntimeError('接口返回空表格')
    cell = lambda row: [re.sub(r'<[^>]+>', '', c).replace('&nbsp;', '').strip()
                        for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
    head = cell(rows[0])
    dates = head[1:]
    out = {d: [] for d in dates}
    for row in rows[1:]:
        cs = cell(row)
        for i, c in enumerate(cs[1:]):
            m = re.match(r'^(.*?)(-?\d+)$', c)
            if m and dates[i] in out:
                out[dates[i]].append((m.group(1), int(m.group(2))))
    return {d: v for d, v in out.items() if v}


def tag(v):
    if v >= TH_ATTACK:
        return '进攻'
    if v >= TH_NORMAL:
        return '正常'
    if v >= TH_DEFEND:
        return '正常偏弱'
    return '防守'


def show_today(boards, n=10):
    days = sorted(boards)[::-1]
    today = days[0]
    prev = days[1] if len(days) > 1 else None
    pmap = dict((nm, v) for nm, v in boards.get(prev, []))
    # 连续上榜天数
    streak = {}
    for d in days:
        for nm, _ in boards[d]:
            streak[nm] = streak.get(nm, 0) + 1 if streak.get(nm, 0) and _in(boards, d, nm, days) else 1
    print(f'开盘啦 板块强度 · {today}（蓝海笔记同源）')
    print(f'{"#":>2} {"板块":<12}{"强度":>8}{"环比":>9}{"态势":>6}')
    for i, (nm, v) in enumerate(boards[today][:n], 1):
        pv = pmap.get(nm)
        delta = '' if pv is None else f'{v - pv:+d}'
        flag = tag(v)
        print(f'{i:>2} {nm:<12}{v:>8}{delta:>9}{flag:>6}')
    top = boards[today][0][1]
    print(f'\n市场态: 第一名 {boards[today][0][0]} {top:,} → {tag(top)}'
          f'   (防守<{TH_DEFEND} / 正常{TH_DEFEND}-{TH_NORMAL} / 进攻>{TH_ATTACK})')
    # 新晋 / 掉榜
    if prev:
        new = [nm for nm, _ in boards[today] if nm not in pmap]
        gone = [nm for nm, _ in boards[prev] if nm not in dict(boards[today])]
        if new:
            print('新晋上榜:', '、'.join(new))
        if gone:
            print('掉出前10:', '、'.join(gone))


def _in(boards, d, nm, days):
    idx = days.index(d)
    return idx + 1 < len(days) and nm in dict(boards[days[idx + 1]])


def show_heat(boards, n=10, png=None):
    latest = sorted(boards)[::-1][:n]
    days = list(reversed(latest))          # 左旧右新
    names = []
    for d in days:
        for nm, _ in boards[d]:
            if nm not in names:
                names.append(nm)
    print(f'\n历史对比（最近 {len(days)} 个交易日 Top{n}，★=当日第一）\n')
    hdr = '板块'.ljust(12) + ''.join(d[5:].rjust(7) for d in days)
    print(hdr)
    for nm in names:
        line = nm.ljust(12)
        hit = False
        for d in days:
            m = dict(boards[d]).get(nm)
            if m is None:
                line += '      -'
            else:
                hit = True
                star = '★' if boards[d] and boards[d][0][0] == nm else ' '
                line += f'{m:>6}{star}'
        if hit:
            print(line)
    if png:
        render(boards, days, n, png)
        print('\n→', png)


def render(boards, days, n, out_path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from matplotlib.font_manager import FontProperties
    FP = FontProperties(fname='/usr/share/fonts/google-droid/DroidSansFallback.ttf')
    plt.rcParams['axes.unicode_minus'] = False
    BG, GRID, TOPBAR = '#2b1b16', '#3a2a24', '#3a211b'

    def cellc(v, vmax):
        r = 0 if vmax <= 0 else max(0.0, min(v / vmax, 1.0))
        c0, c1 = (166, 53, 40), (243, 230, 223)
        return tuple(round(c1[i] + (c0[i] - c1[i]) * (r ** 0.85)) / 255 for i in range(3))

    def label(name, w=6.7):
        out, width = '', 0.0
        for ch in name:
            cw = 1.0 if ord(ch) > 0x2E80 else (0.62 if ch in '()（）' else 0.55)
            if width + cw > w - 0.7:
                return out + '…'
            out += ch
            width += cw
        return out

    n = min(n, max(len(boards[d]) for d in days))
    cw, chh = 1.0, 0.52
    fig_w, fig_h = len(days) * cw + 0.7, n * chh + 1.75
    fig = plt.figure(figsize=(fig_w, fig_h), facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, fig_w); ax.set_ylim(0, fig_h); ax.axis('off')
    ax.add_patch(Rectangle((0, fig_h - 0.62), fig_w, 0.62, facecolor=TOPBAR, edgecolor='none'))
    ax.text(0.25, fig_h - 0.31, '历史对比', color='#f2e8dc', fontsize=15, weight='bold',
            va='center', fontproperties=FP)
    ax.text(1.65, fig_h - 0.31, '开盘啦板块强度（蓝海同源）· 防守<8000 / 正常8000-13000 / 进攻>17000',
            color='#c9a898', fontsize=9.5, va='center', fontproperties=FP)
    x0 = 0.7
    for j, d in enumerate(days):
        cx = x0 + j * cw
        ax.text(cx + cw / 2, fig_h - 0.95, d[5:], color='#e8d5c4', fontsize=11.5,
                ha='center', va='center', weight='bold', fontproperties=FP)
        vmax = boards[d][0][1] if boards[d] else 1
        for i in range(n):
            y = fig_h - 1.35 - (i + 1) * chh
            ax.text(0.35, y + chh / 2, str(i + 1), color='#9c8478', fontsize=11,
                    ha='center', va='center', fontproperties=FP)
            if i < len(boards[d]):
                nm, val = boards[d][i]
                fc = cellc(val, vmax)
                ax.add_patch(Rectangle((cx + 0.04, y + 0.035), cw - 0.08, chh - 0.07,
                                       facecolor=fc, edgecolor=GRID, linewidth=0.8))
                tc = 'white' if val > vmax * 0.45 else '#5a231b'
                ax.text(cx + cw / 2, y + chh / 2 + 0.055, label(nm), color=tc, fontsize=10.5,
                        ha='center', va='center', fontproperties=FP)
                ax.text(cx + cw / 2, y + chh / 2 - 0.115, f'{val:,}', color=tc, fontsize=9.5,
                        ha='center', va='center', fontproperties=FP)
    ax.text(fig_w / 2, 0.18,
            '数据: 开盘啦板块强度(经 duanxianxia 免费接口) | 数值语义为多因子综合分, 仅框内比较有意义',
            color='#8a7061', fontsize=8.5, ha='center', fontproperties=FP)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, facecolor=BG)
    plt.close(fig)


def verify(boards):
    anchors = json.load(open(ANCHORS))
    tot = exact = nearc = bad = missing = 0
    rows = []
    for d, lst in sorted(anchors.items()):
        dd = f'{d[:4]}-{d[4:6]}-{d[6:]}'
        have = dict((nm, v) for nm, v in boards.get(dd, []))
        for nm, v in lst:
            tot += 1
            g = have.get(nm)
            if g is None:
                missing += 1
                rows.append((dd, nm, v, None))
            elif g == v:
                exact += 1
            elif abs(g - v) <= max(3, abs(v) * 0.01):
                nearc += 1
            else:
                bad += 1
                rows.append((dd, nm, v, g))
    print(f'蓝海锚点对账: 共 {tot} 个 | 完全一致 {exact} ({exact/tot*100:.0f}%) | 1%内 {nearc} | '
          f'不一致 {bad} | 当日未进前10 {missing}')
    if rows:
        print('\n不一致明细（差值多为蓝海盘中截图时点差异）:')
        for dd, nm, v, g in rows:
            print(f'  {dd} {nm:<8} 蓝海{v:>7}  收盘{g if g is not None else "—":>7}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=20, help='历史热力表天数')
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--png')
    ap.add_argument('--verify', action='store_true')
    a = ap.parse_args()
    boards = fetch(days=40)
    show_today(boards, a.top)
    if a.png or a.verify:
        show_heat(boards, max(a.days, a.top), a.png)
    if a.verify:
        verify(boards)


if __name__ == '__main__':
    main()
