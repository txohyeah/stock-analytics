# stock-analytics

Tushare 股票数据同步 + A股分析一体化工具（原 tushare_stock_data，2026-08-30 更名）。
数据同步为纯脚本 + crontab 调度，不依赖大模型；分析命令直接读同步后的 sqlite。

## 架构

- **单层同步**：tushare API 直接写入最终表，**表名与结构完全跟随 tushare 接口**（daily / daily_basic / adj_factor / income / balancesheet / cashflow / fina_indicator / index_daily ...）。
- **幂等写入**：每张表带唯一键，`INSERT ... ON CONFLICT DO UPDATE`（sqlite）/ `ON DUPLICATE KEY UPDATE`（mysql），重跑不产生脏数据。字段若有新增会自动 `ALTER TABLE ADD COLUMN`。
- **双存储后端**（`DB_DRIVER` 切换）：
  - `sqlite`（默认）：单文件、零守护进程、零常驻内存，路径 `DB_SQLITE_PATH`（默认 `data/stock.db`）。
  - `mysql`：通过 `MYSQL_*` 配置连接已有库。
- **每次执行记入 `sync_run`**：dataset / mode / 窗口 / status / fetched_rows / affected_rows / 错误信息。
- **失败通知**：整组失败时通过飞书 OpenAPI 发文本消息（不经过大模型）；未配置飞书则仅记录日志。

## 安装

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env   # 填入 TUSHARE_TOKEN，按需选 DB_DRIVER
```

## 用法

```bash
# 拉一组数据（market=行情组，finance=财务组；market 自动跳过非交易日）
./venv/bin/python -m app.cli sync market
./venv/bin/python -m app.cli sync finance

# 拉单个数据集
./venv/bin/python -m app.cli sync daily --start 20260824 --end 20260828
./venv/bin/python -m app.cli sync income --ts-code 600519.SH --start 20250101 --end 20251231

# 其他
./venv/bin/python -m app.cli list
./venv/bin/python -m app.cli check-trade-day
```

## 定时与按需

- **行情（market）**：每天 20:10 定时跑，非交易日自动跳过。日 K 等为高频数据，定时最省心。
- **财务（finance）**：低频数据（季度披露），**不设定时**，由 LLM/分析按需调用 CLI 同步：
  - 拉一批股票的三大报表/财务指标（股票池场景）：
    ```bash
    ./venv/bin/python -m app.cli sync income --ts-codes "600519.SH,000001.SZ" --start 20260101 --end 20260630
    ./venv/bin/python -m app.cli sync fina_indicator --ts-codes "600519.SH,000001.SZ"
    ```
  - 历史全市场补数（慢，需挂机）：
    ```bash
    ./venv/bin/python -m app.cli sync finance --mode history   # 全市场逐股，约 2 小时/轮
    ```

crontab（已配置）：

```cron
10 20 * * 1-5  /home/application/stock-analytics/run_sync.sh market
```

`run_sync.sh` 会写日志到 `logs/`（保留最近 30 份）。market 组内部先查 trade_cal，
非交易日自动退出。

## A股分析（T2，app/analytics/）

数据同步与 A股分析一体：分析命令直接读 `data/stock.db`（sqlite，表名 tushare 原生），
计算引擎来自公共包 `tech-indicators`（需 editable 安装：`./venv/bin/pip install -e /home/application/tech-indicators`）。

```bash
# 查询（8 类）
./venv/bin/python -m app.cli query basic --code 600519.SH
./venv/bin/python -m app.cli query stock-name --name 贵州茅台
./venv/bin/python -m app.cli query history --code 600519.SH --lookback-days 120
./venv/bin/python -m app.cli query daily-basic --code 600519.SH --start 20260824 --end 20260828
./venv/bin/python -m app.cli query industries
./venv/bin/python -m app.cli query industry --name 白酒
./venv/bin/python -m app.cli query index --code 000001.SH --lookback-days 20
./venv/bin/python -m app.cli query fina --code 600519.SH   # 需 fina_indicator 已同步

# 策略筛选 / 报告
./venv/bin/python -m app.cli screen --strategy golden_bull_channel --codes 600519.SH,000858.SZ --output reports/screen.md
./venv/bin/python -m app.cli screen --strategy golden_bull_position_rating --universe a_share --min-circ-mv-e 200 --dry-run
./venv/bin/python -m app.cli screen --strategy golden_bull_position_rating --codes 600519.SH --eval-json reports/eval.json
./venv/bin/python -m app.cli list-strategies
./venv/bin/python -m app.cli inspect-strategy --strategy golden_bull_channel

# 复盘 / 持仓 / 排雷 / 图表
./venv/bin/python -m app.cli market-review --date latest
./venv/bin/python -m app.cli market-period-review --period week
./venv/bin/python -m app.cli account-review --positions positions.csv
./venv/bin/python -m app.cli baolei --self-test        # 或 --codes / --all --report out.md
./venv/bin/python -m app.cli chart --code 600519.SH --output chart.png

# 独立分析入口（等价，命令面相同）
./venv/bin/python -m app.analytics.cli query basic --code 600519.SH
```

**数据缺口**：`fina_indicator` / `income` / `balancesheet` / `cashflow` 需按 ts_code 逐股同步，
当前 sqlite 未全量入库。`query fina` 与 `baolei` 检测到未同步时返回
`DATA_INSUFFICIENT` 并给出同步命令：

```bash
./venv/bin/python -m app.cli sync fina_indicator --ts-codes 600519.SH,000001.SZ   # 按需
./venv/bin/python -m app.cli sync finance --mode history                          # 全市场（约 2h/轮）
```

**研报库联动（T3）**：`invest-research`（研报知识库）的 `pool-rate` 通过子进程调用本仓
`screen --strategy golden_bull_position_rating --eval-json <path>` 获取评分明细并落快照；
本仓目录路径可用环境变量 `POOL_RATING_ANALYTICS_DIR` 覆盖（研报库侧配置，默认本仓）。

## 数据集

| 数据集 | 说明 | 唯一键 |
|---|---|---|
| trade_cal | 交易日历 | exchange, cal_date |
| stock_basic | 股票列表 | ts_code |
| daily / daily_basic / adj_factor | 日行情/每日指标/复权因子 | ts_code, trade_date |
| index_daily / index_daily_basic | 指数 | ts_code, trade_date |
| index_basic | 指数列表 | ts_code |
| fina_indicator | 财务指标 | ts_code, end_date, ann_date |
| income / balancesheet / cashflow | 三大报表 | ts_code, end_date, ann_date, report_type |
| moneyflow_ths / kpl_concept_cons | 同花顺资金流/概念成分（需权限） | 视接口 |

## 目录

```
app/
  cli.py          命令行入口（单次命令，无常驻进程）
  config.py       配置（.env）
  storage.py      sqlite/mysql 双后端（动态建表 + 幂等 upsert）
  db.py           通用读写（sync_run、trade_cal、stock_basic）
  notifier.py     飞书文本通知
  sync/base.py    同步策略与 run 记录
  sync/registry.py 数据集注册表
  tushare_client.py tushare API 封装（重试/限速）
  providers/      fallback 爬虫（tushare 无权限时备用）
  analytics/      A股分析包（T2）：repository(sqlite) / screen / market_review
                  market_period_review / account / report / baolei / chart / cli
run_sync.sh       crontab 入口
```