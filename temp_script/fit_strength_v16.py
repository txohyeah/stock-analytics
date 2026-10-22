#!/usr/bin/env python3
"""v16: 加"下跌股扣分项"的惩罚系数扫描（港口航运口径修正）"""
import pandas as pd, numpy as np, sqlite3, json, warnings
from scipy.stats import spearmanr
warnings.filterwarnings('ignore')
R='/home/application/stock-analytics'
con = sqlite3.connect(f'{R}/data/stock.db')
tds=[r[0] for r in con.execute("SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date BETWEEN '20260901' AND '20261022' ORDER BY cal_date")]
daily=pd.read_sql_query(f"SELECT trade_date,ts_code,pct_chg FROM daily WHERE trade_date IN ({','.join('?'*len(tds))})",con,params=tds)
daily['code6']=daily['ts_code'].str[:6]
cache=json.load(open(f'{R}/data/ths_member_cache.json')); idx=pd.read_csv(f'{R}/data/ths_index_all.csv')

ANCH = {
 '20260930': [('医药',7249),('锂电池',2850),('化工',2286),('汽车零部件',2154),('酿酒',2129),('机器人概念',2053),('AI应用',1944),('疫苗概念',1566),('股权转让',1465),('银行',1446)],
 '20260929': [('锂电池',7341),('通信',5926),('AI应用',5130),('地产链',3721),('并购重组',2567),('字节概念',2158),('算力',1998),('文化传媒',1957),('食品饮料',1611),('钠电池',1599)],
 '20260928': [('AI应用',2898),('风电',1746),('农业',1646),('并购重组',733),('酿酒',431),('海洋经济',265),('股权转让',265),('石油石化',129),('电力',57),('港口',-31)],
 '20260924': [('海峡两岸',6792),('机器人概念',3974),('仪器仪表',2085),('基础建设',1747),('服装家纺',1281),('外贸',1354),('芯片',1249),('文化传媒',1209),('农业',1155),('股权转让',887)],
 '20260923': [('芯片',6583),('通信',5446),('地产链',4676),('算力',4600),('仪器仪表',3999),('机器人概念',3776),('并购重组',1221),('汽车类',972),('汽车零部件',936),('股权转让',885)],
 '20260922': [('AI应用',8048),('芯片',6187),('医药',5196),('端侧AI',4373),('算力',3988),('并购重组',3226),('文化传媒',2126),('通信',2055),('股权转让',1788),('汽车零部件',1197)],
 '20260921': [('医药',12646),('地产链',7272),('AI应用',6189),('芯片',5416),('化工',5217),('医疗器械',3863),('通信',3549),('文化传媒',3263),('酿酒',3247),('农业',3241)],
 '20261009': [('AI应用',14962),('文化传媒',6758),('锂电池',5338),('医药',3922),('证券',3331),('农业',2799)],
 '20261012': [('AI应用',8161),('文化传媒',5829),('并购重组',2360),('智能电网',2232),('银行',1498),('平台经济',1270)],
 '20261013': [('通信',6780),('体育产业',5893),('AI应用',4916),('芯片',3776),('股权转让',2436),('化工',2171)],
 '20261016': [('低空经济',13111),('军工',8496),('商业航天',4022),('AI应用',3947),('汽车零部件',3775),('储气罐',3409)],
 '20261019': [('机器人概念',8022),('通信',3976),('化工',3058),('物理AI',2464),('低空经济',2359),('三季报增长',2260)],
 '20261020': [('机器人概念',16972),('端侧AI',7370),('三季报增长',5302),('AI应用',5289),('物理AI',3989),('北交所',1537)],
 '20261021': [('数字经济',7300),('AI应用',4840),('机器人概念',4688),('算力',3649),('医药',2493),('北交所',2115)],
}
ALIAS = {'AI应用':'AI应用','机器人概念':'机器人概念','低空经济':'低空经济','军工':'军工','锂电池':'锂电池',
 '汽车零部件':'汽车零部件','体育产业':'体育产业','通信':'通信设备','数字经济':'数字经济','商业航天':'商业航天',
 '算力':'东数西算(算力)','证券':'证券','智能电网':'智能电网','银行':'银行','芯片':'芯片概念','平台经济':'平台经济',
 '农业':'农业种植','文化传媒':'文化传媒概念','端侧AI':'端侧AI','物理AI':'物理AI','储气罐':'储气罐','三季报增长':'三季报增长',
 '北交所':'北交所','化工':'化工','医药':'医药','酿酒':'酿酒','电力':'电力','港口':'港口航运','石油石化':'石油加工贸易',
 '风电':'风电','海洋经济':'海洋经济','股权转让':'股权转让','并购重组':'并购重组','地产链':'地产链','医疗器械':'医疗器械',
 '疫苗概念':'疫苗概念','钠电池':'钠电池','食品饮料':'食品饮料','海峡两岸':'海峡两岸','仪器仪表':'仪器仪表',
 '基础建设':'基础建设','服装家纺':'服装家纺','外贸':'外贸','汽车类':'汽车类','字节概念':'字节概念'}
def find(nm0):
    nm=ALIAS.get(nm0,nm0)
    for seg in ('885','886','881','884'):
        hit=idx[(idx['name']==nm)&idx['ts_code'].str.startswith(seg)&idx['ts_code'].isin(cache.keys())]
        if len(hit): return hit.iloc[0]['ts_code']
    return None
rows=[];un=set()
for d,lst in ANCH.items():
    for bk,v in lst:
        code=find(bk)
        if not code: un.add(bk); continue
        cons=set(x[:6] for x in cache[code]['cons'])
        sub=daily[(daily['code6'].isin(cons))&(daily['trade_date']==d)]
        if not len(sub): un.add(bk); continue
        up=sub[sub.pct_chg>0].pct_chg; dn=sub[sub.pct_chg<0].pct_chg
        lim=int((((sub.pct_chg>=9.8)&sub.ts_code.str[:2].isin(['60','00']))|(sub.pct_chg>=19.8)).sum())
        big=int((sub.pct_chg>=5).sum())
        rows.append(dict(bk=bk,date=d,actual=v,n=len(sub),lim=lim,big=big,
                         rest=up.sum()-sub[(sub.pct_chg>=5)].pct_chg.sum(), dn=dn.sum(), s1=up.sum()))
df=pd.DataFrame(rows); y=df.actual.values.astype(float)
print(f'锚点 {len(df)} 个 未匹配 {sorted(un)}')
lim,big,rest,dn = (df[c].values.astype(float) for c in ('lim','big','rest','dn'))
def score(pv,name):
    ok=(pv>0)&(y>0)
    e=np.abs(np.log(pv[ok]/y[ok]))
    t=df.assign(p=pv); rhos=[spearmanr(g['p'],g['actual']).correlation for d,g in t.groupby('date') if len(g)>=4 and g['p'].nunique()>1]
    print(f'{name:<34} ±{(np.exp(np.median(e))-1)*100:4.0f}%  (±30%内{(e<np.log(1.3)).mean()*100:4.1f}%, ±50%内{(e<np.log(1.5)).mean()*100:4.1f}%)  ρ{np.nanmedian(rhos):5.2f}')
base = 243*lim+43*big+2.1*rest
score(base,'生产口径 243/43/2.1 无扣分')
for k in (0.5,1,1.4,2,3,4):
    score(base+k*dn, f'生产口径 + 扣分{k}×下跌Σ')
print()
# 港口/石油石化 专项
for _,r in df[df.actual<=200].iterrows():
    b=243*r['lim']+43*r['big']+2.1*r['rest']
    print(f"  {r['date']} {r['bk']:<6} 实际{r['actual']:>5} | 无扣分{b:>7.0f} | 扣1.4={b+1.4*r['dn']:>7.0f} | 净Σ={r['dn']+r['s1']:>7.1f}")
