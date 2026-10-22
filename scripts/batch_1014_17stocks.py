# -*- coding: utf-8 -*-
"""1014 批 17 只站外 A 股批量取数（大金八步 1/2/5 步数据基础）"""
import sqlite3

db = sqlite3.connect('/home/application/stock-analytics/data/stock.db')
db.row_factory = sqlite3.Row

STOCKS = [
    ("000338.SZ", "潍柴动力"), ("603308.SH", "应流股份"), ("002518.SZ", "科士达"),
    ("002028.SZ", "思源电气"), ("600885.SH", "宏发股份"), ("600183.SH", "生益科技"),
    ("688019.SH", "安集科技"), ("688256.SH", "寒武纪"), ("603986.SH", "兆易创新"),
    ("301269.SZ", "华大九天"), ("002475.SZ", "立讯精密"), ("002916.SZ", "深南电路"),
    ("002463.SZ", "沪电股份"), ("601138.SH", "工业富联"), ("002594.SZ", "比亚迪"),
    ("002600.SZ", "领益智造"), ("002472.SZ", "双环传动"),
]

def q(sql, args=()):
    return db.execute(sql, args).fetchall()

def one(sql, args=()):
    return db.execute(sql, args).fetchone()

for code, name in STOCKS:
    print(f"\n{'='*70}\n■ {name} {code}")
    # 1. 现价（原始 close）与当日涨跌
    r = one("SELECT trade_date, close, pct_chg FROM daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1", (code,))
    last_td, last_close, last_pct = r[0], r[1], r[2]
    # 2. 后复权序列：YTD、52 周高低
    rows = q("""SELECT d.trade_date, d.close*qf.adj_factor AS adj FROM daily d
        JOIN adj_factor qf ON d.ts_code=qf.ts_code AND d.trade_date=qf.trade_date
        WHERE d.ts_code=? AND d.trade_date>='20250101' ORDER BY d.trade_date""", (code,))
    if rows:
        adj_2501 = rows[0][1]
        last_adj = rows[-1][1]
        ytd = (last_adj/adj_2501 - 1) * 100
        hi = max(r2[1] for r2 in rows); lo = min(r2[1] for r2 in rows)
        print(f"现价 {last_close} ({last_td}, {last_pct:+.2f}%) | YTD {ytd:+.1f}% | 距52周高 {(last_adj/hi-1)*100:+.1f}% | 距52周低 {(last_adj/lo-1)*100:+.1f}%")
    # 3. 估值与 3 年分位
    b = one("SELECT trade_date, pe_ttm, pb, total_mv FROM daily_basic WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1", (code,))
    if b:
        pe, pbv, mv = b[1], b[2], b[3]
        hists = q("SELECT pe_ttm, pb FROM daily_basic WHERE ts_code=? AND trade_date>='20231014' AND pe_ttm IS NOT NULL AND pe_ttm>0", (code,))
        pes = sorted(h[0] for h in hists); pbs = sorted(h[1] for h in hists if h[1] and h[1] > 0)
        def pctile(arr, v):
            if not arr or v is None: return None
            return sum(1 for x in arr if x <= v) / len(arr) * 100
        pe_pct = pctile(pes, pe) if pe and pe > 0 else None
        pb_pct = pctile(pbs, pbv) if pbv else None
        pe_str = f"PE {pe:.1f}({pe_pct:.0f}%位)" if pe and pe > 0 else "PE 亏损/失效"
        pb_str = f"PB {pbv:.2f}({pb_pct:.0f}%位)" if pbv else "PB -"
        print(f"市值 {mv/1e4:.0f} 亿 | {pe_str} | {pb_str}")
    # 4. 财务增速：25 年报 / 26H1
    def to_f(v):
        try: return float(v)
        except (TypeError, ValueError): return None
    for label, end in [("2025年报", "20251231"), ("2026H1", "20260630")]:
        f = one("""SELECT or_yoy, netprofit_yoy, dt_netprofit_yoy, grossprofit_margin, roe, debt_to_assets
            FROM fina_indicator WHERE ts_code=? AND end_date=? ORDER BY ann_date DESC LIMIT 1""", (code, end))
        if f:
            fy = [to_f(x) for x in f]
            if fy[0] is not None and fy[1] is not None:
                print(f"{label}: 营收{fy[0]:+.1f}% 归母{fy[1]:+.1f}% 扣非{(fy[2] if fy[2] is not None else float('nan')):+.1f}% 毛利率{fy[3]:.1f}% ROE{fy[4]:.1f}% 负债率{fy[5]:.0f}%")
            else:
                print(f"{label}: 部分字段缺失 {tuple(f)}")
        else:
            print(f"{label}: 无记录")
    # 5. 单季拆解：26Q2 vs 25Q2 归母（负基数检测）
    def nattr(end):
        r2 = one("SELECT n_income_attr_p FROM income WHERE ts_code=? AND end_date=? AND report_type='1' ORDER BY ann_date DESC LIMIT 1", (code, end))
        return to_f(r2[0]) if r2 else None
    h26, q126, h25, q125 = nattr('20260630'), nattr('20260331'), nattr('20250630'), nattr('20250331')
    if h26 and q126 is not None and h25 and q125 is not None:
        q226, q225 = h26 - q126, h25 - q125
        if q225 and q225 > 0:
            print(f"26Q2单季归母 {q226/1e8:.2f} 亿 vs 25Q2 {q225/1e8:.2f} 亿 → 单季 {(q226/q225-1)*100:+.1f}%")
        else:
            print(f"26Q2单季 {q226/1e8:.2f} 亿 vs 25Q2 {q225} 亿（基期≤0，同比失真）")
    elif h26:
        print(f"26H1 归母 {h26/1e8:.2f} 亿（单季拆解缺基期）")
    # 6. 经营现金流 + 合同负债
    cf26 = one("SELECT n_cashflow_act FROM cashflow WHERE ts_code=? AND end_date='20260630' AND report_type='1' ORDER BY ann_date DESC LIMIT 1", (code,))
    cf25 = one("SELECT n_cashflow_act FROM cashflow WHERE ts_code=? AND end_date='20250630' AND report_type='1' ORDER BY ann_date DESC LIMIT 1", (code,))
    if cf26 and cf25 and cf25[0]:
        print(f"经营现金流 26H1 {cf26[0]/1e8:.1f} 亿 vs 25H1 {cf25[0]/1e8:.1f} 亿 ({(cf26[0]/cf25[0]-1)*100:+.0f}%)")
    cl26 = one("SELECT contract_liab FROM balancesheet WHERE ts_code=? AND end_date='20260630' ORDER BY ann_date DESC LIMIT 1", (code,))
    cl25 = one("SELECT contract_liab FROM balancesheet WHERE ts_code=? AND end_date='20251231' ORDER BY ann_date DESC LIMIT 1", (code,))
    if cl26 and cl26[0] and cl25 and cl25[0]:
        print(f"合同负债 26H1 {to_f(cl26[0])/1e8:.1f} 亿 vs 25年报 {to_f(cl25[0])/1e8:.1f} 亿 ({((to_f(cl26[0])/to_f(cl25[0]))-1)*100:+.0f}%)")
