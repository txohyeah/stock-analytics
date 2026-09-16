"""宏观数据集同步（2026-09-16 新增，对应 A+B+C 方案的 A/C 部分）。

两类宏观数据：

1. **月度序列**（sf_month 社融 / cn_m 货币供应 / cn_cpi / cn_ppi / cn_gdp）
   历史总量只有几百行，走 registry 里现成的 ``basic`` 策略一次全量拉取，
   幂等 upsert，不需要日期参数（实测 ``sf_month`` 传 start_month 会被忽略）。

2. **宏观发布日历**（tushare ``eco_cal`` → 表 ``macro_calendar``）
   这是"预期差"的来源：每行是"某次宏观数据发布"，带
   ``value``（实际公布）/ ``fore_value``（市场预期）/ ``pre_value``（上月实际）。
   两个坑：
   - **单次最多返回 100 行**，区间一宽就被静默截断（一次性查 2024-01..2026-09
     只回来 100 行），所以按"月"分块查询；
   - value/fore_value 是**带单位后缀的字符串**（``1,660.0B`` / ``7.5%`` / ``3.438T`` /
     ``52.2``），无法直接比较，这里解析出 ``value_num`` / ``fore_num`` / ``pre_num``
     与预期差 ``surprise``，并保留原始字符串与 ``unit`` 便于回溯。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)

# eco_cal 数值后缀 → 换算倍数。'%' 保持原值（百分点），无后缀按原值。
_UNIT_FACTORS: dict[str, float] = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3, "%": 1.0}

# eco_cal 单次返回上限（实测 100 行，超过会被静默截断）
_ECO_CAL_ROW_CAP = 100

# eco_cal 的 country/currency 字段不可靠：传 country='中国' 仍会回来"澳大利亚出口月率"
# "英国贸易帐"这类行，且它们的 country/currency 也被标成 中国/CNY（2026-09-16 实测，
# 2853 行里有 105 行、33 个这样的事件）。中国宏观事件的标题统一以"中国"开头，
# 因此用标题前缀做收口过滤。
_CN_EVENT_PREFIX = "中国"

# eco_cal 里包含**未来已排期**的数据发布（值为空，如"中国央行贷款市场报价利率(LPR)(九月)"），
# 每次同步都顺带把未来这段时间的日程拉回来，供"发布日程"用（否则 5 天回看的增量窗口
# 永远看不到未来事件）。事件正式公布后，同一主键的行会被 upsert 补上实际值。
_FORWARD_DAYS = 45


def filter_cn_events(frame: pd.DataFrame) -> pd.DataFrame:
    """丢掉 eco_cal 里被错标成中国的境外事件行。"""
    if frame.empty or "event" not in frame.columns:
        return frame
    mask = frame["event"].astype(str).str.startswith(_CN_EVENT_PREFIX)
    dropped = int((~mask).sum())
    if dropped:
        logger.info("macro_calendar: 过滤掉 %d 行境外/错标事件（非'中国'前缀）", dropped)
    return frame[mask].reset_index(drop=True)


def parse_eco_value(value) -> float | None:
    """把 ``1,660.0B`` / ``7.5%`` / ``52.2`` 这类字符串解析成数值。

    返回当前事件自身单位下的数值（B=十亿、T=万亿、M=百万、%=百分点），
    仅供**同一事件**的 value/fore_value/pre_value 相互比较；跨事件比较需先看 unit。
    """
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "N/A"}:
        return None
    factor = 1.0
    suffix = text[-1].upper()
    if suffix in _UNIT_FACTORS:
        factor = _UNIT_FACTORS[suffix]
        text = text[:-1].strip()
    try:
        return float(text) * factor
    except ValueError:
        logger.debug("macro_calendar: 无法解析数值 %r", value)
        return None


def unit_of(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    suffix = text[-1].upper()
    return suffix if suffix in _UNIT_FACTORS else ""


def _month_chunks(start_date: str, end_date: str) -> list[tuple[str, str]]:
    """把 YYYYMMDD 区间切成按自然月对齐的小段（eco_cal 单次 100 行上限）。"""
    start = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")
    chunks: list[tuple[str, str]] = []
    cursor = start.replace(day=1)
    while cursor <= end:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        chunk_start = max(cursor, start)
        chunk_end = min(next_month - timedelta(days=1), end)
        if chunk_start <= chunk_end:
            chunks.append((chunk_start.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
        cursor = next_month
    return chunks


def enrich_eco_cal(frame: pd.DataFrame) -> pd.DataFrame:
    """给 eco_cal 原始行补上解析后的数值列与预期差。"""
    if frame.empty:
        return frame
    out = frame.copy()
    for raw_col, num_col in (("value", "value_num"), ("fore_value", "fore_num"), ("pre_value", "pre_num")):
        if raw_col in out.columns:
            out[num_col] = out[raw_col].map(parse_eco_value)
    if "value" in out.columns:
        out["unit"] = out["value"].map(unit_of)
    if "value_num" in out.columns and "fore_num" in out.columns:
        out["surprise"] = out["value_num"] - out["fore_num"]
    return out


def sync_macro_calendar(ctx, dataset, start_date: str, end_date: str, ts_code: str | None) -> tuple[int, int]:
    """按自然月分块拉取 eco_cal，解析数值后幂等 upsert 到 macro_calendar。

    实际拉取区间 = [start_date, max(end_date, 今天 + _FORWARD_DAYS)]：
    多出来的"前瞻窗口"用来收集**未来已排期的发布日程**（值为空），
    等数据正式公布后同主键的行会被补上实际值。
    """
    from app.sync.base import upsert  # 延迟导入避免与 base 循环依赖

    del ts_code
    forward_end = (datetime.now() + timedelta(days=_FORWARD_DAYS)).strftime("%Y%m%d")
    fetch_end = max(end_date, forward_end)
    if fetch_end != end_date:
        logger.info("macro_calendar: 前瞻窗口 %s → %s（未来 %d 天日程）", end_date, fetch_end, _FORWARD_DAYS)
    params = dict(dataset.default_params or {})
    fetched = 0
    affected = 0
    for chunk_start, chunk_end in _month_chunks(start_date, fetch_end):
        frame = ctx.client.query(dataset.api_name, start_date=chunk_start, end_date=chunk_end, **params)
        if frame is None or frame.empty:
            continue
        if len(frame) >= _ECO_CAL_ROW_CAP:
            # 触到上限说明这一块可能被截断，按半月再切一次
            logger.warning(
                "macro_calendar %s..%s 返回 %d 行（疑似触达单次 %d 行上限），改按半月重查",
                chunk_start, chunk_end, len(frame), _ECO_CAL_ROW_CAP,
            )
            mid = (datetime.strptime(chunk_start, "%Y%m%d") + timedelta(days=14)).strftime("%Y%m%d")
            sub_chunks = [(chunk_start, mid), ((datetime.strptime(mid, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d"), chunk_end)]
            frames = [
                ctx.client.query(dataset.api_name, start_date=a, end_date=b, **params)
                for a, b in sub_chunks
                if a <= b
            ]
            frames = [f for f in frames if f is not None and not f.empty]
            frame = pd.concat(frames, ignore_index=True) if frames else frame
        frame = filter_cn_events(frame)
        if frame.empty:
            continue
        frame = enrich_eco_cal(frame)
        fetched += len(frame)
        affected += upsert(ctx, dataset, frame)
        logger.info("macro_calendar %s..%s fetched=%s affected_total=%s", chunk_start, chunk_end, len(frame), affected)
    return fetched, affected
