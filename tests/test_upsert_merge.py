#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步写入路径回归测试：同键多行合并 + NULL 不覆盖已有值。

背景：tushare 对同一个主键会返回多行，其中只有部分行携带真实值（例如
fina_indicator 对同一 (ts_code, end_date, ann_date) 返回一行完整数据和一行
只有主键、profit_dedt 为空的行）。历史上第二次写入会无条件覆盖第一次，把好
数据抹成 NULL。本测试锁定修复后的语义，防止回归。

用法：cd <repo> && ./venv/bin/python tests/test_upsert_merge.py
只操作临时库，不触碰生产 data/stock.db。
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import upsert_dataframe  # noqa: E402
from app.storage import SqliteStore  # noqa: E402

KEY = ["ts_code", "end_date", "ann_date"]
COLS = ["ts_code", "end_date", "ann_date", "eps", "profit_dedt", "roe", "contract_liab"]

tmpdir = tempfile.mkdtemp(prefix="upsert-test-")
db = os.path.join(tmpdir, "t.db")
store = SqliteStore(db)

failures = []


def check(name, got, want):
    ok = got == want
    print("  %s %s: got=%r want=%r" % ("PASS" if ok else "FAIL", name, got, want))
    if not ok:
        failures.append(name)


def read(ts_code):
    row = store.query(
        'SELECT eps, profit_dedt, roe, contract_liab FROM "fina_indicator" '
        "WHERE ts_code=? AND end_date=? AND ann_date=?",
        (ts_code, "20260630", "20260828"),
    )
    return row[0] if row else None


print("=" * 72)
print("场景 1：tushare 原样返回 —— 同键两行，第二行 profit_dedt 为 NaN")
print("=" * 72)
df1 = pd.DataFrame(
    [
        {"ts_code": "000002.SZ", "end_date": "20260630", "ann_date": "20260828",
         "eps": -1.2532, "profit_dedt": -1.384044e10, "roe": -13.6487, "contract_liab": 5.0e10},
        {"ts_code": "000002.SZ", "end_date": "20260630", "ann_date": "20260828",
         "eps": -1.25, "profit_dedt": None, "roe": -13.6487, "contract_liab": None},
    ]
)
upsert_dataframe(store, "fina_indicator", df1, KEY, 5000)
check("扣非被保留(不是 NULL)", read("000002.SZ")[1], -1.384044e10)
check("合同负债被保留", read("000002.SZ")[3], 5.0e10)
check("同键合并为一行", store.query('SELECT count(*) FROM "fina_indicator"')[0][0], 1)

print()
print("=" * 72)
print("场景 2：后续同步又返回只有 NaN 的同一行 —— 不得抹掉已有值")
print("=" * 72)
df2 = pd.DataFrame(
    [{"ts_code": "000002.SZ", "end_date": "20260630", "ann_date": "20260828",
      "eps": -1.25, "profit_dedt": None, "roe": None, "contract_liab": None}]
)
upsert_dataframe(store, "fina_indicator", df2, KEY, 5000)
r = read("000002.SZ")
check("扣非仍在", r[1], -1.384044e10)
check("合同负债仍在", r[3], 5.0e10)
check("非空字段照常更新(eps -1.2532 -> -1.25)", r[0], -1.25)

print()
print("=" * 72)
print("场景 3：真实修正 —— 有值的字段要能更新（COALESCE 不能挡住更新）")
print("=" * 72)
df3 = pd.DataFrame(
    [{"ts_code": "000002.SZ", "end_date": "20260630", "ann_date": "20260828",
      "eps": -9.99, "profit_dedt": -2.0e10, "roe": None, "contract_liab": None}]
)
upsert_dataframe(store, "fina_indicator", df3, KEY, 5000)
r = read("000002.SZ")
check("扣非被真实修正", r[1], -2.0e10)
check("eps 被真实修正", r[0], -9.99)
check("roe 保留旧值未被 NULL 抹掉", r[2], -13.6487)

print()
print("=" * 72)
print("场景 4：无重复键时行为不变")
print("=" * 72)
df4 = pd.DataFrame(
    [
        {"ts_code": "600519.SH", "end_date": "20260630", "ann_date": "20260810",
         "eps": 30.0, "profit_dedt": 3.0e10, "roe": 20.0, "contract_liab": 1.0e10},
        {"ts_code": "000858.SZ", "end_date": "20260630", "ann_date": "20260812",
         "eps": 5.0, "profit_dedt": 2.0e10, "roe": 15.0, "contract_liab": 8.0e9},
    ]
)
upsert_dataframe(store, "fina_indicator", df4, KEY, 5000)
check("两行都入库", store.query('SELECT count(*) FROM "fina_indicator"')[0][0], 3)

print()
print("=" * 72)
print("场景 5：真实形态（中集集团）—— 逐列取组内首个非空")
print("=" * 72)
df5 = pd.DataFrame(
    [
        {"ts_code": "000039.SZ", "end_date": "20260630", "ann_date": "20260829",
         "eps": 0.13, "profit_dedt": 664779000.0, "roe": 1.4752, "contract_liab": None},
        {"ts_code": "000039.SZ", "end_date": "20260630", "ann_date": "20260829",
         "eps": 0.13, "profit_dedt": None, "roe": 1.4752, "contract_liab": None},
    ]
)
upsert_dataframe(store, "fina_indicator", df5, KEY, 5000)
row = store.query('SELECT profit_dedt, contract_liab FROM "fina_indicator" '
                  "WHERE ts_code='000039.SZ'")[0]
check("扣非保留", row[0], 664779000.0)

print()
print("=" * 72)
print("场景 6：落盘回读（确认不是内存缓存）")
print("=" * 72)
store.commit()
store.close()
raw = sqlite3.connect(db)
v = raw.execute('SELECT profit_dedt FROM "fina_indicator" WHERE ts_code="000002.SZ"').fetchone()[0]
check("磁盘上的扣非", v, -2.0e10)
raw.close()

print()
print("=" * 72)
if failures:
    print("结果：%d 项 FAIL -> %s" % (len(failures), failures))
    sys.exit(1)
print("结果：全部 PASS（6 组场景）")
