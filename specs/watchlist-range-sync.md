# 关注池区间行情同步

## 变更历史

| 版本 | 日期 | 变更内容摘要 | 关联操作 |
| :--- | :--- | :--- | :--- |
| v1.0 | 2026-09-06 | 增加指定股票池的日线与复权因子区间同步规则 | 新建 |

## 当前版本：v1.0

## 背景与范围

超跌起爆使用全市场按交易日同步。趋势回踩起爆只需用户关注池的历史行情；现有批量参数未能高效适用于 `daily` 与 `adj_factor`。

## 数据模型

使用既有 `daily(ts_code, trade_date)` 与 `adj_factor(ts_code, trade_date)` 表及其唯一键，不新增表或字段。

## 业务规则

当 `sync daily` 或 `sync adj_factor` 收到非空 `--ts-codes` 时，逐代码以 `ts_code + start_date + end_date` 调用数据源，并按既有唯一键批量写入。未提供 `--ts-codes` 时，保留按交易日拉取全市场的行为。`--ts-code` 的既有单日查询语义不变。

## 接口定义

沿用既有命令：`python -m app.cli sync <daily|adj_factor> --ts-codes <逗号分隔代码> --start YYYYMMDD --end YYYYMMDD`。

## 验收标准

- 给定多个代码及日期区间时，只请求这些代码并写入对应日期范围。
- 未指定 `--ts-codes` 时，全市场按交易日同步逻辑不变。
- 写入后的日线与复权因子可按 `(ts_code, trade_date)` 完整关联。

## 非目标

不改变全市场同步窗口、扫描策略规则或历史关注池的前视偏差处理。
