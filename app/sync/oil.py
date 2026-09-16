"""外盘原油日线同步（新浪财经公开行情接口 → oil_global 表）。

背景（2026-09-16）：站点要做"框架条件变量体检"，其中最关键的一条是文章写的
**"布伦特原油连续 30 个交易日站稳 95 美元 → 转加息通道"**。实测 tushare 侧：

- `fut_daily` 只有**国内**原油（SC.INE，人民币元/桶，含关税+消费税，与布伦特有系统性价差）；
- `index_global` 只有全球股指（22 个代码：HSI/SPX/N225…），**没有任何原油**；
- `fut_basic(exchange='IPE'/'NYMEX')` 返回空 → 外盘期货根本不在权限内。

所以外盘油价改用**新浪财经全球期货日线接口**（公开、无需鉴权、含 2016 年至今全历史）：

    https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var _=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=OIL

  symbol=OIL → 布伦特原油连续；symbol=CL → WTI 原油连续；返回 JSONP，形如
  ``var _=([{"date":"2016-09-16","open":"46.310",...,"close":"46.050","volume":"14475"}, ...])``

⚠️ 两个坑：
1. 必须带 ``Referer: https://finance.sina.com.cn``，否则直接回 ``Forbidden``；
2. 比例/量级全是对齐后的字符串，成交量为 0 的行（早期数据）属正常，不要当异常抛错。

落表 ``oil_global(symbol, date, open, high, low, close, volume, source)``：
symbol 用 **BRENT** / **WTI**（下游只认这两个名字，别改成 OIL/CL）。

增量策略：单次 HTTP 就够拉全历史（布伦特约 2.6k 行），但没必要每天重灌 →
每次只 upsert 最近 ``MIN_LOOKBACK_DAYS`` 天（默认 400 天）与 ``start_date`` 中更早的那个；
``--history --start 20160101`` 才会灌全历史。
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pandas as pd
import requests

SINA_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/"
    "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={symbol}"
)
HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (compatible; stock-analytics/1.0)",
}
# 新浪代码 → 落库 symbol
SYMBOLS = {"OIL": "BRENT", "CL": "WTI"}
MIN_LOOKBACK_DAYS = 400
_JSON_RE = re.compile(r"var _=\((\[.*\])\)", re.S)


def fetch_series(symbol: str) -> pd.DataFrame:
    """拉单个外盘品种的全历史日线（原始字符串价格 → float）。"""
    resp = requests.get(SINA_URL.format(symbol=symbol), headers=HEADERS, timeout=30)
    resp.raise_for_status()
    match = _JSON_RE.search(resp.text)
    if not match:
        raise RuntimeError(f"新浪返回格式变了，解析不到 JSON 数组：{resp.text[:120]!r}")
    rows = json.loads(match.group(1))
    if not rows:
        raise RuntimeError(f"新浪返回空数据：symbol={symbol}")
    frame = pd.DataFrame(rows)
    for col in ("open", "high", "low", "close", "volume"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


def sync_sina_oil(ctx, dataset, start_date: str, end_date: str, ts_code: str | None) -> tuple[int, int]:
    """SyncFunction：新浪外盘原油日线 → oil_global（幂等 UPSERT）。"""
    from app.sync.base import upsert  # 延迟导入避免与 base 循环依赖

    del end_date, ts_code
    cutoff = (date.today() - timedelta(days=MIN_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    if start_date:
        started = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
        cutoff = min(cutoff, started)  # --start 更早时（--history）跟着灌更早的历史

    frames = []
    for sina_symbol, name in SYMBOLS.items():
        frame = fetch_series(sina_symbol)
        frame = frame[frame["date"] >= cutoff].copy()
        frame["symbol"] = name
        frame["source"] = "sina_global_futures"
        frames.append(frame[["symbol", "date", "open", "high", "low", "close", "volume", "source"]])
    combined = pd.concat(frames, ignore_index=True)
    affected = upsert(ctx, dataset, combined)
    return len(combined), affected


__all__ = ["sync_sina_oil", "fetch_series", "SYMBOLS"]
