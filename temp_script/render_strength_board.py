#!/usr/bin/env python3
"""自建板块强度 → 仿蓝海"历史对比"热力表渲染"""
import json, sqlite3
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.font_manager import FontProperties

FP = FontProperties(fname='/usr/share/fonts/google-droid/DroidSansFallback.ttf')
plt.rcParams['axes.unicode_minus'] = False

K = 5
DATES = ['20261021', '20261020', '20261019', '20261016', '20261015', '20261014']
DATE_LBL = {'20261021': '2026-10-21', '20261020': '2026-10-20', '20261019': '2026-10-19',
            '20261016': '2026-10-16', '20261015': '2026-10-15', '20261014': '2026-10-14'}

con = sqlite3.connect('data/stock.db')
daily = pd.read_sql_query(
    f"SELECT trade_date, ts_code, pct_chg FROM daily WHERE trade_date IN ({','.join('?'*len(DATES))})",
    con, params=DATES)
daily['code6'] = daily['ts_code'].str[:6]

cache = json.load(open('data/ths_member_cache.json'))
BLACKLIST = ['融资融券','沪股通','深股通','转债标的','富时罗素','MSCI','标普','证金','汇金','社保',
             '基金重仓','QFII','机构重仓','保险重仓','信托重仓','券商重仓','茅指数','宁组合','破净',
             '高送转','预亏','次新股','近期新高','近期新低','连续亏损','壳资源','ST','风险提示','B股',
             'AH','GDR','含可转债','独角兽','举牌','AB股','道琼斯','罗素','深成500','上证50','上证180',
             '上证380','中证500','沪深300','创业板综','百元股','低价股','微盘股','增持回购','减持',
             '股权激励','员工持股','高股息','市盈率','市净率','破发','破增','同花顺','中字头',
             '注册制次新股','科创板做市股','标准普尔','昨日','首板','连板','打板',
             '专精特新','国企改革','央企改革','华为概念','人工智能','芯片概念','新能源汽车',
             '人民币贬值受益','仿制药','军民融合','一带一路','华为汽车','小米概念','特斯拉概念',
             '比亚迪概念','安防','大数据','云计算','物联网','5G','6G','智能穿戴',
             '重组概念','并购重组','混改','自贸区','粤港澳大湾区','长三角','京津冀']
def keep(code, name):
    return code.startswith(('885', '886', '881')) and not any(k in name for k in BLACKLIST)

rows, seen = [], {}
for code, info in cache.items():
    name = info['name']
    if not keep(code, name):
        continue
    n = len(info['cons'])
    if name in seen and seen[name] >= n:
        continue
    seen[name] = n
    if name in [r['bk'] for r in rows]:
        rows = [r for r in rows if r['bk'] != name]
    cons = set(c[:6] for c in info['cons'])
    if len(cons) < 5:
        continue
    sub = daily[daily['code6'].isin(cons)]
    for d in DATES:
        dd = sub[sub['trade_date'] == d]
        if len(dd) == 0:
            continue
        s1 = dd['pct_chg'][dd['pct_chg'] > 0].sum()
        rows.append({'bk': name, 'date': d, 'S1': s1, 'strength': round(s1 * K)})
df = pd.DataFrame(rows)

def trunc(name, w=8.2):
    out, width = '', 0.0
    for ch_ in name:
        cw_ = 1.0 if ord(ch_) > 0x2E80 else 0.55
        if width + cw_ > w:
            return out + '…'
        out += ch_; width += cw_
    return out

tops = {}
for d in DATES:
    t = df[df['date'] == d].sort_values('strength', ascending=False).head(10)
    tops[d] = list(zip(t['bk'], t['strength']))

BG = '#2b1b16'; GRID = '#3a2a24'; TOPBAR = '#3a211b'
def cell_color(v, vmax):
    r = 0 if vmax <= 0 else min(v / vmax, 1.0)
    c0 = (166, 53, 40); c1 = (243, 230, 223)
    return tuple(round(c1[i] + (c0[i] - c1[i]) * (r ** 0.85)) / 255 for i in range(3))

NCOL, NROW = len(DATES), 10
cw, ch = 1.0, 0.52
fig_w, fig_h = NCOL * cw + 0.7, NROW * ch + 1.75
fig = plt.figure(figsize=(fig_w, fig_h), facecolor=BG)
ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, fig_w); ax.set_ylim(0, fig_h)
ax.axis('off'); ax.set_facecolor(BG)

ax.add_patch(Rectangle((0, fig_h - 0.62), fig_w, 0.62, facecolor=TOPBAR, edgecolor='none'))
ax.text(0.25, fig_h - 0.31, '历史对比', color='#f2e8dc', fontsize=15, weight='bold', va='center', fontproperties=FP)
ax.text(1.65, fig_h - 0.31, '板块类型: 题材概念+行业  ·  口径: Σ上涨股涨幅×5  ·  最近 6 个交易日',
        color='#c9a898', fontsize=9.5, va='center', fontproperties=FP)

x0 = 0.7
for j, d in enumerate(DATES):
    cx = x0 + j * cw
    ax.text(cx + cw/2, fig_h - 0.95, DATE_LBL[d], color='#e8d5c4', fontsize=11.5,
            ha='center', va='center', weight='bold', fontproperties=FP)
    vmax = tops[d][0][1] if tops[d] else 1
    for i in range(NROW):
        y = fig_h - 1.35 - (i + 1) * ch
        ax.text(0.35, y + ch/2, str(i + 1), color='#9c8478', fontsize=11, ha='center', va='center', fontproperties=FP)
        if i < len(tops[d]):
            bk, val = tops[d][i]
            fc = cell_color(val, vmax)
            ax.add_patch(Rectangle((cx + 0.04, y + 0.035), cw - 0.08, ch - 0.07,
                                   facecolor=fc, edgecolor=GRID, linewidth=0.8))
            tcolor = 'white' if val > vmax * 0.45 else '#5a231b'
            ax.text(cx + cw/2, y + ch/2 + 0.055, trunc(bk), color=tcolor, fontsize=10.5,
                    ha='center', va='center', fontproperties=FP)
            ax.text(cx + cw/2, y + ch/2 - 0.115, f'{val:,}', color=tcolor, fontsize=9.5,
                    ha='center', va='center', fontproperties=FP)
ax.text(fig_w/2, 0.18, '数据: 同花顺板块成分(ths_member) × 本地日线涨幅 | 自建口径, 与蓝海工具黑盒数值有差异, 量纲已对齐',
        color='#8a7061', fontsize=8.5, ha='center', fontproperties=FP)
out = '/root/.qwenpaw/workspaces/default/temp_script/ths_strength_board_20261022.png'
fig.savefig(out, dpi=150, facecolor=BG)
print('saved:', out)
