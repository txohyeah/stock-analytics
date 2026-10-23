#!/usr/bin/env python3
"""大盘复盘 HTML 日报（2026-10-15 用户拍板固化：正文推 HTML 文件，文字回复只留一行摘要）。

数据与 scripts/market_review.py 完全同源：直接复用其 section 函数（K线/涨跌/涨停/
板块/指数）与同一张 market_state_daily 行；⑤⑥⑦ 的算术表达式逐字照搬 fmt_review，
保证 HTML 数字与文字版一致——两套展示、一套口径，禁止在单独一边改算法。

流程：market_state 增量更新 → 取数 → 渲染 reports/market_review_YYYYMMDD.html
     → 飞书文件推送（凭证：环境变量 > 本仓库 .env（FEISHU_OPEN_ID）
       > ~/.qwenpaw/config.json channels.feishu（app_id/app_secret））

stdout 末行（cron 文字回复专用）：
  SUMMARY|<日期>|上证 <收盘>（<涨跌%>）|当日<定性>｜中期<状态>

用法：
  ./venv/bin/python scripts/market_review_html.py               # 最新交易日，渲染+推送
  ./venv/bin/python scripts/market_review_html.py --no-push     # 只渲染（预览/自检）
  ./venv/bin/python scripts/market_review_html.py --date 20261015 --no-push
"""

from __future__ import annotations

import argparse
import base64
import html as H
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.market_review import (  # noqa: E402
    AMP_VIOLENT, DB, KLINE_INDEXES, SECTOR_MIN_N, STATE_STATS, STATE_ZH,
    day_counts, index_section, kline_section, limit_counts, sector_section,
)
from scripts.market_state import connect, update  # noqa: E402
from scripts.market_lines import lines_section, render_lines_chart  # noqa: E402

OUT_DIR = ROOT / "reports"

CHART_JS_PATH = Path('/root/.qwenpaw/workspaces/default/projects/paper-sim/dist/assets/chart.js')


def _chart_js() -> str:
    """paper-sim 与飞书 HTML 共用的 canvas 渲染器（单一真源，勿复制代码）。"""
    return CHART_JS_PATH.read_text(encoding='utf-8')



# 与 fmt_review 尾部逐字一致的口径/纪律文本（改一处必须同步两处）
FOOTER_CALIBER = (
    "口径：等权指数 = 全市场个股当日涨跌幅（截尾 ±21%）横截面均值逐日累乘（2017 起）；"
    "方向 = 指数 vs 自身 MA60；宽度 = 20 日上涨家数占比 >50% 判宽；"
    "当日定性 = 等权涨跌 ±0.5% 内为平稳，超出且上涨占比 <40% 为普跌、>60% 为普涨，其余为分化，"
    "上证或创业板振幅 ≥2% 时加注盘中剧烈震荡；"
    "K线/振幅 = 指数 OHLC 相对昨收，振幅 = (最高-最低)/昨收，排位为近 10 日；"
    "涨停/跌停 = 近似口径（主板 10%、创业科创 20%、北交 30%、主板 ST 5%，收盘触板判定，新股上市初期有误差）；"
    "指数 = tushare 指数日线，两市成交额 = 上证指数+深证综指成交额合计，两融 = 沪深融资融券余额（T-1 披露）；"
    f"板块 = tushare 行业分类（≥{SECTOR_MIN_N} 家），数值 = 行业内个股当日涨跌幅中位数。")
# 画线台账口径（2026-10-22 增，与 fmt_review 同步）
FOOTER_LINES = ("画线台账 = 用户手绘线（登记日 point-in-time 存档），失效 = 收盘穿越（压力站上/支撑跌破），"
                "盘中触碰不算，只提醒不催改；结构锚 = Brooks MTR swing（LH+LL→空头偏/HH+HL→多头偏/混合中性，"
                "左右1根确认、回看40根，复用 pa_engine 原实现）；MACD = 12/26/9 标准 EMA 递推，"
                "数值与通达信可能有零点几的初始化差异，定性一致。")
FOOTER_DISCIPLINE = "纪律：只报状态，不作买卖建议、不当过滤器；结论出自 2018-2026 样本内。"

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--tx:#1a1f26;--mut:#5b6472;--acc:#2563eb;
      --up:#d92626;--dn:#16a34a;--line:#e5e8ec;--warn:#d97706}
@media(prefers-color-scheme:dark){:root{--bg:#12161c;--card:#1b2129;--tx:#e6e9ee;
      --mut:#98a1ad;--acc:#60a5fa;--up:#f87171;--dn:#4ade80;--line:#2a323d}}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);
     font:15px/1.75 -apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
     padding:28px 14px}
main{max-width:680px;margin:0 auto}
header h1{font-size:24px;font-weight:700;letter-spacing:.5px}
header .sub{color:var(--mut);font-size:13px;margin-top:2px}
section{margin-top:24px}
h2{font-size:15px;font-weight:600;color:var(--acc);margin-bottom:12px;letter-spacing:1px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
      padding:14px 18px;margin-bottom:12px}
.card .t{font-weight:600;font-size:14px;color:var(--mut);letter-spacing:.5px}
.card .b{margin-top:6px}
.chips{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 6px}
.chip{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:8px 14px;font-size:13px;min-width:110px}
.chip b{display:block;color:var(--mut);font-weight:500;font-size:11px;
        letter-spacing:.5px;margin-bottom:2px}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:6px}
th{color:var(--mut);font-weight:500;text-align:left;padding:4px 6px;
   border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:5px 6px;border-bottom:1px dashed var(--line);white-space:nowrap}
.up{color:var(--up);font-weight:600}
.dn{color:var(--dn);font-weight:600}
.mut{color:var(--mut)}
.note{margin-top:8px;padding-left:10px;border-left:3px solid var(--acc);
      color:var(--mut);font-size:13.5px}
.footer{margin-top:28px;padding-top:14px;border-top:1px solid var(--line);
        color:var(--mut);font-size:12px;line-height:1.9}
"""


def pct_cls(v: float) -> str:
    return "up" if v > 0 else ("dn" if v < 0 else "mut")


# ---------- 与 fmt_review 相同口径的数据拼装 ----------

def collect(con: sqlite3.Connection, date: str) -> dict:
    rows = pd.read_sql_query(
        "SELECT * FROM market_state_daily WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 2",
        con, params=(date,))
    if rows.empty:
        raise RuntimeError(f"market_state_daily 无 {date} 及以前的数据（先跑 scripts/market_state.py update）")
    cur = rows.iloc[0]
    prev = rows.iloc[1] if len(rows) > 1 else None
    td = cur["trade_date"]
    d = f"{td[:4]}-{td[4:6]}-{td[6:]}"
    c = day_counts(con, td)
    kl_lines, kl_summary, amps = kline_section(con, td)
    lc = limit_counts(con, td)
    sec_lines = sector_section(con, td)
    idx_lines = index_section(con, td)

    # ↓↓↓ 以下表达式与 fmt_review 逐字一致（改 fmt_review 必须同步这里）↓↓↓
    dir_zh = "上方" if cur["mkt_dir"] == "above" else "下方"
    gap = (cur["ew_idx"] / cur["ew_ma60"] - 1) * 100
    slope_pct = (cur["ew_slope60"] or 0) * 100
    slope_zh = "走平" if abs(slope_pct) <= 0.05 else ("上行" if slope_pct > 0 else "下行")
    b_now = cur["breadth20"] * 100
    b_prev_txt = f"（前一日 {prev['breadth20']*100:.1f}%）" if prev is not None else ""
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
    if cur["mkt_dir"] == "above":
        mid_seg = (f"仍在 MA60 上方但缓冲薄（{gap:+.2f}%）" if abs(gap) < 1
                   else f"仍在 MA60 上方（缓冲 {gap:+.2f}%）")
    else:
        mid_seg = f"位于 MA60 下方（{gap:+.2f}%）"
    st = STATE_STATS.get(cur["mkt_state"])
    state_short = ""
    if st:
        state_short = STATE_ZH.get(cur["mkt_state"], "状态样本不足").split(" —— ")[0]
    # ↑↑↑ 同步区结束 ↑↑↑

    sh = con.execute("SELECT close, pct_chg FROM index_daily WHERE ts_code='000001.SH' "
                     "AND trade_date = ?", (td,)).fetchone()
    # ⑧画线台账配图：优先「数据 + 前端 canvas」（与 paper-sim 站点同一渲染器），失败降级 PNG
    chart_payload = None
    lines_png = None
    try:
        from scripts.market_chart import build_payload
        chart_payload = build_payload(con, td)
    except Exception as e:
        print(f"[warn] 画线台账数据包失败：{type(e).__name__}: {e}", file=sys.stderr)
    if not chart_payload:
        try:
            png = render_lines_chart(con, td)
            lines_png = base64.b64encode(png).decode() if png else None
        except Exception as e:
            print(f"[warn] 画线台账配图失败：{type(e).__name__}: {e}", file=sys.stderr)
    return {
        "date": td, "d": d, "wk": "一二三四五六日"[pd.Timestamp(d).weekday()],
        "counts": c, "lc": lc, "kl_lines": kl_lines, "kl_summary": kl_summary,
        "sec_lines": sec_lines, "idx_lines": idx_lines,
        "lines_rows": lines_section(con, td),
        "lines_png": lines_png,
        "chart_payload": chart_payload,
        "ew_idx": cur["ew_idx"], "ew_ma60": cur["ew_ma60"], "dir_zh": dir_zh, "gap": gap,
        "slope_pct": slope_pct, "slope_zh": slope_zh, "b_now": b_now, "b_prev_txt": b_prev_txt,
        "ew_ret": ew_ret, "up_pct": up_pct, "day_tag": day_tag, "amp_note": amp_note,
        "mid_seg": mid_seg, "mkt_state": cur["mkt_state"], "mkt_breadth": cur["mkt_breadth"],
        "state": st, "state_short": state_short,
        "sh_close": sh[0] if sh else None, "sh_pct": sh[1] if sh else None,
    }


# ---------- 解析 section 函数产出的行（格式串固定，解析失败则降级为纯文本） ----------

SECTOR_RE = re.compile(r"^(.+?) ([+-]?\d+\.\d)%（涨(\d+)/(\d+)）$")
IDX_CLOSE_RE = re.compile(r"^(.+?) (-?[\d.]+)（([+-][\d.]+)%）$")
IDX_PCT_RE = re.compile(r"^(.+?)（([+-][\d.]+)%）$")


def parse_sector_line(line: str) -> list[tuple[str, str, int, int]]:
    out = []
    body = line.split("：", 1)[1] if "：" in line else line
    for seg in body.split("｜"):
        m = SECTOR_RE.match(seg.strip())
        if m:
            out.append((m.group(1), m.group(2), int(m.group(3)), int(m.group(4))))
    return out


def parse_index_lines(lines: list[str]) -> tuple[list[dict], list[str]]:
    """返回 (带收盘的指数 chips, 其余 freeform chips 文本)。"""
    close_chips: list[dict] = []
    free: list[str] = []
    for ln in lines:
        body = ln.strip()
        body = body.removeprefix("④指数：")
        for seg in body.split("｜"):
            seg = seg.strip().lstrip("　")
            if not seg:
                continue
            m = IDX_CLOSE_RE.match(seg)
            if m:
                close_chips.append({"name": m.group(1), "close": m.group(2), "pct": m.group(3)})
                continue
            m = IDX_PCT_RE.match(seg)
            if m:
                close_chips.append({"name": m.group(1), "close": None, "pct": m.group(2)})
                continue
            free.append(seg)  # 两市成交 / 两融余额 等
    return close_chips, free


# ---------- HTML 渲染 ----------

def render(v: dict) -> str:
    c, lc = v["counts"], v["lc"]
    zt_dt = f"｜涨停 {lc['zt']} / 跌停 {lc['dt']}（收盘封板近似）" if lc else ""
    up_rows = parse_sector_line(v["sec_lines"][1]) if len(v["sec_lines"]) > 1 else []
    dn_rows = parse_sector_line(v["sec_lines"][2]) if len(v["sec_lines"]) > 2 else []
    idx_chips, free_chips = parse_index_lines(v["idx_lines"])
    main4 = [x for x in idx_chips if x["close"] is not None][:4]
    rest4 = [x for x in idx_chips if x["close"] is None][:3]

    h1 = f"📊 大盘复盘 {v['d']}（周{v['wk']}）"
    parts = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>大盘复盘 · {H.escape(v['d'])}</title>",
        f"<style>{CSS}</style></head><body><main>",
        f"<header><h1>{H.escape(h1)}</h1>",
        f"<div class='sub'>{H.escape(v['kl_summary'] or '')}</div></header>",
    ]
    chips1 = "".join(
        f"<div class='chip'><b>{H.escape(x['name'])}</b>"
        f"<span class='{pct_cls(float(x['pct']))}'>{H.escape(x['close'])}（{H.escape(x['pct'])}%）</span></div>"
        for x in main4)
    parts.append(f"<div class='chips'>{chips1}</div>")
    chips2 = "".join(
        f"<div class='chip'><b>{H.escape(x['name'])}</b>"
        f"<span class='{pct_cls(float(x['pct']))}'>{H.escape(x['pct'])}%</span></div>"
        for x in rest4)
    chips2 += "".join(f"<div class='chip'>{H.escape(x)}</div>" for x in free_chips)
    parts.append(f"<div class='chips'>{chips2}</div>")

    parts.append("<section><h2>盘面</h2>")
    if v["kl_lines"]:
        kl_body = "<br>".join(
            H.escape(ln.strip().lstrip("　").removeprefix("④").removeprefix("①K线："))
            for ln in v["kl_lines"])
        if v["kl_summary"]:
            kl_body += f"<br>全日：<b>{H.escape(v['kl_summary'])}</b>"
        parts.append(f"<div class='card'><div class='t'>① K线形态</div><div class='b'>{kl_body}</div></div>")
    med_cls = pct_cls(c["median"])
    parts.append(
        f"<div class='card'><div class='t'>② 涨跌分布</div><div class='b'>"
        f"涨 {c['up']} / 跌 {c['down']} / 平 {c['flat']}{H.escape(zt_dt)}<br>"
        f"中位涨幅 <span class='{med_cls}'>{c['median']:+.2f}%</span>｜跌超 5% 共 {c['down5']} 家</div></div>")
    if v["sec_lines"]:
        head = v["sec_lines"][0].split("：", 1)[1] if "：" in v["sec_lines"][0] else v["sec_lines"][0]
        parts.append(f"<div class='card'><div class='t'>③ 板块</div>"
                     f"<div class='b mut'>{H.escape(head)}</div>")
        if up_rows or dn_rows:
            rows_html = ""
            for i in range(max(len(up_rows), len(dn_rows))):
                u = up_rows[i] if i < len(up_rows) else None
                w = dn_rows[i] if i < len(dn_rows) else None
                cells = []
                for item, is_gain in ((u, True), (w, False)):
                    if item:
                        name, med, nup, n = item
                        cls = "up" if float(med) > 0 else ("dn" if float(med) < 0 else "mut")
                        cells += [f"<td>{H.escape(name)}</td>",
                                  f"<td class='{cls}'>{med}%</td>" if is_gain
                                  else f"<td class='{cls}'>{med}%</td>",
                                  f"<td class='mut'>涨{nup}/{n}</td>"]
                    else:
                        cells += ["<td></td>", "<td></td>", "<td></td>"]
                rows_html += "<tr>" + "".join(cells) + "</tr>"
            parts.append(
                "<table><tr><th>领涨板块</th><th>涨跌</th><th>广度</th>"
                "<th>领跌板块</th><th>涨跌</th><th>广度</th></tr>" + rows_html + "</table>")
        parts.append("</div>")
    parts.append("</section>")

    parts.append("<section><h2>信号地形</h2>")
    parts.append(
        f"<div class='card'><div class='t'>⑤ 方向</div><div class='b'>"
        f"等权指数 {v['ew_idx']:.4f}｜MA60 {v['ew_ma60']:.4f} → "
        f"<b>在 MA60 {H.escape(v['dir_zh'])}（{v['gap']:+.2f}%）</b>；"
        f"MA60 十日斜率 {v['slope_pct']:+.2f}%</div></div>")
    parts.append(
        f"<div class='card'><div class='t'>⑥ 宽度</div><div class='b'>"
        f"20 日上涨家数占比 {v['b_now']:.1f}%{H.escape(v['b_prev_txt'])} → "
        f"<b>{H.escape(str(v['mkt_breadth'] or ''))}</b>（阈 50%）</div></div>")
    state_note = ""
    if v["state"]:
        state_note = (f"<div class='note'>中期状态「{H.escape(v['state_short'])}」样本内参照 2018-2026："
                      f"该状态 {v['state'][2]:,} 笔起爆，f20 均值 {v['state'][0]:+.2f}%、"
                      f"中位 {v['state'][1]:+.2f}%——历史统计，不是承诺</div>")
    parts.append(
        f"<div class='card'><div class='t'>⑦ 定性</div><div class='b'>"
        f"当日<b>{H.escape(v['day_tag'])}</b>（等权 {v['ew_ret']:+.2f}%，上涨 {v['up_pct']:.1f}%）"
        f"{H.escape(v['amp_note'])}<br>中期：<b>{H.escape(v['mid_seg'])}</b>，"
        f"MA60 {H.escape(v['slope_zh'])}</div>{state_note}</div>")
    parts.append("</section>")

    # —— ⑧画线台账 + ⑨结构参考（2026-10-22 增，market_lines）——
    cur_title, blocks = None, []
    for r in v["lines_rows"]:
        if r.startswith("⑧") or r.startswith("⑨"):
            if cur_title:
                blocks.append((cur_title, cur_body))
            cur_title, cur_body = r, []
        else:
            cur_body.append(r)
    if cur_title:
        blocks.append((cur_title, cur_body))
    if blocks:
        parts.append("<section><h2>画线台账</h2>")
        for title, body in blocks:
            body_html = ""
            for row in body:
                txt = H.escape(row.strip())
                if row.strip().startswith("⚠️"):
                    body_html += f"<div class='b'><b style='color:var(--up)'>{txt}</b></div>"
                elif row.strip().startswith("✅"):
                    body_html += f"<div class='b'><b style='color:var(--dn)'>{txt}</b></div>"
                else:
                    body_html += f"<div class='b'>{txt}</div>"
            parts.append(f"<div class='card'><div class='t'>{H.escape(title)}</div>{body_html}</div>")
        if v.get("chart_payload"):
            parts.append(
                "<div class='card' style='padding:6px'>"
                "<canvas id='mktchart' style='width:100%;display:block;height:520px'></canvas></div>")
        elif v.get("lines_png"):
            parts.append(
                f"<div class='card' style='padding:6px'>"
                f"<img style='width:100%;display:block;border-radius:6px' "
                f"src='data:image/png;base64,{v['lines_png']}' alt='画线台账K线图'></div>")
        parts.append("</section>")

    if v.get("chart_payload"):
        parts.append("<script>" + _chart_js() + "</script>")
        pj = json.dumps(v["chart_payload"], ensure_ascii=False).replace("</", "<\\/")
        parts.append(
            "<script>var PAYLOAD=" + pj + ";(function(){var cv=document.getElementById('mktchart');"
            "if(!cv||!window.MarketChart)return;var rend=function(){var m=window.matchMedia;"
            "var dk=!!(m&&m('(prefers-color-scheme: dark)').matches);"
            "MarketChart.render(cv,PAYLOAD,{height:520,dark:dk});};rend();"
            "window.addEventListener('resize',rend);})();</script>")

    parts.append(
        f"<div class='footer'>{H.escape(FOOTER_CALIBER)}<br>{H.escape(FOOTER_LINES)}<br>"
        f"{H.escape(FOOTER_DISCIPLINE)}</div>"
        "</main></body></html>")
    return "\n".join(parts)


# ---------- 飞书推送（机制同 crypto-research/btc/scripts/feishu_send_file.py） ----------

def _load_env_file(path: Path) -> dict:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def feishu_conf() -> tuple[str | None, str | None, str | None]:
    env = _load_env_file(ROOT / ".env")
    app_id = os.environ.get("FEISHU_APP_ID") or env.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET") or env.get("FEISHU_APP_SECRET")
    open_id = os.environ.get("FEISHU_OPEN_ID") or env.get("FEISHU_OPEN_ID")
    if not (app_id and app_secret):
        cfg_path = Path.home() / ".qwenpaw" / "config.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        f = cfg.get("channels", {}).get("feishu", {})
        app_id = app_id or f.get("app_id")
        app_secret = app_secret or f.get("app_secret")
    return app_id, app_secret, open_id


def feishu_push(file_path: Path) -> str:
    app_id, app_secret, open_id = feishu_conf()
    if not (app_id and app_secret and open_id):
        raise RuntimeError("飞书凭证不全（需 FEISHU_APP_ID/SECRET + FEISHU_OPEN_ID）")
    tok = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                        json={"app_id": app_id, "app_secret": app_secret}, timeout=10).json()
    if tok.get("code") != 0:
        raise RuntimeError(f"获取 token 失败: {tok.get('msg')}")
    with open(file_path, "rb") as f:
        up = requests.post("https://open.feishu.cn/open-apis/im/v1/files",
                           headers={"Authorization": f"Bearer {tok['tenant_access_token']}"},
                           files={"file": (file_path.name, f)},
                           data={"file_type": "stream", "file_name": file_path.name},
                           timeout=30).json()
    if up.get("code") != 0:
        raise RuntimeError(f"上传失败: {up.get('msg')}")
    sent = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
        headers={"Authorization": f"Bearer {tok['tenant_access_token']}",
                 "Content-Type": "application/json; charset=utf-8"},
        json={"receive_id": open_id, "msg_type": "file",
              "content": json.dumps({"file_key": up["data"]["file_key"]})},
        timeout=10).json()
    if sent.get("code") != 0:
        raise RuntimeError(f"发送失败: {sent.get('msg')}")
    return sent["data"]["message_id"]


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="大盘复盘 HTML 日报（渲染+飞书推送）")
    ap.add_argument("--date", help="指定交易日 YYYYMMDD，默认最新")
    ap.add_argument("--no-update", action="store_true", help="不先增量更新 market_state_daily")
    ap.add_argument("--no-push", action="store_true", help="只渲染不推送")
    args = ap.parse_args()

    if not args.no_update:
        ucon = connect()
        n = update(ucon)
        ucon.close()
        print(f"[market_state 增量 +{n} 行]", file=sys.stderr)

    con = sqlite3.connect(DB)
    date = args.date or con.execute("SELECT MAX(trade_date) FROM market_state_daily").fetchone()[0]
    v = collect(con, date)
    con.close()

    html = render(v)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"market_review_{v['date']}.html"
    out.write_text(html, encoding="utf-8")
    print(f"[ok] HTML 已生成: {out}")

    if not args.no_push:
        msg_id = feishu_push(out)
        print(f"[ok] 飞书推送成功: msg_id={msg_id}")
    else:
        print("[ok] --no-push：跳过飞书推送")

    sh_txt = f"上证 {v['sh_close']:.1f}（{v['sh_pct']:+.2f}%）" if v["sh_close"] is not None else "上证 缺数"
    print(f"SUMMARY｜{v['d']}｜{sh_txt}｜当日{v['day_tag']}｜中期{v['mid_seg']}，MA60 {v['slope_zh']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
