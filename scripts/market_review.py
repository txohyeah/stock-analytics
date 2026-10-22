#!/usr/bin/env python3
"""大盘复盘日报 —— 只报状态，不给建议（2026-10-08，B2；2026-10-13 改版：盘面基础段前置）。

数据源：`market_state_daily`（脚本 `market_state.py` 维护）+ 当日 `daily` + `index_daily` + `stock_basic` + `margin`。
输出两块：
  【盘面】① K线（上证/创业板开收与盘中路径、振幅及近10日排位）② 涨跌家数+涨停跌停（近似）③ 板块主导与领涨领跌 ④ 指数与两市成交额、两融余额
  【信号地形】⑤ 等权指数 vs MA60 ⑥ 20 日宽度 ⑦ 定性（当日 + 中期两段，2026-10-08 定稿；2026-10-13 起盘中振幅联动标注）

**纪律（弱耦合，勿破）**：
  - 只给状态判断，**不给买卖建议**、**不当过滤器**（不参与任何信号筛选）；
  - 天天标注口径与「结论来自 2018-2026 样本内」，避免被当成承诺；
  - 与信号层共用市场层这一份计算结果，禁止联合调参。

用法：
    ./venv/bin/python scripts/market_review.py                  # 增量更新后输出最新交易日
    ./venv/bin/python scripts/market_review.py --date 20260930   # 指定日期
    ./venv/bin/python scripts/market_review.py --no-update       # 不更新状态表
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.market_state import connect, update  # noqa: E402
from scripts.market_lines import lines_section  # noqa: E402

DB = ROOT / "data" / "stock.db"

# 各状态样本内的起爆信号表现（2018-2026，29,818 笔事件研究；来源
# research/pa-state-label/reports/state_at_events_v1.csv，勿改口径）
STATE_STATS = {
    "above_宽": (-0.01, -1.18, 2432),
    "above_窄": (1.67, -0.40, 3862),
    "below_宽": (5.66, 4.08, 1336),
    "below_窄": (5.55, 4.45, 22188),
}
STATE_ZH = {
    "above_窄": "指数在 MA60 上方、宽度窄 —— 指数偏强但赚钱面窄（普涨不足）",
    "above_宽": "指数在 MA60 上方、宽度宽 —— 指数与普涨同步，样本内这种格局反而最差",
    "below_窄": "指数在 MA60 下方、宽度窄 —— 指数弱、面也窄，样本内反而是起爆信号的主场",
    "below_宽": "指数在 MA60 下方、宽度宽 —— 指数弱但普涨修复中，样本内与 below_窄 相当",
}

# ① K线段：两大指数的开收与盘中路径（2026-10-13 增，补"收盘截面看不见盘中"的盲区）
KLINE_INDEXES = [("000001.SH", "上证"), ("399006.SZ", "创业板指")]
AMP_VIOLENT = 2.0  # 盘中振幅 ≥2% 视为剧烈震荡，⑦定性联动标注（2026-10-13 定）


def kline_section(con: sqlite3.Connection, date: str) -> tuple[list[str], str, dict]:
    """返回 (K线描述行, 一句话总结, {code: 振幅})。指数行情缺失时返回空段。"""
    df = pd.read_sql_query(
        "SELECT ts_code, trade_date, open, high, low, close, pre_close, pct_chg FROM index_daily "
        "WHERE ts_code IN ('000001.SH','399006.SZ') AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT 20", con, params=(date,))
    if df.empty or date not in set(df["trade_date"]):
        return [], "", {}
    lines, amps, info = [], {}, {}
    for code, name in KLINE_INDEXES:
        sub = df[df["ts_code"] == code].sort_values("trade_date", ascending=False).reset_index(drop=True)
        t = sub.iloc[0]
        if t["trade_date"] != date or pd.isna(t["open"]) or pd.isna(t["pre_close"]) or t["pre_close"] <= 0:
            continue
        amp10 = ((sub["high"] - sub["low"]) / sub["pre_close"] * 100).head(10).dropna().tolist()
        amp = (t["high"] - t["low"]) / t["pre_close"] * 100
        rank = 1 + sum(1 for a in amp10 if a > amp + 1e-9)
        rank_zh = {1: "（近10日最大）", 2: "（近10日次大）", 3: "（近10日第3大）"}.get(rank, "")
        o, c_, h, l, pc = t["open"], t["close"], t["high"], t["low"], t["pre_close"]
        gap = (o / pc - 1) * 100
        lowp, highp = (l / pc - 1) * 100, (h / pc - 1) * 100
        body = abs(c_ - o) / pc * 100
        lower, upper = (min(o, c_) - l) / pc * 100, (h - max(o, c_)) / pc * 100
        shape = ""
        if lower >= 0.8 and lower >= 1.5 * max(body, 0.2):
            shape = "长下影"
        elif upper >= 0.8 and upper >= 1.5 * max(body, 0.2):
            shape = "长上影"
        elif body < 0.3 and upper >= 0.3 and lower >= 0.3:
            shape = "十字星"
        gap_zh = "平开" if abs(gap) < 0.2 else f"{'高' if gap > 0 else '低'}开{abs(gap):.2f}%"
        if lowp <= -0.2 and highp >= 0.2:
            inner = f"盘中最低 {lowp:+.2f}%、最高 {highp:+.2f}%"
        elif lowp <= -0.2:
            inner = f"盘中最低 {lowp:+.2f}%"
        elif highp >= 0.2:
            inner = f"盘中最高 {highp:+.2f}%"
        else:
            inner = "全天窄幅"
        # 阴阳线按实体（收盘 vs 开盘）；较昨收反向时用"假阳/假阴"避免自相矛盾
        yang = c_ >= o
        if yang and t["pct_chg"] < 0:
            candle = "假阳线"
        elif not yang and t["pct_chg"] > 0:
            candle = "假阴线"
        else:
            candle = "阳线" if yang else "阴线"
        lines.append(f"　{name} {gap_zh}，{inner}，收{shape}{candle}"
                     f"（{t['pct_chg']:+.2f}%）｜振幅 {amp:.2f}%{rank_zh}")
        amps[code] = amp
        info[code] = {"gap": gap, "recov": (c_ - l) / pc * 100, "upper": upper, "amp": amp}
    if not info:
        return [], "", amps
    deepest_gap = min(v["gap"] for v in info.values())
    max_recov = max(v["recov"] for v in info.values())
    max_upper = max(v["upper"] for v in info.values())
    max_amp = max(v["amp"] for v in info.values())
    if deepest_gap <= -0.5 and max_recov >= 1.0:
        summary = "全日：低开深砸、尾盘拉回（V 型）"
    elif max_upper >= 1.0 and max_recov < 1.0:
        summary = "全日：冲高回落"
    elif max_amp < 1.0:
        summary = "全日：窄幅整理"
    else:
        summary = "全日：宽幅震荡"
    return lines, summary, amps


# ② 涨停/跌停家数（2026-10-13 增）：近似口径——按板块涨跌幅阈值 + 收盘触板判定
def limit_counts(con: sqlite3.Connection, date: str) -> dict | None:
    d = pd.read_sql_query(
        "SELECT d.ts_code, d.close, d.high, d.low, d.pct_chg, s.name FROM daily d "
        "LEFT JOIN stock_basic s ON d.ts_code = s.ts_code "
        "WHERE d.trade_date = ? AND d.pct_chg IS NOT NULL", con, params=(date,))
    if d.empty:
        return None

    def lim_n(row) -> float:
        c = row["ts_code"]
        if c.endswith(".BJ"):
            base = 30.0
        elif c.startswith(("300", "301", "302", "688", "689")):
            base = 20.0
        else:
            base = 10.0
        nm = row["name"] or ""
        if base == 10.0 and "ST" in nm.upper():
            base = 5.0  # 主板 ST ±5%；创业/科创 ST 仍为 ±20%
        return base

    d["lim"] = d.apply(lim_n, axis=1)
    zt = d[(d["pct_chg"] >= d["lim"] * 0.998) & (d["close"] >= d["high"] * 0.999)]
    dt = d[(d["pct_chg"] <= -d["lim"] * 0.998) & (d["close"] <= d["low"] * 0.999)]
    return {"zt": int(len(zt)), "dt": int(len(dt))}


def day_counts(con: sqlite3.Connection, date: str) -> dict:
    px = pd.read_sql_query("SELECT pct_chg FROM daily WHERE trade_date = ? AND pct_chg IS NOT NULL",
                           con, params=(date,))["pct_chg"]
    return {"up": int((px > 0).sum()), "down": int((px < 0).sum()), "flat": int((px == 0).sum()),
            "median": float(px.median()), "down5": int((px < -5).sum()), "n": int(len(px))}


# ④ 主要指数（2026-10-09 增）：白名单宽基 + 两市成交额（上证指数+深证综指口径）+ 两融余额（2026-10-13 增）
INDEX_WHITELIST = [
    ("000001.SH", "上证"), ("399001.SZ", "深成"), ("399006.SZ", "创业板指"), ("000688.SH", "科创50"),
    ("000300.SH", "沪深300"), ("000905.SH", "中证500"), ("000852.SH", "中证1000"),
]
TWO_MKT_AMT = ("000001.SH", "399106.SZ")  # 沪市总额 + 深市总额（深证综指）


def margin_tail(con: sqlite3.Connection, date: str) -> str:
    """两融余额（沪深合计，T-1 披露）；数据落后超过 7 个自然日则静默跳过。"""
    try:
        r = con.execute("SELECT trade_date, ROUND(SUM(rzye)/1e12, 2) FROM margin "
                        "WHERE exchange_id IN ('SSE','SZSE') GROUP BY trade_date "
                        "ORDER BY trade_date DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return ""
    if not r or r[1] is None:
        return ""
    gap_days = (pd.Timestamp(date) - pd.Timestamp(r[0])).days
    if gap_days > 7:
        return ""
    md = r[0]
    return f"两融余额 {r[1]:.2f} 万亿（截至 {md[:4]}-{md[4:6]}-{md[6:]}）"


def index_section(con: sqlite3.Connection, date: str) -> list[str]:
    codes = [c for c, _ in INDEX_WHITELIST] + list(TWO_MKT_AMT)
    qs = ",".join("?" * len(codes))
    df = pd.read_sql_query(
        f"SELECT ts_code, trade_date, close, pct_chg, amount FROM index_daily "
        f"WHERE ts_code IN ({qs}) AND trade_date <= ? ORDER BY trade_date DESC LIMIT {len(codes) * 3}",
        con, params=(*codes, date))
    if df.empty or date not in set(df["trade_date"]):
        return []  # 指数行情缺失时静默跳过该段，不影响其余复盘
    today = df[df["trade_date"] == date].set_index("ts_code")
    prev_dates = df[df["trade_date"] < date]["trade_date"]
    prev = df[df["trade_date"] == prev_dates.max()].set_index("ts_code") if not prev_dates.empty else None

    def part(code: str, name: str, with_close: bool) -> str:
        pct = f"{today.loc[code, 'pct_chg']:+.2f}%"
        return f"{name} {today.loc[code, 'close']:.1f}（{pct}）" if with_close else f"{name}（{pct}）"

    line1 = "④指数：" + "｜".join(part(c, n, True) for c, n in INDEX_WHITELIST[:4] if c in today.index)
    line2 = "　" + "｜".join(part(c, n, False) for c, n in INDEX_WHITELIST[4:] if c in today.index)
    amt = ""
    if all(c in today.index for c in TWO_MKT_AMT):
        amt_t = sum(today.loc[c, "amount"] for c in TWO_MKT_AMT) / 1e9  # 千元 → 万亿
        if prev is not None and all(c in prev.index for c in TWO_MKT_AMT):
            amt_p = sum(prev.loc[c, "amount"] for c in TWO_MKT_AMT) / 1e9
            amt = f"两市成交 {amt_t:.2f} 万亿（前一日 {amt_p:.2f} 万亿，{(amt_t / amt_p - 1) * 100:+.0f}%）"
        else:
            amt = f"两市成交 {amt_t:.2f} 万亿"
    if amt:
        line2 += f"｜{amt}"
    mrg = margin_tail(con, date)
    if mrg:
        line2 += f"｜{mrg}"
    return [line1, line2]


# ③ 行业板块（2026-10-09 增；2026-10-13 加主导句）：stock_basic 行业分类聚合个股涨跌幅，报中位数状态
SECTOR_MIN_N = 5  # 家数不足的行业不列，防小样本噪声


def sector_section(con: sqlite3.Connection, date: str, top_n: int = 5) -> list[str]:
    d = pd.read_sql_query("SELECT ts_code, pct_chg FROM daily WHERE trade_date = ? AND pct_chg IS NOT NULL",
                          con, params=(date,))
    b = pd.read_sql_query("SELECT ts_code, industry FROM stock_basic "
                          "WHERE industry IS NOT NULL AND industry != ''", con)
    if d.empty or b.empty:
        return []
    m = d.merge(b, on="ts_code", how="inner")
    g = m.groupby("industry").agg(n=("pct_chg", "size"), med=("pct_chg", "median"),
                                  up=("pct_chg", lambda s: int((s > 0).sum())))
    g = g[g["n"] >= SECTOR_MIN_N].sort_values("med", ascending=False)
    if g.empty:
        return []

    def fmt(rows: pd.DataFrame) -> str:
        return "｜".join(f"{i} {r['med']:+.1f}%（涨{int(r['up'])}/{int(r['n'])}）"
                         for i, r in rows.iterrows())

    head3 = "、".join(list(g.head(3).index))
    tail3 = "、".join(list(g.tail(3).index)[::-1])
    bottom = g.tail(top_n).iloc[::-1]  # 最差的排前面
    return [f"③板块：主要涨在 {head3}｜主要跌在 {tail3}",
            f"　领涨：{fmt(g.head(top_n))}",
            f"　领跌：{fmt(bottom)}"]


def fmt_review(con: sqlite3.Connection, date: str) -> str:
    rows = pd.read_sql_query(
        "SELECT * FROM market_state_daily WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 2",
        con, params=(date,))
    if rows.empty:
        return f"⚠️ market_state_daily 无 {date} 及以前的数据（先跑 scripts/market_state.py update）"
    cur = rows.iloc[0]
    prev = rows.iloc[1] if len(rows) > 1 else None
    c = day_counts(con, cur["trade_date"])
    d = f"{cur['trade_date'][:4]}-{cur['trade_date'][4:6]}-{cur['trade_date'][6:]}"
    wk = "一二三四五六日"[pd.Timestamp(d).weekday()]

    # —— 盘面（2026-10-13 前置）——
    out = [f"📊 大盘复盘 {d}（周{wk}）", "", "【盘面】"]
    kl_lines, kl_summary, amps = kline_section(con, cur["trade_date"])
    if kl_lines:
        out.append("①K线：" + kl_lines[0].removeprefix("　"))
        out += kl_lines[1:]
        if kl_summary:
            out.append("　" + kl_summary)
    lc = limit_counts(con, cur["trade_date"])
    zt_dt = f"｜涨停 {lc['zt']} / 跌停 {lc['dt']}（收盘封板近似）" if lc else ""
    out.append(f"②涨跌：涨 {c['up']} / 跌 {c['down']} / 平 {c['flat']}{zt_dt}；"
               f"中位涨幅 {c['median']:+.2f}%；跌超 5% {c['down5']} 家")
    out += sector_section(con, cur["trade_date"])
    out += index_section(con, cur["trade_date"])

    # —— 信号地形（研究向，2026-10-13 起编后置）——
    out.append("")
    out.append("【信号地形】")
    dir_zh = "上方" if cur["mkt_dir"] == "above" else "下方"
    gap = (cur["ew_idx"] / cur["ew_ma60"] - 1) * 100
    out.append(f"⑤方向：等权指数 {cur['ew_idx']:.4f}｜MA60 {cur['ew_ma60']:.4f} → 在 MA60 {dir_zh}"
               f"（{gap:+.2f}%）；MA60 十日斜率 {(cur['ew_slope60'] or 0)*100:+.2f}%")
    b_now = cur["breadth20"] * 100
    b_prev = f"（前一日 {prev['breadth20']*100:.1f}%）" if prev is not None else ""
    out.append(f"⑥宽度：20 日上涨家数占比 {b_now:.1f}%{b_prev} → {cur['mkt_breadth']}（阈 50%）")
    # ⑦定性（2026-10-08 定稿；2026-10-13 加盘中振幅联动）：当日与中期分开说
    # 当日分档：等权涨跌 ±0.5% 内=平稳；超出且上涨占比 <40%=普跌、>60%=普涨；其余=分化
    ew_ret = (cur["ret_mean"] or 0) * 100
    up_pct = c["up"] / c["n"] * 100 if c["n"] else 0.0
    if abs(ew_ret) <= 0.5:
        day_tag = "平稳"
    elif ew_ret < 0 and up_pct < 40:
        day_tag = "普跌"
    elif ew_ret > 0 and up_pct > 60:
        day_tag = "普涨"
    else:
        day_tag = "分化"
    amp_note = ""
    if amps:
        amp_max = max(amps.values())
        if amp_max >= AMP_VIOLENT:
            amp_detail = "、".join(f"{dict(KLINE_INDEXES)[cd]} {a:.2f}%" for cd, a in
                                   sorted(amps.items(), key=lambda kv: -kv[1]))
            amp_note = f"｜盘中剧烈震荡：{amp_detail}"
    slope_pct = (cur["ew_slope60"] or 0) * 100
    slope_zh = "走平" if abs(slope_pct) <= 0.05 else ("上行" if slope_pct > 0 else "下行")
    if cur["mkt_dir"] == "above":
        mid_seg = (f"仍在 MA60 上方但缓冲薄（{gap:+.2f}%）" if abs(gap) < 1
                   else f"仍在 MA60 上方（缓冲 {gap:+.2f}%）")
    else:
        mid_seg = f"位于 MA60 下方（{gap:+.2f}%）"
    out.append(f"⑦定性：当日{day_tag}（等权 {ew_ret:+.2f}%，上涨 {up_pct:.1f}%）{amp_note}"
               f"｜中期：{mid_seg}，MA60 {slope_zh}")
    st = STATE_STATS.get(cur["mkt_state"])
    if st:
        state_short = STATE_ZH.get(cur["mkt_state"], "状态样本不足").split(" —— ")[0]
        out.append(f"　（中期状态「{state_short}」样本内参照 2018-2026：该状态 {st[2]:,} 笔起爆，"
                   f"f20 均值 {st[0]:+.2f}%、中位 {st[1]:+.2f}%——历史统计，不是承诺）")

    # —— 画线台账（2026-10-22 增：用户手绘线每日监控 + MTR 结构锚 + MACD 状态）——
    out += lines_section(con, cur["trade_date"])

    out.append("")
    out.append("口径：等权指数 = 全市场个股当日涨跌幅（截尾 ±21%）横截面均值逐日累乘（2017 起）；"
               "方向 = 指数 vs 自身 MA60；宽度 = 20 日上涨家数占比 >50% 判宽；"
               "当日定性 = 等权涨跌 ±0.5% 内为平稳，超出且上涨占比 <40% 为普跌、>60% 为普涨，其余为分化，"
               "上证或创业板振幅 ≥2% 时加注盘中剧烈震荡；"
               "K线/振幅 = 指数 OHLC 相对昨收，振幅 = (最高-最低)/昨收，排位为近 10 日；"
               "涨停/跌停 = 近似口径（主板 10%、创业科创 20%、北交 30%、主板 ST 5%，收盘触板判定，新股上市初期有误差）；"
               "指数 = tushare 指数日线，两市成交额 = 上证指数+深证综指成交额合计，两融 = 沪深融资融券余额（T-1 披露）；"
               f"板块 = tushare 行业分类（≥{SECTOR_MIN_N} 家），数值 = 行业内个股当日涨跌幅中位数。")
    out.append("画线台账 = 用户手绘线（登记日 point-in-time 存档），失效 = 收盘穿越（压力站上/支撑跌破），"
               "盘中触碰不算，只提醒不催改；结构锚 = Brooks MTR swing（LH+LL→空头偏/HH+HL→多头偏/混合中性，"
               "左右1根确认、回看40根，复用 pa_engine 原实现）；MACD = 12/26/9 标准 EMA 递推，"
               "数值与通达信可能有零点几的初始化差异，定性一致。")
    out.append("纪律：只报状态，不作买卖建议、不当过滤器；结论出自 2018-2026 样本内。")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="大盘复盘日报")
    ap.add_argument("--date", help="指定交易日 YYYYMMDD，默认最新")
    ap.add_argument("--no-update", action="store_true", help="不先增量更新 market_state_daily")
    args = ap.parse_args()

    if not args.no_update:
        ucon = connect()
        n = update(ucon)
        ucon.close()
        print(f"[market_state 增量 +{n} 行]", file=sys.stderr)
    con = sqlite3.connect(DB)
    date = args.date or con.execute("SELECT MAX(trade_date) FROM market_state_daily").fetchone()[0]
    print(fmt_review(con, date))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
