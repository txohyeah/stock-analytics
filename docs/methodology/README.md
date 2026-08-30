# 投资方法论库（docs/methodology）

> 从独立 skill 沉淀的投资分析体系——**一套完整闭环**：宏观 → 行业 → 个股 → 归类 → 跟踪。
> 数据层：stock-analytics（tushare 同步 → sqlite → `app.cli` 查询）；建页承载：stock-page-builder。
> 本文档为**唯一权威来源**，各专题已去重融合，概念冲突以本文档为准。

## 体系总览（30 秒看懂）

```
步骤一 宏观定位 ──→ references/macro-check.md
步骤二 降息传导链 ─┐
步骤三 行业三维定性 ┤── 00-framework.md（总框架，六个步骤的导航与精简版）
步骤四 四层筛子  ──┘ ──→ references/four-layer-sieve.md（淘汰制细则+4b+估值检查）
步骤五 定位归类 ──→ 01-position-classification.md（五类判定+多产线+P/G/T）
         └── 形态细节 → 02-industry-explosion.md（爆发型识别/纪律/证伪）
步骤六 跟踪退出 ──→ 03-tracking.md（红绿灯/兑现/催化/波段四表）
```

## 文档地图

| 文档 | 内容 | 什么时候翻 |
|------|------|-----------|
| [00-framework.md](00-framework.md) | 总框架：六步闭环导航 + 关键结论（传导链/行业三维/2×2 总图/形态分水岭） | 完整流程、方向性提问、查步骤衔接 |
| [01-position-classification.md](01-position-classification.md) | 五类定位：产业主脉（两形态）/前瞻卡位（P/G/T）/技术短线/分红企业/夕阳龙头 + 多产线逐线归类 + 常见误归类 10 条 | "属于哪一类、怎么持有" |
| [02-industry-explosion.md](02-industry-explosion.md) | 产业爆发形态唯一权威：识别五信号/操作纪律/兑现跟踪表/证伪与切换/福瑞案例 | "拐点到了吗、报表放量了吗" |
| [03-tracking.md](03-tracking.md) | 跟踪体系：红绿灯表（通用，含中际旭创示例）+ 分类专用三表（兑现/催化/波段） | "怎么跟踪/盯持仓/预警" |
| [references/macro-check.md](references/macro-check.md) | 宏观检查清单：三类降息识别要点 + 判定模板 | "宏观/降息/利率" |
| [references/four-layer-sieve.md](references/four-layer-sieve.md) | 四层筛子评分细则：通过特征/否决条件 + 4b 参股重估 + 估值安全边际 | "这只股票能买吗" |
| [examples/xiechuang-300857-case.md](examples/xiechuang-300857-case.md) | 案例：协创数据 → 技术短线（外溢受益型） | "业绩好 ≠ 产业主脉"反例 |

## 关键概念对照（防歧义速查）

| 概念 | 权威定义位置 | 易混点 |
|------|------------|--------|
| 四层筛子 | `references/four-layer-sieve.md` | 严格淘汰制；估值检查**不是**第 5 个筛子，只是买入前必做 |
| 产业主脉两形态 | `02-industry-explosion.md` | 爆发 vs 现金牛分水岭：渗透率+增速+估值三合一 |
| 爆发 vs 前瞻卡位 | `02-industry-explosion.md` §判定流程 | 唯一分水岭：最新一期报表**有没有放量** |
| 前瞻卡位双条件 | `01-position-classification.md` ② | 卡住的位置风没来 + 风要来的位置卡住，缺一不可 |
| 2×2 矩阵（含第三行） | `00-framework.md` §步骤五 | 产业已验证 × 公司卡住，两问独立回答 |
| 四类跟踪表 | `03-tracking.md` §一 | 按持仓类型选表，爆发型同时要红绿灯+兑现表 |
| 夕阳龙头 vs 分红企业 | `01-position-classification.md` ④⑤ | 产业**趋势性萎缩**时优先归夕阳龙头（波段），不看股息厚度 |

## 与代码/站点的衔接

- 数据：`stock-analytics` 同步（tushare → sqlite）与查询（`app.cli query / baolei / lhb`）
- 排雷防守：`app/analytics/baolei.py`（五雷区红绿灯，财报层）
- 龙虎榜信号：`app/analytics/lhb.py`（top_list 超买/超卖）
- 建页承载：`stock-page-builder`——`explosion` 模板（爆发型兑现跟踪页）、`frontier` 模板（前瞻卡位页）、主线+副线（多产线）