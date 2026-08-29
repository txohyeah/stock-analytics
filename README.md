# tushare_stock_data

Tushare 股票数据定时同步工具。纯脚本 + crontab 调度，不依赖大模型。

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

## 定时（crontab）

日 K 每个交易日约 19:00 后才有，行情组定在 20:10；财务公告晚间更新，定在 21:30。

```cron
10 20 * * 1-5  /home/application/tushare_stock_data/run_sync.sh market
30 21 * * *    /home/application/tushare_stock_data/run_sync.sh finance
```

`run_sync.sh` 会写日志到 `logs/`（保留最近 30 份）。market 组内部先查 trade_cal，
非交易日自动退出。

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
run_sync.sh       crontab 入口
```