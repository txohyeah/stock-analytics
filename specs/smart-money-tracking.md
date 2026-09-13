# 三路聪明钱跟踪（社保 / 公募 / 国家队）— 设计文档

> 2026-09-12 定稿方向：**市场大方向分析**（不纠结个股）。社保/汇金 = tushare 全量存明细；公募 = 天天基金按基金维度爬主动权益基金前十大重仓，聚合行业配置。
> 状态：设计待确认，未动代码。

## 1. 目标

季度跟踪三路聪明钱（社保基金 / 公募基金 / 国家队·汇金）的资金动向，输出**市场级**结论：

1. 公募基金行业配置变化（QoQ）：哪些行业加仓/减仓、抱团集中度
2. 公募重仓股换血：新进/退出前十的股票
3. 社保/汇金行业分布 + 增减持方向
4. 汇金 ETF 动向（半年报，后续可选）

定位：**第二层证据**（验证资金面共识），不是买入信号。慢变量，季度频率，滞后 1-2 个月。

## 2. 数据源与频率

| 数据 | 来源 | 频率 | 成本（实测） |
|---|---|---|---|
| top10_holders（前十大股东） | tushare，按 period 全市场分页拉 | 季度增量 | 6.3s/季度，57646 行 |
| top10_floatholders（前十大流通股东） | tushare，同上 | 季度增量 | 同上 |
| fund_basic（基金列表） | tushare，market='O' 全量 | 季度（或首次后低频） | 15000 行 |
| fund_holdings（基金前十大重仓） | 天天基金 fundf10 逐只爬 | 季度 | 3222 只 × 1 请求 ≈ 30-60 分钟（限速） |

**关键实测结论**：
- `top10_holders` 支持按 period 全市场分页拉（offset/limit，6000 行/批），**不需要逐股拉**（三大报表才必须逐股）
- 主动权益基金筛选：混合型+股票型 10160 → 剔除指数（3340）/QDII（356）/C 类份额（5000）→ **3222 只**
- 天天基金 fundf10 jjcc 接口已验证可爬（返回前十大重仓股+占比+市值）
- **year 参数控制返回年份**（2026-09-13 实测）：请求 year=2026 返回 2026Q2/Q1，year=2025 返回 2025Q4/Q3/Q2/Q1。固定请求单年会导致无该年数据的基金（新基金/规模太小不披露）返回空——必须请求最新 2 个年份合并
- **fund_basic 必须分页**（2026-09-13 实测）：tushare 单次上限 5000 行，原单次调用只拉到前 15000 行，漏掉 110022 易方达消费行业股票等老基金；分页后 25029 行，主动权益筛选 6568 只
- 未爬基金（约 766 只）为 2025/2026 无披露数据的基金（FOF/养老/规模太小），返回空属正常

## 3. 表结构（schema.sql 新增 4 张表）

```sql
-- 前十大股东（社保/汇金/养老金/公募都在这里）
CREATE TABLE IF NOT EXISTS top10_holders (
    ts_code          TEXT NOT NULL,
    ann_date         TEXT,
    end_date         TEXT NOT NULL,      -- 报告期 20250630
    holder_name      TEXT NOT NULL,
    hold_amount      REAL,               -- 持股数量（股）
    hold_ratio       REAL,               -- 占总股本比例 %
    hold_float_ratio REAL,               -- 占流通股本比例 %
    hold_change      REAL,               -- 持股变动
    holder_type      TEXT,               -- 投资公司/国资局/基金等
    PRIMARY KEY (ts_code, end_date, ann_date, holder_name)
);
CREATE INDEX IF NOT EXISTS idx_top10_holders_end ON top10_holders(end_date);
CREATE INDEX IF NOT EXISTS idx_top10_holders_name ON top10_holders(holder_name);

-- 前十大流通股东（同结构）
CREATE TABLE IF NOT EXISTS top10_floatholders (
    ts_code          TEXT NOT NULL,
    ann_date         TEXT,
    end_date         TEXT NOT NULL,
    holder_name      TEXT NOT NULL,
    hold_amount      REAL,
    hold_ratio       REAL,
    hold_float_ratio REAL,
    hold_change      REAL,
    holder_type      TEXT,
    PRIMARY KEY (ts_code, end_date, ann_date, holder_name)
);
CREATE INDEX IF NOT EXISTS idx_top10_floatholders_end ON top10_floatholders(end_date);
CREATE INDEX IF NOT EXISTS idx_top10_floatholders_name ON top10_floatholders(holder_name);

-- 基金列表（tushare fund_basic，market='O'）
CREATE TABLE IF NOT EXISTS fund_basic (
    ts_code      TEXT PRIMARY KEY,       -- 基金代码
    name         TEXT,
    management   TEXT,                   -- 管理人
    custodian    TEXT,
    fund_type    TEXT,                   -- 混合型/股票型/债券型/货币型
    found_date   TEXT,
    list_date    TEXT,
    delist_date  TEXT,
    issue_amount REAL,
    m_fee        REAL,
    c_fee        REAL,
    duration_year REAL,
    p_value      REAL,
    min_amount   REAL,
    exp_return   REAL,
    benchmark    TEXT,
    status       TEXT,
    invest_type  TEXT,
    type         TEXT,
    market       TEXT
);

-- 基金前十大重仓股（天天基金爬虫）
CREATE TABLE IF NOT EXISTS fund_holdings (
    fund_code   TEXT NOT NULL,           -- 基金代码
    ts_code     TEXT NOT NULL,           -- 股票代码
    end_date    TEXT NOT NULL,           -- 报告期 20251231
    rank        INTEGER,                 -- 重仓排名 1-10
    hold_ratio  REAL,                    -- 占净值比例 %
    hold_vol    REAL,                    -- 持股数（万股）
    hold_amount REAL,                    -- 持仓市值（万元）
    PRIMARY KEY (fund_code, ts_code, end_date)
);
CREATE INDEX IF NOT EXISTS idx_fund_holdings_end ON fund_holdings(end_date);
CREATE INDEX IF NOT EXISTS idx_fund_holdings_stock ON fund_holdings(ts_code);
```

## 4. 同步模块

### 4.1 tushare 部分（社保/汇金）— 复用现有框架

**base.py 新增策略 `holders`**（按 period 全市场分页拉）：

```python
def sync_by_period_paged(ctx, dataset, start_date, end_date, ts_code):
    """top10_holders/top10_floatholders：period=end_date 全市场分页拉。
    实测 6000 行/批，全市场约 10 批，6 秒完成。"""
    del start_date, ts_code
    period = end_date or today_yyyymmdd()
    fetched = affected = 0
    offset = 0
    while True:
        frame = ctx.client.query(dataset.api_name, period=period, offset=offset, limit=6000)
        fetched += len(frame)
        affected += upsert(ctx, dataset, frame)
        if len(frame) < 6000:
            break
        offset += 6000
    return fetched, affected
```

**registry.py 注册**：

```python
"top10_holders": Dataset("top10_holders", "top10_holders", "top10_holders",
    ("ts_code", "end_date", "ann_date", "holder_name"), "holders"),
"top10_floatholders": Dataset("top10_floatholders", "top10_floatholders", "top10_floatholders",
    ("ts_code", "end_date", "ann_date", "holder_name"), "holders"),
"fund_basic": Dataset("fund_basic", "fund_basic", "fund_basic",
    ("ts_code",), "basic", {"market": "O", "status": "L"}),
```

**CLI 用法**：`sync top10_holders --end 20250630`（--end 即报告期）

### 4.2 天天基金部分（公募）— 新 provider + 新同步函数

**新文件 `app/providers/eastmoney_fund.py`**：

```python
def fetch_fund_holdings(fund_code: str, year: int, month: int) -> pd.DataFrame:
    """爬天天基金 fundf10 jjcc 接口，返回前十大重仓股 DataFrame。
    字段: ts_code, name, hold_ratio, hold_vol(万股), hold_amount(万元)"""
    # GET https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={fund_code}&topline=10&year={year}&month={month}
    # 解析 HTML 表格（已验证可用），股票代码转 ts_code 格式（600519→600519.SH）
```

**新同步函数 `sync_fund_holdings`**（base.py 或独立模块）：

```python
def sync_fund_holdings(ctx, dataset, start_date, end_date, ts_code):
    """从 fund_basic 读主动权益基金列表 → 逐只爬前十大重仓 → 幂等 upsert。
    筛选规则（实测 3222 只）：
      fund_type in (混合型, 股票型)
      剔除 name 含 指数/ETF/联接/QDII
      剔除 name 以 C/E 结尾（份额类别，同持仓只留 A）
    限速 1-2 req/s，断点续爬（按 fund_code 记录进度，失败重试）"""
```

**registry.py 注册**：`"fund_holdings": Dataset("fund_holdings", "", "fund_holdings", ("fund_code", "ts_code", "end_date"), "fund_holdings")`，STRATEGIES 加 `"fund_holdings": sync_fund_holdings`。

**报告期推导**：天天基金返回"2025年4季度"→ 转 `20251231`（季度末）。

## 5. 聚合分析（市场大方向报告）

**新文件 `app/analytics/smart_money.py`**，CLI 命令 `smart-money report --end 20250630`：

### 5.1 公募行业配置（核心输出）

```sql
-- 每只基金重仓股 → 行业映射（stock_basic.industry）→ 按持仓市值加权聚合
SELECT f.end_date, s.industry,
       SUM(f.hold_amount) AS amt,
       SUM(f.hold_amount) * 1.0 / SUM(SUM(f.hold_amount)) OVER (PARTITION BY f.end_date) AS ratio
FROM fund_holdings f
JOIN stock_basic s ON f.ts_code = s.ts_code
WHERE f.end_date = ?
GROUP BY f.end_date, s.industry
ORDER BY amt DESC
```

- 加权方式：持仓市值（hold_amount）加权
- **近似性标注**：前十大重仓约占基金仓位 50-70%，聚合结果是"前十大重仓的行业分布"，非全仓精确值——报告里必须标注
- 行业映射用 stock_basic.industry（东财行业，已有表零成本）

### 5.2 抱团集中度

- CR10：前十大重仓股市值 / 公募总持仓市值
- 行业集中度：前 3 大行业占比合计
- HHI：行业占比平方和（>0.25 视为高度集中）

### 5.3 公募重仓股换血

- 本季度 vs 上季度：新进前十 / 退出前十的股票清单

### 5.4 社保/汇金行业分布与增减持

```sql
-- 行业分布：holder_name LIKE '%社保%' OR '%汇金%' OR '%养老%'，按行业聚合
-- 增减持：相邻季度 hold_amount 对比（同 holder_name + ts_code）
```

- 社保：`holder_name LIKE '%社保%'`（社保基金101组合等 + 全国社会保障基金理事会）
- 汇金：`holder_name LIKE '%汇金%'`（中央汇金投资/资产管理）
- 养老金：`holder_name LIKE '%养老%'`（基本养老保险基金组合）

### 5.5 报告输出格式

季度《聪明钱市场大方向报告》：
1. 公募行业配置变化（QoQ）：加仓 TOP5 / 减仓 TOP5
2. 抱团集中度：CR10、行业集中度、HHI（含历史对比）
3. 公募重仓股换血：新进/退出
4. 社保/汇金行业分布 + 增减持方向
5. 结论段：资金面共识在哪、拥挤度是否上升

## 6. CLI 命令汇总

```
sync top10_holders --end 20250630        # 社保/汇金股东数据（季度）
sync top10_floatholders --end 20250630   # 同上（流通股东）
sync fund_basic                          # 基金列表（低频）
sync fund_holdings --end 20250630        # 公募重仓（季度，30-60 分钟）
smart-money report --end 20250630        # 市场大方向报告
```

## 7. 调度

- **不放进现有日频 crontab**（20:10 market / crypto 4h）
- 季度手动触发：财报披露后（1/4/7/10 月下旬）跑 `sync top10_holders` + `sync fund_holdings` + `smart-money report`
- 跑顺后考虑 qwenpaw cron 季度任务（披露日期不定，先手动）

## 8. 实施步骤

1. schema.sql 加 4 张表 + 索引
2. base.py 加 `sync_by_period_paged` 策略 + `sync_fund_holdings` 函数
3. registry.py 注册 4 个 Dataset
4. 新文件 `app/providers/eastmoney_fund.py`（爬虫）
5. 新文件 `app/analytics/smart_money.py`（聚合分析）
6. cli.py 加 `smart-money` 命令
7. 实测：拉 2025Q2 全量股东 + 爬 3222 只基金 + 出报告
8. git：master 分支，commit 报备，push 等确认

## 9. 已知限制

- 前十大重仓股聚合是近似行业配置（非全仓精确值）
- 天天基金爬虫依赖第三方接口，接口变动需维护
- 汇金 ETF 持仓（半年报披露）本期不做，后续可选
- 行业映射用东财行业（stock_basic.industry），非申万——粒度够用，如需申万后续加接口