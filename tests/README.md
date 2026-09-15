# tests/

纯脚本回归测试，无需 pytest（本仓库 venv 未安装）。每个文件都可直接运行，
全部 PASS 时退出码为 0，失败时退出码为 1，方便挂到 CI 或 pre-commit。

```sh
cd <repo>
./venv/bin/python tests/test_upsert_merge.py
./venv/bin/python tests/test_merge_equivalence.py
./venv/bin/python tests/test_rate_limit.py
./venv/bin/python tests/test_baolei_merge.py
```

| 文件 | 守卫的对象 |
|------|-----------|
| `test_upsert_merge.py` | `app.db.upsert_dataframe` / `app.storage` 的写入语义：同键多行合并 + **NULL 不得覆盖已有值**。tushare 会对同一主键返回"一行有值 + 一行只有主键"的两行，第二次写入若无条件覆盖就会把好数据抹成 NULL。 |
| `test_merge_equivalence.py` | `app.db.merge_duplicate_keys` 与参考实现（逐列取组内首个非空）逐格等价；含 500 轮随机比对、无重复键短路、宽表不触发碎片化告警。 |
| `test_rate_limit.py` | `app.tushare_client.TushareClient.query` 的失败处理：限流要等**整分钟窗口**而非短退避、限流重试有独立预算、权限/参数错误立即失败不浪费额度、请求前节流仍生效。 |
| `test_baolei_merge.py` | `app.analytics.baolei.bulk_fetch` 的归并语义：同一报告期多公告日时，**每列取最新公告的非空值**，空壳行不得覆盖真实值，且结果与 SQL 返回顺序无关（原实现无 ORDER BY = "最后一行赢"，实测把 688311 盟升电子 2024 年扣非 -2.69 亿读成空）。 |

## 约定

- 测试只使用临时目录 / mock 对象，**不触碰生产 `data/stock.db`，不发起真实 tushare 请求**。
- 新增同步或写入逻辑时，请同步补一个断言到对应文件。
- 与 tushare 行为相关的测试请写明依据（接口文档、真实返回形态），避免写成"实现快照"。
- `test_baolei_merge.py` 里除 688311 的真实数值外均为合成数据，勿当真实财报引用。
- 注意 baolei 的四张财务表**只取年报**（`substr(end_date,5,4)='1231'`），中报/季报不进视野。

