#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""baolei.bulk_fetch 的归并语义回归测试。

缺陷背景：同一报告期在 tushare 里可能有多个 ann_date（业绩快报 / 年报 / 更正），
其中部分行只填主键、其余字段为空。原实现按 SQL 返回顺序**无条件赋值**，
等价于"最后一行赢"，而"最后一行"取决于 SQLite 的查询计划（走索引还是全表扫）
—— 取到空壳行就把真实数据读成 None。实测受害者：688311 盟升电子 2024 年
扣非 -2.69 亿被读成空、688015 交控科技 +4740 万被读成空等 12 个键。

修后语义：按 ann_date 升序遍历，**每列取最新公告里的非空值**；
空壳行不能覆盖已有值，且结果与 SQL 返回顺序无关。

用法：cd <repo> && ./venv/bin/python tests/test_baolei_merge.py
使用临时库，不触碰生产 data/stock.db。
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analytics.baolei import bulk_fetch  # noqa: E402

SCHEMA = {
    "income": """CREATE TABLE income (ts_code TEXT, end_date TEXT, ann_date TEXT,
        n_income REAL, n_income_attr_p REAL, revenue REAL, fv_value_chg_gain REAL,
        invest_income REAL, rd_exp REAL, n_oth_income REAL, assets_impair_loss REAL)""",
    "cashflow": """CREATE TABLE cashflow (ts_code TEXT, end_date TEXT, ann_date TEXT,
        n_cashflow_act REAL, c_fr_sale_sg REAL)""",
    "balancesheet": """CREATE TABLE balancesheet (ts_code TEXT, end_date TEXT, ann_date TEXT,
        goodwill REAL, total_hldr_eqy_exc_min_int REAL, inventories REAL, payroll_payable REAL,
        money_cap REAL, st_borr REAL, lt_borr REAL, bond_payable REAL)""",
    "fina_indicator": """CREATE TABLE fina_indicator (ts_code TEXT, end_date TEXT, ann_date TEXT,
        profit_dedt REAL, dt_netprofit_yoy REAL, netprofit_yoy REAL, netprofit_margin REAL,
        grossprofit_margin REAL, turn_days REAL, interestdebt REAL, debt_to_assets REAL)""",
    "stock_basic": "CREATE TABLE stock_basic (ts_code TEXT, name TEXT, industry TEXT)",
    "fina_audit": "CREATE TABLE fina_audit (ts_code TEXT, end_date TEXT, audit_result TEXT)",
}

tmp = tempfile.mkdtemp(prefix="baolei-merge-")
db = os.path.join(tmp, "t.db")
conn = sqlite3.connect(db)
for ddl in SCHEMA.values():
    conn.execute(ddl)

# 688311 盟升电子：真实还原 —— 空壳行（ann 早）**故意最后插入**，制造"最后一行赢"取到空
conn.execute("INSERT INTO stock_basic VALUES ('688311.SH','盟升电子','军工电子')")
conn.executemany(
    "INSERT INTO fina_indicator (ts_code,end_date,ann_date,profit_dedt,netprofit_margin) VALUES (?,?,?,?,?)",
    [
        ("688311.SH", "20241231", "20250425", -269190584.27, -47.0),  # 年报：有值，先插
        ("688311.SH", "20241231", "20250228", None, None),             # 快报空壳：后插
    ],
)
conn.executemany(
    "INSERT INTO income (ts_code,end_date,ann_date,revenue,n_income_attr_p) VALUES (?,?,?,?,?)",
    [
        ("688311.SH", "20241231", "20250425", 573000000.0, -269190584.27),
        ("688311.SH", "20241231", "20250228", None, None),
    ],
)
conn.executemany(
    "INSERT INTO cashflow (ts_code,end_date,ann_date,n_cashflow_act,c_fr_sale_sg) VALUES (?,?,?,?,?)",
    [
        ("688311.SH", "20241231", "20250425", -80000000.0, 610000000.0),
        ("688311.SH", "20241231", "20250228", None, None),
    ],
)
conn.executemany(
    "INSERT INTO balancesheet (ts_code,end_date,ann_date,goodwill,inventories) VALUES (?,?,?,?,?)",
    [
        ("688311.SH", "20241231", "20250425", 138222583.58, 757070482.05),
        ("688311.SH", "20241231", "20250228", None, None),
    ],
)
# 更正场景：两次公告都有值 → 必须取**较新**那次
conn.execute(
    "INSERT INTO income (ts_code,end_date,ann_date,revenue,n_income_attr_p) VALUES ('000068.SZ','20221231','20230420',100.0,10.0)"
)
conn.execute(
    "INSERT INTO income (ts_code,end_date,ann_date,revenue,n_income_attr_p) VALUES ('000068.SZ','20221231','20230510',200.0,20.0)"
)
conn.execute("INSERT INTO stock_basic VALUES ('000068.SZ','华控赛格','环保')")
# 空串场景：空串也必须视作"缺失"，不能盖掉真实值
conn.execute(
    "INSERT INTO fina_indicator (ts_code,end_date,ann_date,profit_dedt) VALUES ('601065.SH','20221231','20230415',9314374.17)"
)
conn.execute(
    "INSERT INTO fina_indicator (ts_code,end_date,ann_date,profit_dedt) VALUES ('601065.SH','20221231','20230301','')"
)
conn.execute("INSERT INTO stock_basic VALUES ('601065.SH','江盐集团','化工')")
conn.commit()
conn.close()

by_code, _basic, _audit, _trend = bulk_fetch(db)
failures = []


def check(name, got, want):
    ok = got == want
    print("  %s %s: got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))
    if not ok:
        failures.append(name)


def annual_of(code):
    rows = by_code.get(code) or []
    return rows[0] if rows else {}


print("=" * 74)
print("用例 1：空壳行最后插入（原实现必错）—— 扣非不得被读成 None")
print("=" * 74)
r = annual_of("688311.SH")
check("扣非净利润", r.get("profit_dedt"), -269190584.27)
check("营业收入", r.get("revenue"), 573000000.0)
check("归母净利润", r.get("n_income_attr_p"), -269190584.27)
check("经营现金流", r.get("n_cashflow_act"), -80000000.0)
check("销售收现", r.get("c_fr_sale_sg"), 610000000.0)
check("商誉", r.get("goodwill"), 138222583.58)
check("存货", r.get("inventories"), 757070482.05)
check("ann_date 仍取最新公告日", r.get("ann_date"), "20250425")

print()
print("=" * 74)
print("用例 2：两次公告都有值 —— 取较新那次（更正生效）")
print("=" * 74)
r2 = annual_of("000068.SZ")
check("营收取更正后 200.0", r2.get("revenue"), 200.0)
check("归母取更正后 20.0", r2.get("n_income_attr_p"), 20.0)
check("ann_date", r2.get("ann_date"), "20230510")

print()
print("=" * 74)
print("用例 3：空串也要视作缺失，不能盖掉真实值")
print("=" * 74)
r3 = annual_of("601065.SH")
check("扣非保留真实值", r3.get("profit_dedt"), 9314374.17)

print()
print("=" * 74)
print("用例 4：结果与插入顺序无关（把两组插入顺序对调，结论应一致）")
print("=" * 74)
db2 = os.path.join(tmp, "t2.db")
c2 = sqlite3.connect(db2)
for ddl in SCHEMA.values():
    c2.execute(ddl)
c2.execute("INSERT INTO stock_basic VALUES ('688311.SH','盟升电子','军工电子')")
c2.execute(
    "INSERT INTO income (ts_code,end_date,ann_date,revenue) VALUES ('688311.SH','20241231','20250425',573000000.0)"
)
# 顺序对调：空壳行先插、有值行后插
c2.executemany(
    "INSERT INTO fina_indicator (ts_code,end_date,ann_date,profit_dedt) VALUES (?,?,?,?)",
    [
        ("688311.SH", "20241231", "20250228", None),
        ("688311.SH", "20241231", "20250425", -269190584.27),
    ],
)
c2.commit()
c2.close()
by2, _b2, _a2, _t2 = bulk_fetch(db2)
r4 = (by2.get("688311.SH") or [{}])[0]
check("对调顺序后扣非一致", r4.get("profit_dedt"), -269190584.27)

print()
print("=" * 74)
print("用例 5：另一只受害股（001359 平安电工 2023 年报）同形态 —— 数值为合成")
print("=" * 74)
db3 = os.path.join(tmp, "t3.db")
c3 = sqlite3.connect(db3)
for ddl in SCHEMA.values():
    c3.execute(ddl)
c3.execute("INSERT INTO stock_basic VALUES ('001359.SZ','平安电工','电工材料')")
c3.execute(
    "INSERT INTO income (ts_code,end_date,ann_date,revenue,n_income_attr_p) "
    "VALUES ('001359.SZ','20231231','20240425',900000000.0,150000000.0)"
)
c3.executemany(
    "INSERT INTO fina_indicator (ts_code,end_date,ann_date,profit_dedt) VALUES (?,?,?,?)",
    [
        ("001359.SZ", "20231231", "20240425", 140000000.0),  # 年报：有值，先插
        ("001359.SZ", "20231231", "20240131", None),          # 快报空壳：后插
    ],
)
c3.commit()
c3.close()
by3, _b3, _a3, _t3 = bulk_fetch(db3)
r5 = (by3.get("001359.SZ") or [{}])[0]
check("2023 年报扣非", r5.get("profit_dedt"), 140000000.0)
check("ann_date 取最新", r5.get("ann_date"), "20240425")

print()
print("=" * 74)
print("用例 6：确认取数范围（baolei 四张表都只吃年报，中报/季报不进视野）")
print("=" * 74)
db4 = os.path.join(tmp, "t4.db")
c4 = sqlite3.connect(db4)
for ddl in SCHEMA.values():
    c4.execute(ddl)
c4.execute("INSERT INTO stock_basic VALUES ('002192.SZ','融捷股份','小金属')")
c4.execute(
    "INSERT INTO income (ts_code,end_date,ann_date,revenue) "
    "VALUES ('002192.SZ','20251231','20260420',2000000000.0)"
)
c4.execute(
    "INSERT INTO fina_indicator (ts_code,end_date,ann_date,profit_dedt) "
    "VALUES ('002192.SZ','20260630','20260818',1004440432.52)"
)
c4.commit()
c4.close()
by4, _b4, _a4, _t4 = bulk_fetch(db4)
rows4 = by4.get("002192.SZ") or []
check("中报期不进 annual（只保留年报）", [r.get("end_date") for r in rows4], ["20251231"])

print()
print("=" * 74)
if failures:
    print("结果：FAIL -> %s" % failures)
    sys.exit(1)
print("结果：全部 PASS（6 组用例）")
