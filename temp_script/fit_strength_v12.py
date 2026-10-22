#!/usr/bin/env python3
"""v12: B版公式验证 + 归一化口径对比 (A=Σ涨×5 / B=涨停×570+涨5%×100+其余×5 / C=人均 / D=涨停家数单因子)"""
import pandas as pd, numpy as np, sqlite3, json

con = sqlite3.connect('data/stock.db')
cal = pd.read_sql_query("SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date BETWEEN '20260901' AND '20261022' ORDER BY cal_date", con)
tds = cal['cal_date'].tolist()
ph = ','.join('?'*len(tds))
daily = pd.read_sql_query(f"SELECT trade_date, ts_code, pct_chg FROM daily WHERE trade_date IN ({ph})", con, params=tds)
daily['code6'] = daily['ts_code'].str[:6]

cache = json.load(open('data/ths_member_cache.json'))
idx = pd.read_csv('data/ths_index_all.csv')

SEPT = {
 '20260930': [('医药',7249),('锂电池',2850),('化工',2286),('汽车零部件',2154),('酿酒',2129),('机器人概念',2053),('AI应用',1944),('疫苗概念',1566),('股权转让',1465),('银行',1446)],
 '20260929': [('锂电池',7341),('通信',5926),('AI应用',5130),('地产链',3721),('并购重组',2567),('字节概念',2158),('算力',1998),('文化传媒',1957),('食品饮料',1611),('钠电池',1599)],
 '20260928': [('AI应用',2898),('风电',1746),('农业',1646),('并购重组',733),('酿酒',431),('海洋经济',265),('股权转让',265),('石油石化',129),('电力',57),('港口',-31)],
 '20260924': [('海峡两岸',6792),('机器人概念',3974),('仪器仪表',2085),('基础建设',1747),('服装家纺',1281),('外贸',1354),('芯片',1249),('文化传媒',1209),('农业',1155),('股权转让',887)],
 '20260923': [('芯片',6583),('通信',5446),('地产链',4676),('算力',4600),('仪器仪表',3999),('机器人概念',3776),('并购重组',1221),('汽车类',972),('汽车零部件',936),('股权转让',885)],
 '20260922': [('AI应用',8048),('芯片',6187),('医药',5196),('端侧AI',4373),('算力',3988),('并购重组',3226),('文化传媒',2126),('通信',2055),('股权转让',1788),('汽车零部件',1197)],
 '20260921': [('医药',12646),('地产链',7272),('AI应用',6189),('芯片',5416),('化工',5217),('医疗器械',3863),('通信',3549),('文化传媒',3263),('酿酒',3247),('农业',3241)],
}
OCT = {
 '20261009': [('AI应用',14962),('文化传媒',6758),('锂电池',5338),('医药',3922),('证券',3331),('农业',2799)],
 '20261012': [('AI应用',8161),('文化传媒',5829),('并购重组',2360),('智能电网',2232),('银行',1498),('平台经济',1270)],
 '20261013': [('通信',6780),('体育产业',5893),('AI应用',4916),('芯片',3776),('股权转让',2436),('化工',2171)],
 '20261016': [('低空经济',13111),('军工',8496),('商业航天',4022),('AI应用',3947),('汽车零部件',3775),('储气罐',3409)],
 '20261019': [('机器人概念',8022),('通信',3976),('化工',3058),('物理AI',2464),('低空经济',2359),('三季报增长',2260)],
 '20261020': [('机器人概念',16972),('端侧AI',7370),('三季报增长',5302),('AI应用',5289),('物理AI',3989),('北交所',1537)],
 '20261021': [('数字经济',7300),('AI应用',4840),('机器人概念',4688),('算力',3649),('医药',2493),('北交所',2115)],
}
NAME2CODE = {'AI应用':'AI应用','机器人概念':'机器人概念','低空经济':'低空经济','军工':'军工','锂电池':'锂电池',
 '汽车零部件':'汽车零部件','体育产业':'体育产业','通信':'通信设备','数字经济':'数字经济','商业航天':'商业航天',
 '算力':'东数西算(算力)','游戏':'游戏','证券':'证券','物流':'物流','智能电网':'智能电网','银行':'银行',
 '芯片':'芯片概念','平台经济':'平台经济','农业':'农业种植','文化传媒':'文化传媒概念','端侧AI':'端侧AI',
 '物理AI':'物理AI','储气罐':'储气罐','三季报增长':'三季报增长','北交所':'北交所','化工':'化工','医药':'医药',
 '酿酒':'酿酒','电力':'电力','港口':'港口','石油石化':'石油石化','风电':'风电','海洋经济':'海洋经济',
 '股权转让':'股权转让','并购重组':'并购重组','地产链':'地产链','医疗器械':'医疗器械','疫苗概念':'疫苗概念',
 '钠电池':'钠电池','食品饮料':'食品饮料','海峡两岸':'海峡两岸','仪器仪表':'仪器仪表','基础建设':'基础建设',
 '服装家纺':'服装家纺','外贸':'外贸','汽车类':'汽车类','字节概念':'字节概念'}
def find(nm0):
    nm = NAME2CODE.get(nm0, nm0)
    for seg in ('885','886','881','883'):
        hit = idx[(idx['name']==nm) & idx['ts_code'].str.startswith(seg)]
        if len(hit): return hit.iloc[0]['ts_code']
    return None

def is_limit(row):
    c, p = row['ts_code'][:2], row['pct_chg']
    if p is None or pd.isna(p): return False
    if c in ('60','00'): return p >= 9.8
    if c in ('30','68'): return p >= 19.8
    if c == '8' or c == '4' or row['ts_code'][:3] in ('830','831','832','833','834','835','836','837','838','839','870','871','872','873','874','833'): 
        return p >= 29.5
    return p >= 9.8

def calc(code, d):
    cons = set(x[:6] for x in cache[code]['cons'])
    sub = daily[(daily['code6'].isin(cons)) & (daily['trade_date']==d)]
    if len(sub)==0: return None
    n_all = len(sub)
    up = sub[sub['pct_chg']>0]['pct_chg']
    lim = int(sub.apply(is_limit, axis=1).sum())
    big = int((sub['pct_chg']>=5).sum())
    rest_up = sub[(sub['pct_chg']>0) & (sub['pct_chg']<5)]['pct_chg'].sum()
    s1 = up.sum()
    A = s1*5
    B = lim*570 + big*100 + rest_up*5
    C = s1/n_all*100            # 人均口径(每只成分股平均涨幅×100)
    D = lim*1000                # 涨停单因子(万级量纲)
    return dict(n=n_all, up_ratio=len(up)/n_all, s1=s1, lim=lim, big=big, A=A, B=B, C=C, D=D)

rows=[]
for d,lst in {**SEPT, **OCT}.items():
    for bk,v in lst:
        code = find(bk)
        if not code or code not in cache: continue
        st = calc(code,d)
        if not st: continue
        st.update(bk=bk, date=d, actual=v, code=code)
        rows.append(st)
df = pd.DataFrame(rows)
print(f'可算锚点 {len(df)} 个，{df.date.nunique()} 天\n')

def ev(col, name):
    sub = df[(df[col]>0) & (df['actual']>0)].copy()
    sub['ratio'] = sub[col]/sub['actual']
    mape = (sub['ratio'].apply(np.log).abs()).median()
    within = ((sub['ratio']>0.7)&(sub['ratio']<1.43)).mean()*100
    # 同日排序一致性
    rhos=[]
    for d,g in sub.groupby('date'):
        if len(g)>=4 and g[col].nunique()>1:
            rhos.append(g[col].corr(g['actual'], method='spearman'))
    print(f'{name:<22} 中位误差(ln)={mape:.3f} → ±{(np.exp(mape)-1)*100:5.1f}%  "{name}"±30%内占比 {within:5.1f}%  同日排序ρ中位 {np.median(rhos):.2f} (n={len(rhos)}天)')
    return mape, within

print('==== 单日口径（当日涨跌，含当日窗口）====')
for col,name in [('A','A: Σ涨×5'),('B','B: 涨停570+涨5%100+其余5'),('C','C: 人均涨幅×100'),('D','D: 涨停×1000')]:
    ev(col,name)
print()
print('==== 分板块明细（|实际|>=2000）====')
dd = df[df['actual'].abs()>=2000].copy()
dd['A_r']=dd['A']/dd['actual']; dd['B_r']=dd['B']/dd['actual']; dd['C_r']=dd['C']/dd['actual']
dd = dd.sort_values(['date','actual'], ascending=[True,False])
print(f"{'日期':<10}{'板块':<10}{'实际':>7}{'成分':>5}{'上涨%':>6}{'涨停':>5}{'涨5%':>5}{'A':>8}{'B':>9}{'A倍':>6}{'B倍':>6}{'C':>7}")
for _,r in dd.iterrows():
    print(f"{r['date']:<10}{r['bk']:<10}{r['actual']:>7.0f}{r['n']:>5}{r['up_ratio']*100:>6.0f}{r['lim']:>5}{r['big']:>5}{r['A']:>8.0f}{r['B']:>9.0f}{r['A_r']:>6.2f}{r['B_r']:>6.2f}{r['C']:>7.1f}")
df.to_csv('data/strength_fit_v12.csv', index=False)
print('\n→ data/strength_fit_v12.csv')
