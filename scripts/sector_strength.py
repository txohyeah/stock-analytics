#!/usr/bin/env python3
"""板块强度榜（自建口径，仿蓝海复盘笔记）

公式（v14 终版，61 锚点标定）:
    强度 = 涨停家数 × 243 + 涨5%以上家数 × 43 + 其余上涨股涨幅和 × 2.1
等价于蓝海量纲的 B 口径整体缩放 1/2.35，保留其 8000 / 1.3万 / 1.7万 三档阈值语义。
同日排序一致性（vs 蓝海锚点）ρ=0.80；量级中位误差 ±49%。

用法:
    python scripts/sector_strength.py                # 最近交易日
    python scripts/sector_strength.py 20261022       # 指定日期
    python scripts/sector_strength.py 20261022 --top 15 --png out.png
"""
import argparse, json, sqlite3, sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / 'data' / 'stock.db'
CACHE = ROOT / 'data' / 'ths_member_cache.json'
INDEX_CSV = ROOT / 'data' / 'ths_index_all.csv'

K_LIM, K_BIG, K_REST = 243.0, 43.0, 2.1
TH_DEFEND, TH_NORMAL, TH_ATTACK = 8000, 13000, 17000

# 市场标签 / 大杂烩概念，剔除（只留纯题材 + 同花顺行业）
BLACKLIST = ['融资融券', '沪股通', '深股通', '转债标的', '富时罗素', 'MSCI', '标普', '证金', '汇金', '社保',
             '基金重仓', 'QFII', '机构重仓', '保险重仓', '信托重仓', '券商重仓', '茅指数', '宁组合', '破净',
             '高送转', '预亏', '次新股', '近期新高', '近期新低', '连续亏损', '壳资源', 'ST', '风险提示', 'B股',
             'AH', 'GDR', '含可转债', '独角兽', '举牌', 'AB股', '道琼斯', '罗素', '深成500', '上证50', '上证180',
             '上证380', '中证500', '沪深300', '创业板综', '百元股', '低价股', '微盘股', '增持回购', '减持',
             '股权激励', '员工持股', '高股息', '市盈率', '市净率', '破发', '破增', '同花顺', '中字头',
             '注册制次新股', '科创板做市股', '标准普尔', '昨日', '首板', '连板', '打板',
             '专精特新', '国企改革', '央企改革', '华为概念', '人工智能', '芯片概念', '新能源汽车',
             '人民币贬值受益', '仿制药', '军民融合', '一带一路', '华为汽车', '小米概念', '特斯拉概念',
             '比亚迪概念', '安防', '大数据', '云计算', '物联网', '5G', '6G', '智能穿戴',
             '重组概念', '并购重组', '混改', '自贸区', '粤港澳大湾区', '长三角', '京津冀']


def is_new_listing(name):
    """N/C 开头 = 上市首日/上市不足五日的新股，涨幅不可比，剔除"""
    if not name:
        return False
    return name[:1] in ('N', 'C') and not name.startswith('CQ')


def is_limit_up(ts_code, pct, name):
    if is_new_listing(name):
        return False
    if name and 'ST' in name:
        return pct >= 4.8
    pre = ts_code[:2]
    if pre in ('60', '00'):
        return pct >= 9.8
    if pre in ('30', '68'):
        return pct >= 19.8
    if pre in ('83', '87', '88', '43', '92'):
        return pct >= 29.5
    return pct >= 9.8


def load_members():
    cache = json.load(open(CACHE))
    idx = pd.read_csv(INDEX_CSV)
    out = {}
    for code, info in cache.items():
        name = info['name']
        if not code.startswith(('885', '886', '881')):
            continue
        if any(k in name for k in BLACKLIST):
            continue
        cons = set(c[:6] for c in info['cons'])
        if len(cons) < 5:
            continue
        prev = out.get(name)
        if prev and len(prev['cons']) >= len(cons):
            continue
        out[name] = {'code': code, 'cons': cons}
    return out, idx


def trade_days(con, n_back=30, end=None):
    sql = "SELECT cal_date FROM trade_cal WHERE is_open=1"
    params = []
    if end:
        sql += " AND cal_date<=?"
        params.append(end)
    sql += " ORDER BY cal_date DESC LIMIT ?"
    params.append(n_back)
    rows = [r[0] for r in con.execute(sql, params)]
    return sorted(rows)


def build(date, top=12):
    con = sqlite3.connect(DB)
    members, _ = load_members()
    ph = ','.join('?' * len(members))
    names = list(members)
    d = pd.read_sql_query(
        "SELECT d.ts_code, d.pct_chg, b.name FROM daily d LEFT JOIN stock_basic b ON b.ts_code=d.ts_code "
        "WHERE d.trade_date=?", con, params=[date])
    con.close()
    d['code6'] = d['ts_code'].str[:6]
    pct = dict(zip(d['code6'], d['pct_chg']))
    nm = dict(zip(d['code6'], d['name']))
    limset, bigset, upmap = set(), set(), {}
    for c6, p in pct.items():
        if pd.isna(p) or is_new_listing(nm.get(c6)):
            continue
        if is_limit_up(c6, p, nm.get(c6)):
            limset.add(c6)
        if p >= 5:
            bigset.add(c6)
        if p > 0:
            upmap[c6] = p

    rows = []
    for name, info in members.items():
        cons = info['cons']
        n = len(cons)
        live = [c for c in cons if c in pct]
        if len(live) < 5:
            continue
        lim = len(cons & limset)
        big = len(cons & bigset)
        ups = [upmap[c] for c in cons if c in upmap]
        s1 = sum(ups)
        rest = s1 - sum(pct[c] for c in cons & bigset if c in upmap and c not in limset)
        strength = K_LIM * lim + K_BIG * big + K_REST * rest
        rows.append(dict(bk=name, code=info['code'], n=len(live), lim=lim, big=big,
                         up_ratio=len(ups) / len(live), s1=s1, strength=round(strength),
                         per_cap=round(s1 / len(live) * 100, 1)))
    df = pd.DataFrame(rows).sort_values('strength', ascending=False)
    return df, date


def tag(v):
    if v >= TH_ATTACK:
        return '进攻'
    if v >= TH_NORMAL:
        return '正常'
    if v >= TH_DEFEND:
        return '正常偏弱'
    return '防守'


def _width(s):
    w = 0.0
    for ch_ in s:
        if ord(ch_) > 0x2E80:
            w += 1.0
        elif ch_ in '()（）[]{}':
            w += 0.62
        else:
            w += 0.55
    return w


def _label(name, w=6.0):
    """榜内显示名：太长先去掉括号补充（数据中心(AIDC)→数据中心），再硬截断"""
    import re
    s = name
    if _width(s) > w:
        s2 = re.sub(r'[（(][^）)]*[）)]', '', s).strip()
        if s2 and _width(s2) <= w:
            return s2
    out, width = '', 0.0
    for ch_ in s:
        cw_ = 1.0 if ord(ch_) > 0x2E80 else (0.62 if ch_ in '()（）[]{}' else 0.55)
        if width + cw_ > w - 0.7:
            return out + '…'
        out += ch_
        width += cw_
    return out


def render(boards, out_path, nrow=10):
    """boards: {date: DataFrame(已按强度降序)} → 仿蓝海历史对比热力表"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from matplotlib.font_manager import FontProperties

    FP = FontProperties(fname='/usr/share/fonts/google-droid/DroidSansFallback.ttf')
    plt.rcParams['axes.unicode_minus'] = False
    BG, GRID, TOPBAR = '#2b1b16', '#3a2a24', '#3a211b'
    dates = list(boards)

    def cell(v, vmax):
        r = 0 if vmax <= 0 else min(v / vmax, 1.0)
        c0, c1 = (166, 53, 40), (243, 230, 223)
        return tuple(round(c1[i] + (c0[i] - c1[i]) * (r ** 0.85)) / 255 for i in range(3))

    cw, chh = 1.0, 0.52
    fig_w, fig_h = len(dates) * cw + 0.7, nrow * chh + 1.75
    fig = plt.figure(figsize=(fig_w, fig_h), facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, fig_w); ax.set_ylim(0, fig_h); ax.axis('off')
    ax.add_patch(Rectangle((0, fig_h - 0.62), fig_w, 0.62, facecolor=TOPBAR, edgecolor='none'))
    ax.text(0.25, fig_h - 0.31, '历史对比', color='#f2e8dc', fontsize=15, weight='bold',
            va='center', fontproperties=FP)
    ax.text(1.65, fig_h - 0.31,
            f'题材概念+行业  ·  口径: 涨停×{K_LIM:.0f}+涨5%以上×{K_BIG:.0f}+其余×{K_REST}  ·  最近 {len(dates)} 个交易日',
            color='#c9a898', fontsize=9.5, va='center', fontproperties=FP)
    x0 = 0.7
    for j, d in enumerate(dates):
        cx = x0 + j * cw
        ax.text(cx + cw / 2, fig_h - 0.95, f'{d[:4]}-{d[4:6]}-{d[6:]}', color='#e8d5c4',
                fontsize=11.5, ha='center', va='center', weight='bold', fontproperties=FP)
        tb = boards[d]
        vmax = tb.iloc[0]['strength'] if len(tb) else 1
        for i in range(nrow):
            y = fig_h - 1.35 - (i + 1) * chh
            ax.text(0.35, y + chh / 2, str(i + 1), color='#9c8478', fontsize=11,
                    ha='center', va='center', fontproperties=FP)
            if i < len(tb):
                bk, val = tb.iloc[i]['bk'], int(tb.iloc[i]['strength'])
                fc = cell(val, vmax)
                ax.add_patch(Rectangle((cx + 0.04, y + 0.035), cw - 0.08, chh - 0.07,
                                       facecolor=fc, edgecolor=GRID, linewidth=0.8))
                tc = 'white' if val > vmax * 0.45 else '#5a231b'
                ax.text(cx + cw / 2, y + chh / 2 + 0.055, _label(bk), color=tc, fontsize=10.5,
                        ha='center', va='center', fontproperties=FP)
                ax.text(cx + cw / 2, y + chh / 2 - 0.115, f'{val:,}', color=tc, fontsize=9.5,
                        ha='center', va='center', fontproperties=FP)
    ax.text(fig_w / 2, 0.18,
            '数据: 同花顺板块成分(ths_member) × 本地日线涨幅 | 自建口径, 量纲对齐蓝海阈值, 数值与其黑盒工具存在 ±50% 级差异',
            color='#8a7061', fontsize=8.5, ha='center', fontproperties=FP)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, facecolor=BG)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('date', nargs='?')
    ap.add_argument('--top', type=int, default=12)
    ap.add_argument('--cmp', type=int, default=0, help='对比前 N 个交易日出热力表(png)')
    ap.add_argument('--png')
    a = ap.parse_args()
    con = sqlite3.connect(DB)
    date = a.date or con.execute("SELECT max(trade_date) FROM daily").fetchone()[0]
    con.close()
    df, date = build(date, a.top)
    top = df.head(a.top)
    print(f'板块强度榜 {date}  (口径: 涨停×{K_LIM:.0f} + 涨5%以上×{K_BIG:.0f} + 其余涨幅×{K_REST:.1f})')
    print(f'{"#":>2} {"板块":<10}{"强度":>8}{"涨停":>5}{"涨5%":>6}{"成分":>5}{"上涨%":>7}{"人均":>7}  态势')
    for i, r in enumerate(top.itertuples(), 1):
        print(f'{i:>2} {r.bk:<10}{r.strength:>8}{r.lim:>5}{r.big:>6}{r.n:>5}{r.up_ratio*100:>6.0f}%{r.per_cap:>7}  {tag(r.strength)}')
    print(f'\n市场态: 第一名 {top.iloc[0].bk} {top.iloc[0].strength:,} → {tag(top.iloc[0].strength)}'
          f'  (阈值 防守<{TH_DEFEND} / 正常{TH_DEFEND}-{TH_NORMAL} / 进攻>{TH_ATTACK})')
    print(f'覆盖板块 {len(df)} 个（已剔除市场标签与大杂烩概念）')

    if a.cmp:
        con = sqlite3.connect(DB)
        days = trade_days(con, a.cmp, end=date)
        con.close()
        boards = {}
        for dd in days:
            f, _ = build(dd)
            boards[dd] = f.head(max(a.top, 10))
        out = a.png or str(ROOT / 'reports' / f'sector_strength_{date}.png')
        render(boards, out)
        print('→', out)


if __name__ == '__main__':
    main()
