#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""merge_duplicate_keys 的等价性测试。

确保向量化实现与"逐列取组内首个非空值"的参考语义完全一致，且不退化为
O(n) 的碎片化路径（groupby.first 在宽表上会触发 PerformanceWarning）。
历史上曾用 groupby(...).ffill() 实现，语义不等价（ffill 只向下填充，
填不到组内首行的空缺），本测试即为那次回归的守卫。

用法：cd <repo> && ./venv/bin/python tests/test_merge_equivalence.py
"""
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import merge_duplicate_keys  # noqa: E402

KEY = ["ts_code", "end_date", "ann_date"]


def reference(df, keys):
    """参考实现：groupby.first() 语义（仅用于比对，不用在生产路径）。"""
    ordered = df.assign(_populated=df.notna().sum(axis=1)).sort_values(
        "_populated", ascending=False, kind="stable"
    )
    return (
        ordered.groupby(keys, as_index=False, sort=False, dropna=False)
        .first()
        .drop(columns="_populated")[list(df.columns)]
    )


rng = np.random.default_rng(7)
failures = []


def check(name, cond, extra=""):
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, (" | " + extra) if extra else ""))
    if not cond:
        failures.append(name)


print("=" * 72)
print("用例 1：真实形状 —— 同键双行，各自缺不同字段，逐列互补")
print("=" * 72)
real = pd.DataFrame([
    {"ts_code": "000002.SZ", "end_date": "20260630", "ann_date": "20260828",
     "eps": -1.2532, "profit_dedt": -1.384044e10, "roe": -13.6487, "contract_liab": None},
    {"ts_code": "000002.SZ", "end_date": "20260630", "ann_date": "20260828",
     "eps": -1.25, "profit_dedt": None, "roe": -13.6487, "contract_liab": 5.0e10},
    {"ts_code": "000039.SZ", "end_date": "20260630", "ann_date": "20260829",
     "eps": 0.13, "profit_dedt": 664779000.0, "roe": 1.4752, "contract_liab": None},
    {"ts_code": "000039.SZ", "end_date": "20260630", "ann_date": "20260829",
     "eps": 0.13, "profit_dedt": None, "roe": None, "contract_liab": None},
])
got = merge_duplicate_keys(real, KEY)
print(got.to_string(index=False))
a = got[got.ts_code == "000002.SZ"].iloc[0]
b = got[got.ts_code == "000039.SZ"].iloc[0]
check("行数合并为 2", len(got) == 2)
check("万科扣非保留", a.profit_dedt == -1.384044e10)
check("万科合同负债由兄弟行补齐", a.contract_liab == 5.0e10)
check("中集扣非保留", b.profit_dedt == 664779000.0)
check("中集 roe 保留(非空行优先)", b.roe == 1.4752)

print()
print("=" * 72)
print("用例 2：随机数据上与参考实现逐格等价（500 轮）")
print("=" * 72)
mismatch = 0
rounds = 0
for _ in range(500):
    n = int(rng.integers(2, 40))
    keys_pool = [("K%d" % i, "2026%02d30" % (i % 4 + 1)) for i in range(4)]
    rows = []
    for _ in range(n):
        k = keys_pool[int(rng.integers(0, len(keys_pool)))]
        rows.append({
            "ts_code": k[0], "end_date": k[1],
            "ann_date": "2026%02d01" % int(rng.integers(1, 9)),
            "a": None if rng.random() < 0.5 else float(rng.integers(-100, 100)),
            "b": None if rng.random() < 0.5 else float(rng.integers(-100, 100)),
            "c": None if rng.random() < 0.5 else str(rng.integers(0, 5)),
        })
    df = pd.DataFrame(rows)
    if not df.duplicated(subset=KEY).any():
        continue
    rounds += 1
    new = merge_duplicate_keys(df, KEY).sort_values(KEY).reset_index(drop=True)
    old = reference(df, KEY).sort_values(KEY).reset_index(drop=True)
    if not new.equals(old):
        mismatch += 1
        if mismatch == 1:
            print("  首个不一致样本：")
            print(df.to_string(index=False))
            print("  new:")
            print(new.to_string(index=False))
            print("  old:")
            print(old.to_string(index=False))
check("500 轮随机比对无差异（有效轮次=%d）" % rounds, mismatch == 0, "不一致=%d" % mismatch)

print()
print("=" * 72)
print("用例 3：无重复键时不改动输入（零开销短路）")
print("=" * 72)
plain = pd.DataFrame([
    {"ts_code": "600519.SH", "end_date": "20260630", "ann_date": "20260810", "a": 1.0},
    {"ts_code": "000858.SZ", "end_date": "20260630", "ann_date": "20260812", "a": 2.0},
])
out = merge_duplicate_keys(plain, KEY)
check("原样返回、未复制未重排", out is plain)

print()
print("=" * 72)
print("用例 4：宽表不产生碎片化告警（防 groupby.first 性能退化）")
print("=" * 72)
wide_rows = []
for r in range(6):
    base = {"ts_code": "K%d" % (r % 3), "end_date": "20260630", "ann_date": "20260828"}
    base.update({("c%d" % i): (None if (i + r) % 3 == 0 else i) for i in range(260)})
    wide_rows.append(base)
wide = pd.DataFrame(wide_rows)
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    merge_duplicate_keys(wide, KEY)
frag = [w for w in caught if "fragmented" in str(w.message).lower()]
check("无碎片化告警", not frag, "捕获=%d 碎片化=%d" % (len(caught), len(frag)))

print()
print("=" * 72)
if failures:
    print("结果：FAIL -> %s" % failures)
    sys.exit(1)
print("结果：全部 PASS（4 组用例）")
