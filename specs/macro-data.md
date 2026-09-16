# 宏观数据集（A+B+C）— 实现文档

> 2026-09-16 落地。起因：中叶投研部 0916 策略篇把 8 月社融写成"预期 2.3 万亿、实际 1.66 万亿、
> 9,000 亿凭空蒸发"，本地无宏观数据无法核对 → 补齐数据层。
> 状态：**A/B/C 已落地并跑通**（D「站点宏观页」待议）。

## 1. 目标

1. 让"研报里的宏观数字"能在本地**逐条核对**，而不是照抄；
2. 建立**预期差**（实际公布值 − 市场预期）这一可量化信号；
3. 提供资金面/杠杆资金日频背景（Shibor、两融、北向）。

## 2. 数据集（`app/sync/registry.py`）

| dataset | tushare 接口 | 表 | 主键 | 策略 | 覆盖（实测 2026-09-16） |
|---|---|---|---|---|---|
| `sf_month` | sf_month | sf_month | month | basic（全量） | 200201 → 202607 |
| `cn_m` | cn_m | cn_m | month | basic（全量） | 197801 → 202607 |
| `cn_cpi` | cn_cpi | cn_cpi | month | basic（全量） | 195112 → **202608** |
| `cn_ppi` | cn_ppi | cn_ppi | month | basic（全量） | 197812 → **202608** |
| `cn_gdp` | cn_gdp | cn_gdp | quarter | basic（全量） | 1952Q4 → 2026Q2 |
| `macro_calendar` | eco_cal | macro_calendar | date,time,country,event | macro_calendar | 2018-01-07 → 至今 |
| `shibor` | shibor | shibor | date | date_range | 2018-01-02 → 今日 |
| `margin` | margin | margin | trade_date,exchange_id | date_range | 2018-01-02 → 最新交易日 |
| `moneyflow_hsgt` | moneyflow_hsgt | moneyflow_hsgt | trade_date | date_range | 2018-01-02 → 最新交易日 |

分组（`app/cli.py` GROUPS）：`macro_monthly`（5 个月度序列）、`macro_daily`（日历 + 三个日频）、`macro`（两者）。

## 3. 三个必须知道的口径坑

1. **eco_cal 单次最多 100 行**，区间一宽就被**静默截断**（一次查 2024-01..2026-09 只回来 100 行）
   → `sync_macro_calendar` 按**自然月分块**，触到 100 行再自动拆半月。别删这段分块逻辑。
2. **value / fore_value / pre_value 是带单位后缀的字符串**：`1,660.0B`（十亿）、`3.438T`（万亿）、
   `-9.20M`（百万）、`7.5%`（百分点）、`52.2`（无单位）。落表时解析出 `value_num` / `fore_num` /
   `pre_num` 与预期差 `surprise`，并保留原始字符串与 `unit`（**跨事件比较前先看 unit**）。
3. **country / currency 字段不可靠**：传 `country='中国'` 仍会回来"澳大利亚出口月率""英国贸易帐"
   这类行，且它们的 country/currency 也被标成 `中国`/`CNY`（2853 行里 105 行、33 个事件）
   → 用事件标题的**「中国」前缀**收口过滤（`filter_cn_events`）。

补充：`pre_value` 语义 = **上月实际**（6→7、5→6 月传接自洽），但 2026-09-14 那行社融的
pre_value=660.0B 对不上 7 月实际的 1,410.0B（疑数据源脏行）→ 别把 pre_value 当"上月值"无脑用。

## 4. 调度

```
10 20 * * 1-5  run_sync.sh market          # 原有：行情/财务
30 21 * * *    run_sync.sh macro_daily     # 新增：日历 + shibor/两融/北向（不判交易日，宏观周末也发）
40 21 * * *    run_sync.sh macro_monthly   # 新增：5 个月度序列（全量拉取，幂等，每天跑不会漏发布）
```

月度序列不加日期参数：实测 `sf_month` 传 `start_month` 会被忽略、仍返回全量（295 行），
全量只有几百行，每天重拉 + 幂等 upsert 反而最稳。

## 5. 常用命令

```bash
cd /home/application/stock-analytics && source venv/bin/activate
python -m app.cli sync macro_monthly --history                 # 全量刷新月度序列
python -m app.cli sync macro_calendar --start 20180101 --end 20260916 --history
python -m app.cli sync macro_daily                             # 5 天增量（cron 同款）
./venv/bin/python tests/test_macro_parse.py                    # 解析/分块/过滤回归测试
```

### 预期差怎么查（SQL）

```sql
-- 最新一次社融/信贷的预期差（注意 unit：B=十亿）
select date, event, value, fore_value, surprise/1e9 as surprise_十亿
from macro_calendar
where event like '中国社会融资%' order by date desc limit 6;
```

⚠️ `shibor` 表的隔夜利率列名是 SQLite 保留字 `on`，必须加双引号：
`select date, "on" from shibor order by date desc limit 1;`
另：`moneyflow_hsgt.north_money` 单位是**万元**（/1e4 得亿元），`margin.rzye` 单位是**元**（/1e8 得亿元）。

## 6. 首次核对结论（0916 策略篇）

| 项目 | 研报/爆款文 | 本地实测（macro_calendar） | 判定 |
|---|---|---|---|
| 8 月社融实际 | 1.66 万亿 | 1,660.0B = 1.66 万亿 | ✅ |
| 8 月信贷实际 | 600 亿 | 60.0B = 600 亿 | ✅ |
| 社融市场预期 | 2.3 万亿 | **2,040.0B = 2.04 万亿** | ❌ 文中偏高 |
| 信贷市场预期 | 3,800 亿 | **480.0B = 4,800 亿** | ❌ 文中偏低 |
| "9,000 亿凭空蒸发" | 说成预期差 | **同比少增**：2025-08 社融 2.566 万亿 − 1.66 万亿 = **9,060 亿** | ⚠️ 口径混用 |

真实的预期差：信贷 **−4,200 亿**（最差）、社融 **−3,800 亿**。
反面证据（原文未提）：PPI **+3.8%** 同比转正（超预期 +0.2pct）、工业增加值 +5.2%（超预期）、
出口 +25.0%（符合预期）。

## 7. 未覆盖（缺口）

- **信贷分项**（居民 −2,029 亿、企业中长贷 3,200 亿）：tushare 无接口，需爬央行官网；
- **中债国债收益率曲线** `yc_cb`：token 无权限；
- `cn_pmi`：接口存在但字段大面积 NaN，PMI 改用 eco_cal（财新 PMI 有实际/预期）；
- `shibor_lpr`：有权限但**频次 1 次/小时**，未纳入定时。
