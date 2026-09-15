#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 TushareClient 的限流处理：限流要等整个分钟窗口、且有独立重试预算。

完全用假的 _pro 模拟报错，不发起真实请求、不消耗 tushare 额度。
"""
import sys
from pathlib import Path
import time

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.tushare_client as tc  # noqa: E402
from app.tushare_client import TushareClient, is_rate_limit_error  # noqa: E402

RATE_MSG = (
    "抱歉，您每分钟最多访问该接口200次，权限的具体详情访问："
    "https://tushare.pro/document/1?doc_id=108"
)
PERM_MSG = "抱歉，您没有接口(moneyflow_ths)访问权限，权限的具体详情访问：https://tushare.pro/document/1?doc_id=108"

failures = []
sleeps = []


def check(name, cond, extra=""):
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, (" | " + extra) if extra else ""))
    if not cond:
        failures.append(name)


# 把真实 sleep 换成记录，测试瞬间完成
tc.time.sleep = lambda s: sleeps.append(round(s, 3))


def make_client(errors, backoff=2, retries=3, rl_wait=60, rl_retries=3):
    """errors: 每次调用依次抛出的异常；None 表示返回正常数据。"""
    client = object.__new__(TushareClient)
    client._retry_times = retries
    client._backoff_seconds = backoff
    client._interval_seconds = 0.5
    client._rate_limit_wait_seconds = rl_wait
    client._rate_limit_retry_times = rl_retries
    calls = {"n": 0}

    class FakePro:
        def query(self, api_name, **params):
            idx = calls["n"]
            calls["n"] += 1
            err = errors[idx] if idx < len(errors) else None
            if err is not None:
                raise RuntimeError(err)
            return pd.DataFrame([{"ts_code": "000002.SZ", "profit_dedt": -1.384044e10}])

    client._pro = FakePro()
    return client, calls


print("=" * 72)
print("用例 1：限流识别（只认限流语义，不能被无关数字误伤）")
print("=" * 72)
check("tushare 限流原文", is_rate_limit_error(RATE_MSG))
check("权限不足 ≠ 限流", not is_rate_limit_error(PERM_MSG))
check("无权限标记不被当限流", not is_rate_limit_error("没有接口(daily)访问权限"))
check("普通超时 ≠ 限流", not is_rate_limit_error("HTTPConnectionPool timeout"))
check("含 429 的普通报错字符串", is_rate_limit_error("HTTP 429 Too Many Requests"))
check("数字巧合不误判（如 14290）", not is_rate_limit_error("read timeout after 14290 ms"))

print()
print("=" * 72)
print("用例 2：限流一次后恢复 —— 必须等 60s（整分钟窗口），而不是 2s")
print("=" * 72)
sleeps.clear()
client, calls = make_client([RATE_MSG])
out = client.query("fina_indicator", ts_code="000002.SZ")
check("最终拿到数据", not out.empty and out.iloc[0]["profit_dedt"] == -1.384044e10)
check("共调用 2 次", calls["n"] == 2)
waits = [s for s in sleeps if s >= 30]
check("出现 ≥30s 的限流冷却", len(waits) == 1, "sleeps=%s" % sleeps)
check("冷却时长 = 配置的 60s", waits == [60])
check("没有走 2s 的短退避", 2 not in sleeps, "sleeps=%s" % sleeps)

print()
print("=" * 72)
print("用例 3：持续限流 —— 有独立预算，用满后明确报错（不无限重试）")
print("=" * 72)
sleeps.clear()
client, calls = make_client([RATE_MSG] * 10, rl_retries=3)
try:
    client.query("fina_indicator", ts_code="000002.SZ")
    check("应当抛错", False)
except RuntimeError as exc:
    check("抛出 RuntimeError", True)
    check("错误信息保留 tushare 原文", "每分钟最多访问" in str(exc))
check("限流重试 3 次后放弃（共 4 次调用）", calls["n"] == 4, "实际=%d" % calls["n"])
check("冷却 3 次", len([s for s in sleeps if s == 60]) == 3, "sleeps=%s" % sleeps)

print()
print("=" * 72)
print("用例 4：限流不该吃掉普通退避预算（两者互不干扰）")
print("=" * 72)
sleeps.clear()
# 先一次普通失败(2s) + 一次限流(60s) + 再普通失败(4s) + 成功
client, calls = make_client(["connection reset", RATE_MSG, "connection reset"])
out = client.query("fina_indicator", ts_code="000002.SZ")
check("最终成功", not out.empty)
check("共 4 次调用", calls["n"] == 4, "实际=%d" % calls["n"])
check("普通退避 2s、4s 都在", 2 in sleeps and 4 in sleeps, "sleeps=%s" % sleeps)
check("限流冷却 60s 也在", 60 in sleeps, "sleeps=%s" % sleeps)

print()
print("=" * 72)
print("用例 5：权限不足 —— 立即失败，不做任何重试（省额度）")
print("=" * 72)
sleeps.clear()
client, calls = make_client([PERM_MSG] * 5)
try:
    client.query("moneyflow_ths")
    check("应当抛错", False)
except RuntimeError:
    check("抛出 RuntimeError", True)
check("只调用 1 次", calls["n"] == 1, "实际=%d" % calls["n"])
check("无任何重试等待（仅请求前 0.5s 节流）",
      [s for s in sleeps if s >= 2] == [], "sleeps=%s" % sleeps)

print()
print("=" * 72)
print("用例 6：每次请求前的节流间隔仍然生效（防限流的第一道闸）")
print("=" * 72)
sleeps.clear()
client, calls = make_client([])
client.query("fina_indicator", ts_code="000002.SZ")
check("调用间隔 0.5s 生效", sleeps[:1] == [0.5], "sleeps=%s" % sleeps)

print()
print("=" * 72)
print("用例 7：真实配置下的速率换算（对照 tushare 2000 积分档 200 次/分钟）")
print("=" * 72)
from app.config import get_settings  # noqa: E402

s = get_settings()
per_min = 60.0 / s.sync_request_interval_seconds
print("  配置间隔 = %.2fs -> 上限 %.0f 次/分钟" % (s.sync_request_interval_seconds, per_min))
print("  tushare 2000 积分档 = 200 次/分钟/接口")
check("默认速率留有余量(<200)", per_min < 200, "%.0f/分钟" % per_min)
check("限流冷却参数已加载", s.sync_rate_limit_wait_seconds == 60 and s.sync_rate_limit_retry_times == 3)

print()
print("=" * 72)
if failures:
    print("结果：FAIL -> %s" % failures)
    sys.exit(1)
print("结果：全部 PASS（7 组用例）")
