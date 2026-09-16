#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宏观数据集同步（app/sync/macro.py）的解析/分块/过滤回归测试。

背景：tushare eco_cal 是"预期差"的唯一来源，但三个坑都在数据本身——
  1. 单次最多返回 100 行（宽区间被静默截断）→ 必须按自然月分块；
  2. value/fore_value/pre_value 是带单位后缀的字符串（1,660.0B / 7.5% / 3.438T），
     直接比较会得出错误结论 → 需解析出 *_num 与 surprise；
  3. country/currency 字段不可靠，传 country='中国' 仍会回来"澳大利亚出口月率"
     "英国贸易帐"这类行 → 用事件标题的"中国"前缀收口。
本测试锁定这三条语义，防止以后改优雅了反而改坏（例如把 B 当 10 亿还是 1 亿）。

用法：cd <repo> && ./venv/bin/python tests/test_macro_parse.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.sync.macro import (  # noqa: E402
    _month_chunks,
    enrich_eco_cal,
    filter_cn_events,
    parse_eco_value,
    unit_of,
)

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got={got!r} want={want!r}")
        print(f"  ✗ {label}: got={got!r} want={want!r}")
    else:
        print(f"  ✓ {label}")


print("== parse_eco_value: 单位后缀换算 ==")
check("B=十亿(1,660.0B→1.66万亿)", parse_eco_value("1,660.0B"), 1660.0e9)
check("T=万亿(3.438T)", parse_eco_value("3.438T"), 3.438e12)
check("M=百万(-9.20M)", parse_eco_value("-9.20M"), -9.2e6)
check("%按百分点原值(7.5%)", parse_eco_value("7.5%"), 7.5)
check("无后缀原值(52.2)", parse_eco_value("52.2"), 52.2)
check("负数(-340.0B)", parse_eco_value("-340.0B"), -340.0e9)
check("空值→None", parse_eco_value(None), None)
check("NaN→None", parse_eco_value(float("nan")), None)
check("脏值→None", parse_eco_value("N/A"), None)
check("unit_of(B)", unit_of("1,660.0B"), "B")
check("unit_of(%)", unit_of("7.5%"), "%")
check("unit_of(无后缀)", unit_of("52.2"), "")

print("== _month_chunks: 按自然月切块（防 100 行截断）==")
check("跨月区间", _month_chunks("20240115", "20240403"),
      [("20240115", "20240131"), ("20240201", "20240229"), ("20240301", "20240331"), ("20240401", "20240403")])
check("单月内", _month_chunks("20260901", "20260916"), [("20260901", "20260916")])
check("整月", _month_chunks("20260901", "20260930"), [("20260901", "20260930")])
check("闰年2月", _month_chunks("20240201", "20240229"), [("20240201", "20240229")])
check("跨年", _month_chunks("20251220", "20260105"), [("20251220", "20251231"), ("20260101", "20260105")])

print("== filter_cn_events: 剔除错标成中国的境外事件 ==")
raw = pd.DataFrame([
    {"date": "20260914", "time": "16:00", "currency": "CNY", "country": "中国",
     "event": "中国社会融资规模(人民币十亿元)(八月)", "value": "1,660.0B", "pre_value": "660.0B", "fore_value": "2,040.0B"},
    {"date": "20230908", "time": "11:00", "currency": "CNY", "country": "中国",
     "event": "英国贸易帐(英镑)(八月)", "value": "488.00B", "pre_value": "575.70B", "fore_value": "805.00B"},
    {"date": "20230908", "time": "11:00", "currency": "CNY", "country": "中国",
     "event": "澳大利亚出口月率(%)(八月)", "value": "-3.20M", "pre_value": "-9.20M", "fore_value": None},
])
kept = filter_cn_events(raw)
check("保留 1 行中国事件", len(kept), 1)
check("保留的是社融", kept.iloc[0]["event"][:2], "中国")

print("== enrich_eco_cal: 预期差 ==")
enriched = enrich_eco_cal(kept)
check("value_num", enriched.iloc[0]["value_num"], 1660.0e9)
check("fore_num", enriched.iloc[0]["fore_num"], 2040.0e9)
check("surprise = 实际-预期", enriched.iloc[0]["surprise"], -380.0e9)
check("unit", enriched.iloc[0]["unit"], "B")
empty = enrich_eco_cal(pd.DataFrame())
check("空表不炸", len(empty), 0)

print()
if failures:
    print(f"FAILED {len(failures)}: " + "; ".join(failures))
    sys.exit(1)
print("ALL PASS")
