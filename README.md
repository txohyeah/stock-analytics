# Tushare Stock Data Sync

Python 3 同步器，用于从 Tushare Pro 拉取股票、指数、财务和复权数据，并幂等写入 MySQL。

## 功能

- 支持手动同步和应用内定时任务
- 支持日常增量和历史回补
- 使用 MySQL 唯一键 + `ON DUPLICATE KEY UPDATE` 防止重复数据
- 批量写入，避免逐行入库
- 每次同步写入 `sync_run`，便于追踪成功、失败、耗时和影响行数

## 初始化

安装依赖：

```bash
pip install -r requirements.txt
```

配置 `.env`：

```bash
cp .env.example .env
```

然后填写 `TUSHARE_TOKEN` 和 MySQL 连接信息。真实 `.env` 已被 `.gitignore` 忽略，不会提交。

初始化数据库表：

```bash
python -m app.cli init-db
```

## 手动同步

同步最近几天的日常数据：

```bash
python -m app.cli sync daily_group
```

同步单个数据集：

```bash
python -m app.cli sync daily --start 20260501 --end 20260505
python -m app.cli sync adj_factor --start 20260501 --end 20260505
```

历史回补：

```bash
python -m app.cli sync all --start 20100101 --end 20260505 --history
```

财务数据通常按股票循环同步。可以先同步股票列表：

```bash
python -m app.cli sync stock_basic
python -m app.cli sync finance_group --start 20250101 --end 20260505
```

同步单只股票：

```bash
python -m app.cli sync fina_indicator --ts-code 000001.SZ --start 20200101 --end 20260505
```

## 定时任务

启动内置定时器：

```bash
python -m app.cli scheduler
```

当前定时策略：

- 交易日 18:10 同步行情、每日指标、复权、指数和补充数据
- 每天 21:30 同步最近 30 天公告窗口内的财务数据

## 数据集

查看支持的数据集：

```bash
python -m app.cli list
```

## Fallback 数据源

默认开启 fallback。同步时会优先使用 Tushare；如果接口无权限、超时、连接异常，或 `daily` 在应同步日期返回空数据，会尝试备用来源。

当前已实现：

- `trade_cal`：使用 `exchange_calendars` 生成上交所交易日历；如果依赖不可用，则降级为工作日规则，并在日志中标记。
- `daily`：使用东方财富历史 K 线接口补充个股日 K，字段会转换为当前 `stock_daily` 表结构后批量 upsert。
- `index_daily`：使用东方财富历史 K 线接口补充常用指数或指定指数日 K，字段会转换为当前 `index_daily` 表结构后批量 upsert。

可通过命令行临时关闭：

```bash
python -m app.cli sync daily --start 20260505 --end 20260505 --no-fallback
```

也可在 `.env` 中调整：

```bash
ENABLE_FALLBACK=true
CRAWLER_SLEEP_MIN_SECONDS=1.5
CRAWLER_SLEEP_MAX_SECONDS=3.0
CRAWLER_MAX_RETRIES=3
CRAWLER_COOLDOWN_SECONDS=300
CRAWLER_TIMEOUT_SECONDS=20
# 可选：代理。CRAWLER_PROXY_URL 会同时用于 HTTP 和 HTTPS。
# CRAWLER_PROXY_URL=http://user:password@proxy_host:proxy_port
# CRAWLER_HTTP_PROXY=http://user:password@proxy_host:proxy_port
# CRAWLER_HTTPS_PROXY=http://user:password@proxy_host:proxy_port
```

备用抓取源内置随机 User-Agent、随机请求间隔、指数退避和冷却机制。若东方财富返回 403/429，或持续出现远端断开连接，会进入冷却期，避免继续高频打同一个出口。批量同步时仍按 DataFrame 批量写库，不逐行写入。

## 大模型按需补库

当 `stock-research` 返回结构化 `missing_data` 时，可以调用补库脚本。脚本只写入 MySQL，只输出 JSON 状态，不输出原始行情数据：

```bash
python -m scripts.prepare_analysis_data --ts-codes 600519.SH --datasets daily,adj_factor,index_daily --start 20230101 --end 20260509
```

按 profile 准备数据：

```bash
python -m scripts.prepare_analysis_data --ts-codes 600519.SH,000001.SZ --profile technical --years 3
```

大模型调用说明见 `docs/LLM_STOCK_ANALYSIS_SKILL.md`。
