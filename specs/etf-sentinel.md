# 任务书：国家队宽基 ETF 监控（数据层 + 信号 + 复盘集成）

仓库：/home/application/stock-analytics（master 分支）。**只改文件，不 commit、不 push、不动其他表、不跑任何写库的历史回补。**

## 背景（已验证事实，直接用，不要重新探路）

- 目的：识别"国家队"（汇金类资金）通过宽基 ETF 托底/压盘的行为，并入每日 20:15 大盘复盘。
- tushare `fund_daily`（场内基金日线）2000 积分实测可调；**amount 单位是千元**，vol 单位是手；单只一次区间拉 3 年（约 760 行）没问题。
- 11 只 ETF 白名单（ts_code → 简称）：
  - 核心家族 FAMILY_CORE（国家队评级对象）：510300.SH 华泰柏瑞300 / 510330.SH 华夏300 / 510310.SH 易方达300 / 159919.SZ 嘉实300 / 510050.SH 华夏上证50
  - 观察池 FAMILY_WATCH（只报放量、不评国家队）：510500.SH 南方500 / 159915.SZ 易方达创业板 / 588000.SH 华夏科创50 / 588080.SH 易方达科创50 / 159338.SZ 国泰A500 / 563360.SH 华泰柏瑞A500
- akshare 1.19.1 已在 venv；`ak.fund_etf_spot_em()` 无参调用，返回全市场场内基金快照，列含 `代码`/`最新价`/`IOPV实时估值`/`最新份额`/`流通市值`/`总市值`（「最新份额」单位=份；无历史参数，只做当日快照）。
- 真实验收锚点（量级核对 ±5%）：
  - 20261015：510310.SH 成交额 ≈194 亿（20 日均值的 ~25 倍），上证 -1.59% → 必须判"疑似托底"
  - 20261016：510300.SH ≈161 亿（≈120 日 P99），上证 +0.24% → 报"异常放量"
  - 20261022：510300.SH≈146 亿、510330.SH≈41 亿、159919.SZ≈106 亿，上证 -1.48% → 家族共振≥2 → "疑似托底"
  - 20261013：全部平静（四只 300ETF 合计 ~65 亿）→ "平静"
- 指数语境用本地 `index_daily` 表 000001.SH（已有数据）。
- 限流框架、幂等 upsert（`COALESCE(incoming, existing)`）、sqlite 串行写——全部沿用项目现有模式（app/sync/base.py + app/storage.py），不要发明新轮子。

## 交付物（4 件）

### 1. 数据集 fund_daily（app/sync/registry.py + app/sync/base.py）

- registry 加 `"fund_daily"` Dataset：api 名 fund_daily、表名 fund_daily、主键 `("ts_code","trade_date")`、新策略 `etf_daily`。
- base.py 新增策略 `sync_etf_daily`：对 11 只白名单**逐只**调 `pro.fund_daily(ts_code=..., start_date, end_date)`（每日增量 11 次请求；显式传了 --ts-code 时只拉指定的）。白名单常量放 registry（`ETF_SENTINEL_CODES` + `ETF_CORE_CODES`/`ETF_WATCH_CODES` 两个集合），供 base 和分析层 import。
- DAILY_ORDER 末尾加 `"fund_daily"`（20:10 market 组自动带上；注意 trade_cal 判交易日已由框架处理）。
- 表 schema 由现有框架按 Dataset 自动建，确认 amount/vol 等列齐全（tushare 原生列：open/high/low/close/pre_close/change/pct_chg/vol/amount）。

### 2. 份额快照（scripts/etf_share_snapshot.py + 新表 etf_share_daily）

- 新表（脚本内 `CREATE TABLE IF NOT EXISTS`，**不进 registry**）：
  `etf_share_daily(ts_code TEXT, trade_date TEXT, shares REAL, close REAL, captured_at TEXT, PRIMARY KEY(ts_code,trade_date))`
- 逻辑：`ak.fund_etf_spot_em()` 拉全市场 → 过滤 11 只白名单 → trade_date 用"今天"（仅交易日运行；调用方保证，脚本自己也用 app.sync.base.is_trade_day_today 守一道）→ 幂等 upsert（同键 COALESCE，captured_at 更新为最新）。
- 设计成可被 import 的函数 `snapshot_today(con, date=None) -> dict`（返回 {ts_code: shares}），`__main__` 入口打印一行 JSON 摘要（成功 N 只 / 失败清单）。akshare 失败时 exit 2、不写任何行。

### 3. 信号核心 + CLI（app/analytics/etf_sentinel.py + cli.py 子命令 etf-sentinel）

`python -m app.analytics.cli etf-sentinel [--date YYYYMMDD] [--output md路径]`（默认 latest 有数据交易日）。

计算与判定（阈值来自用户拍板，写成模块顶部常量）：

- 对 11 只各自计算：`ratio20` = 当日 amount ÷ 前 20 个交易日均值（**严格不含当日**，不足 20 日用现有全部）；`p90_120` = 前 120 日 amount 的 90 分位（不含当日，不足用现有全部）。`abnormal = ratio20 >= 3.0 或 当日 amount >= p90_120`。
- 大盘语境（000001.SH，2026-10-26 v2 修正：当日 + 近 3 日累计双条件，防的是 20261023
  "大跌次日修复反弹被误判压盘"——当日 +0.89% 但 3 日累计 -1.19%，实为托底延续）：
  - down（防守）：当日 pct_chg ≤ -0.5 **或** 近 3 日累计 ≤ -1.0
  - up（过热）：当日 pct_chg ≥ +0.5 **且** 近 3 日累计 ≥ +1.5（连涨过热，单日冲高不算）
  - 其余 flat（中性）
- 评级（只对 FAMILY_CORE）：核心家族 abnormal 计数 ≥2 = 共振，==1 = 单发。
  - down+共振 → **疑似托底（强）**；down+单发 → **疑似托底（中）**
  - up+共振 → **疑似压盘（弱）**；up+单发 → 冲高巨量（仅描述，不评国家队）
  - flat+任意 abnormal → 仅"异常放量"
  - 全无 → **平静**
- WATCH 池 abnormal 的一律只出一行提示，不参与国家队评级。
- 份额增量：etf_share_daily 若有前一日与当日，`share_chg=(今-昨)/昨`，绝对值 ≥1% 标注"份额异动 ±X%（一级申购/赎回嫌疑）"；基线不足时输出"份额基线积累中（第 N 天）"。
- 输出：markdown 段落（写入 --output，默认 /tmp/etf_sentinel_<date>.md）+ stdout 一行 JSON `{ok, date, rating, abnormal_codes, md_path}`。markdown 结构：标题行 `🛡️ 国家队 ETF 监控 <YYYYMMDD>｜<评级>` → 核心家族表格（代码/简称/成交额亿/20日倍数/P90 标记/份额变化）→ 观察池异常行 → 结论行（评级+依据：哪几只、几倍、上证 ±X%）→ 脚注一行固定口径："放量≠实锤：场内成交为双边口径，份额与官方公告为更确凿证据"。

### 4. 复盘集成（scripts/market_review_html.py）

- `main()` 里在生成 HTML 前调用 `snapshot_today()`（失败仅打 warning 继续，不阻塞复盘）→ 再 import etf_sentinel 的计算函数（抽出纯函数 `evaluate(con, date) -> dict`，别让渲染层走 subprocess）→ `render()` 里加一个"🛡️ 国家队 ETF 监控"卡片区块（复用现有卡片样式，表格红涨绿跌配色一致；平静时显示一行平静文案）。
- **注意：market_review_html.py 的 main 会真推飞书。** 你的测试禁止跑它的完整 main；用临时脚本调 `render()` / `evaluate()` 冒烟（结果写 /tmp，绝不调 feishu_push）。

## 硬约束

1. 只改上述 4 处 + specs/etf-sentinel.md（把本任务书整理成 spec 文档落档）。
2. 绝不打印/回显 .env 或 token；git 不 commit/push；不动库中既有表；sqlite 写串行。
3. tushare/akshare 外呼只做只读冒烟（如 fund_daily 拉单只 1 天验证连通），**历史回补与全量 sync 由人审核后执行**。
4. 代码风格对齐项目现状（中文注释、模块顶部常量、失败 fail-soft + 明确错误信息）。

## 自检清单（写完逐项跑，输出结果）

1. `python -c "from app.sync.registry import DATASETS, DAILY_ORDER; assert 'fund_daily' in DATASETS and 'fund_daily' in DAILY_ORDER"` 通过
2. etf_sentinel.evaluate 对 20261015/20261022/20261013 的判定与锚点一致（注意：历史回补未做之前 evaluate 会因缺数报 DATA_INSUFFICIENT 类明确错误，这也算通过——错误信息要能区分"表不存在/无数据"）
3. `etf_share_snapshot.py` 语法检查通过（python -m py_compile），不实际跑
4. render() 冒烟：构造假 evaluate 结果渲染出 HTML 含"国家队 ETF 监控"区块
5. cli `etf-sentinel --help` 可用

## 实现说明

- evaluate 仅查询本地库；缺表 reason=TABLE_MISSING，缺当日数据 reason=NO_DATA，缺历史基线 reason=BASELINE_MISSING，均使用 DATA_INSUFFICIENT。
- 默认日期取白名单 fund_daily 最新日期，11 只当日数据必须齐全；历史窗口严格排除当日。
- 份额比较使用最近一次有效历史快照；首次上线无基线时显示积累天数。snapshot_today 拒绝非今天日期，避免历史报告误标快照。
- 快照抓取/解析成功后才创建独立表并事务写入；部分缺失代码在 CLI JSON failed 中报告。
- 所有验证仅在内存库或只读连接执行；历史回补与全量同步由人审核后执行。
