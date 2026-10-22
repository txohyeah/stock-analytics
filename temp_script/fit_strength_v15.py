#!/usr/bin/env python3
"""v15: 加入 884 二级行业成分 + 净涨幅项(可负) 的回归，检验能否解释负值锚点"""
import pandas as pd, numpy as np, sqlite3, json, warnings
warnings.filterwarnings('ignore')

con = sqlite3.connect('/home/application/stock-analytics/data/stock.db')
tds = [r[0] for r in con.execute("SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date BETWEEN '20260801' AND '20261022' ORDER BY cal_date")]
ph = ','.join('?'*len(tds))
daily = pd.read_sql_query(f"SELECT trade_date, ts_code, pct_chg FROM daily WHERE trade_date IN ({ph})", con, params=tds)
daily['code6'] = daily['ts_code'].str[:6]
cache = json.load(open('/home/application/stock-analytics/data/ths_member_cache.json'))
idx = pd.read_csv('/home/application/stock-analytics/data/ths_index_all.csv')

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
ALIAS = {'AI应用':'AI应用','机器人概念':'机器人概念','低空经济':'低空经济','军工':'军工','锂电池':['锂电池','锂电池概念'],
 '汽车零部件':'汽车零部件','体育产业':'体育产业','通信':['通信设备','通信'],'数字经济':'数字经济','商业航天':'商业航天',
 '算力':'东数西算(算力)','证券':'证券','智能电网':'智能电网','银行':'银行','芯片':['芯片概念','数字芯片设计'],'平台经济':'平台经济',
 '农业':'农业种植','文化传媒':'文化传媒概念','端侧AI':'端侧AI','物理AI':'物理AI','储气罐':'储气罐','三季报增长':'三季报增长',
 '北交所':'北交所','化工':['化工','氟化工','磷化工'],'医药':['医药','医药商业','化学制药'],'酿酒':'酿酒','电力':'电力',
 '港口':'港口','石油石化':'石油加工贸易','风电':'风电','海洋经济':'海洋经济','股权转让':'股权转让','并购重组':'并购重组',
 '地产链':'地产链','医疗器械':'医疗器械','疫苗概念':'疫苗概念','钠电池':'钠电池','食品饮料':'食品饮料','海峡两岸':'海峡两岸',
 '仪器仪表':'仪器仪表','基础建设':'基础建设','服装家纺':'服装家纺','外贸':'外贸','汽车类':'汽车','字节概念':'字节概念'}
def find(nm0, prefixes=('885','886','881','884')):
    cand = ALIAS.get(nm0, nm0)
    cands = cand if isinstance(cand, list) else [cand]
    for nm in cands:
        for seg in prefixes:
            hit = idx[(idx['name']==nm) & idx['ts_code'].str.startswith(seg) & idx['ts_code'].isin(cache.keys())]
            if len(hit): return hit.iloc[0]['ts_code']
        hit = idx[(idx['name']==nm) & idx['ts_code'].isin(cache.keys())]
        if len(hit): return hit.iloc[0]['ts_code']
    return None

rows=[]; unmatched=set()
for d,lst in {**SEPT, **OCT}.items():
    for bk,v in lst:
        code = find(bk)
        if not code:
            unmatched.add(bk); continue
        cons = set(x[:6] for x in cache[code]['cons'])
        sub = daily[(daily['code6'].isin(cons)) & (daily['trade_date']==d)]
        if len(sub)==0:
            unmatched.add(bk); continue
        up = sub[sub['pct_chg']>0]['pct_chg']
        lim = int(((sub['pct_chg']>=9.8)&(sub['ts_code'].str[:2].isin(['60','00'])) | (sub['pct_chg']>=19.8)).sum())
        big = int((sub['pct_chg']>=5).sum())
        rows.append(dict(bk=bk, date=d, actual=v, code=code, name=cache[code]['name'], n=len(sub),
                         lim=lim, big=big, s1=up.sum(), net=sub['pct_chg'].sum(),
                         avg=sub['pct_chg'].mean(), up_ratio=len(up)/len(sub)))
df = pd.DataFrame(rows)
print(f'匹配锚点 {len(df)} / {sum(len(v) for v in {**SEPT,**OCT}.values())}，未匹配: {sorted(unmatched)}')
print(f'负值/近零锚点:')
print(df[df.actual<600][['date','bk','actual','n','lim','big','s1','net','up_ratio']].to_string(index=False))

from scipy.optimize import minimize
from scipy.stats import spearmanr
def score(pv, name, mask=None):
    m = np.isfinite(pv)
    if mask is not None: m &= mask
    y = df['actual'].values
    ok = m & (pv>0) & (y>0)
    e = np.abs(np.log(pv[ok]/y[ok]))
    rhos=[]
    t = df.assign(p=pv)
    for d,g in t.groupby('date'):
        if len(g)>=4 and g['p'].nunique()>1: rhos.append(spearmanr(g['p'], g['actual']).correlation)
    print(f'{name:<30} ±{(np.exp(np.median(e))-1)*100:4.0f}% (±30%内{(e<np.log(1.3)).mean()*100:4.1f}%) ρ中位{np.nanmedian(rhos):5.2f}')

lim,big,net,s1 = df['lim'].values.astype(float), df['big'].values.astype(float), df['net'].values.astype(float), df['s1'].values.astype(float)
y = df['actual'].values.astype(float)

def loss(t):  # 对数空间 + 净额项
    a,b,c = t
    p = a*lim + b*big + c*net
    if (p<=0).any(): return 1e9
    return np.mean((np.log(p)-np.log(np.abs(y)))**2)
r = minimize(loss, [250,40,3], method='Nelder-Mead', options={'maxiter':8000})
a,b,c = r.x
print(f'\n拟合(涨停×{a:.0f} + 涨5%×{b:.0f} + 净Σ涨×{c:.2f}):')
score(a*lim+b*big+c*net, '含净额项')
score(243*lim+43*big+2.1*(s1-big), '现行生产口径(对照)')
print('\n负值锚点检验:')
for _,rr in df[df.actual<600].iterrows():
    p = a*rr['lim']+b*rr['big']+c*rr['net']
    print(f"  {rr['date']} {rr['bk']:<8} 实际{rr['actual']:>6} 预测{p:>8.1f} (涨停{rr['lim']} 涨5%{rr['big']} 净Σ涨{rr['net']:>7.1f})")
df.to_csv('/home/application/stock-analytics/data/strength_fit_v15.csv', index=False)
