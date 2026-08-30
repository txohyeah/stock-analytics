# 投资方法论库（docs/methodology）

> 从独立 skill 沉淀的投资分析体系，供 agent 与研报流程引用。
> 与 stock-analytics 数据/算法层配合：数据（tushare 同步 → sqlite）→ 算法
> （analytics 分析模块）→ 方法论（本文档，判定/归类/纪律的依据）。

## 文档索引

| 文档 | 内容 | 用途 |
|------|------|------|
| [01-investment-framework.md](01-investment-framework.md) | 产业投资框架：宏观利率周期 → 降息传导 → 行业三维定性 → 四层筛子 → 红绿灯跟踪 | 自上而下定赛道与核心持仓 |
| [02-stock-position-classification.md](02-stock-position-classification.md) | 个股定位分类：产业主脉（爆发/现金牛）/前瞻卡位/技术短线/分红企业/夕阳龙头 + 多产线逐线归类 | 回答"属于哪一类、按什么方式持有" |
| [03-industry-explosion.md](03-industry-explosion.md) | 产业爆发型主脉识别与跟踪：拐点核验/兑现跟踪表/证伪线/操作纪律 | 主脉内部"爆发形态"的识别与操作 |
| [references/four-layer-sieve.md](references/four-layer-sieve.md) | 四层筛子评分细则与否决条件（含 4b 参股价值重估、估值安全边际） | 个股评估淘汰制细则 |
| [references/macro-check.md](references/macro-check.md) | 宏观判断检查清单（美联储三类降息识别要点） | 利率周期类型判定的数据依据 |
| [references/tracking-template.md](references/tracking-template.md) | 红绿灯跟踪表模板与退出条件预设（各行业跟踪维度） | 持仓跟踪表设计 |
| [examples/xiechuang-300857-case.md](examples/xiechuang-300857-case.md) | 案例：协创数据 → 技术短线（外溢受益型） | "业绩好 ≠ 产业主脉"反例 |

## 体系关系

```
investment-framework（自上而下选赛道）
        │ 四层筛子（references/four-layer-sieve.md）
        ▼
stock-position-classification（输出归类层：怎么持有）
        │ 主脉内部再分形态
        ▼
industry-explosion（爆发型：拐点核验/兑现跟踪/证伪线）
        │
        ▼
红绿灯跟踪（references/tracking-template.md）→ 持续跟踪至逻辑破坏
```

## 与代码的衔接

- 财务/行情数据：`app/sync`（tushare 同步）+ `app/analytics`（分析模块）
- 排雷防守：`app/analytics/baolei.py`（五档红绿灯，见《财报排雷手册》逻辑）
- 龙虎榜信号：`app/analytics/lhb.py`（top_list 超买/超卖筛选）
