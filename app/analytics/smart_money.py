"""三路聪明钱市场大方向分析（社保 / 公募 / 国家队）。

数据来源：
- fund_holdings（天天基金爬虫）：公募主动权益基金前十大重仓股
- top10_holders / top10_floatholders（tushare）：社保/汇金/养老金股东明细
- stock_basic.industry（东财行业）：行业映射

设计见 specs/smart-money-tracking.md。
注意：前十大重仓股聚合是"近似行业配置"（季报只披露前十大，约占仓位 50-70%），
报告输出必须标注该近似性。
"""
from __future__ import annotations

import logging
import sqlite3

import pandas as pd

logger = logging.getLogger(__name__)


def _query(conn, sql: str, params: tuple = ()) -> list:
    """执行 SQL 返回行列表（兼容 sqlite3.Connection）。

    表不存在时返回空列表（如尚未同步 top10_holders / fund_holdings），
    使报告优雅降级而非报错。
    """
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            logger.warning("table missing, skip query: %s", str(exc))
            return []
        raise

# 聪明钱关键词（holder_name 匹配）
_SOCIAL_SECURITY = "%社保%"
_HUIJIN = "%汇金%"
_PENSION = "%养老%"


def _industry_map(conn) -> dict[str, str]:
    """ts_code → 东财行业 映射。"""
    rows = _query(conn, "SELECT ts_code, industry FROM stock_basic WHERE industry IS NOT NULL")
    return {r[0]: r[1] for r in rows}


def industry_allocation(conn, end_date: str) -> pd.DataFrame:
    """公募行业配置：fund_holdings 按行业聚合（持仓市值加权）。

    大白话：把全市场主动权益基金的前十大重仓股，按股票所属行业加总市值，
    算出每个行业占公募总持仓的比例——这就是"公募在买什么方向"。
    """
    rows = _query(conn, 
        """
        SELECT f.ts_code, f.hold_amount
        FROM fund_holdings f
        WHERE f.end_date = ? AND f.hold_amount IS NOT NULL
        """,
        (end_date,),
    )
    if not rows:
        return pd.DataFrame(columns=["industry", "amount", "ratio"])
    industry_map = _industry_map(conn)
    df = pd.DataFrame(rows, columns=["ts_code", "hold_amount"])
    df["industry"] = df["ts_code"].map(industry_map).fillna("未分类")
    grouped = df.groupby("industry")["hold_amount"].sum().sort_values(ascending=False)
    total = grouped.sum()
    result = pd.DataFrame({"amount": grouped, "ratio": grouped / total})
    result = result.reset_index().rename(columns={"index": "industry"})
    return result


def concentration(conn, end_date: str) -> dict:
    """抱团集中度：CR10（前十大重仓股市值占比）、行业集中度、HHI。

    大白话：
    - CR10：公募最爱的 10 只股票占全部持仓的比例——越高说明越抱团
    - 行业集中度：前 3 大行业占比合计
    - HHI：行业占比平方和，>0.25 视为高度集中（一家独大）
    """
    rows = _query(conn, 
        """
        SELECT ts_code, hold_amount FROM fund_holdings
        WHERE end_date = ? AND hold_amount IS NOT NULL
        """,
        (end_date,),
    )
    if not rows:
        return {"cr10": None, "top3_industry": None, "hhi": None}
    df = pd.DataFrame(rows, columns=["ts_code", "hold_amount"])
    total = df["hold_amount"].sum()
    # CR10
    top10 = df.groupby("ts_code")["hold_amount"].sum().nlargest(10).sum()
    cr10 = top10 / total if total else None
    # 行业集中度 + HHI
    industry_map = _industry_map(conn)
    df["industry"] = df["ts_code"].map(industry_map).fillna("未分类")
    ind_ratio = df.groupby("industry")["hold_amount"].sum() / total
    top3 = ind_ratio.nlargest(3).sum()
    hhi = (ind_ratio**2).sum()
    return {"cr10": cr10, "top3_industry": top3, "hhi": hhi}


def top_holdings_change(conn, end_date: str, prev_date: str) -> dict:
    """公募重仓股换血：本季度 vs 上季度，前十大重仓股的新进/退出。

    大白话：公募整体最爱的 10 只股票，这个季度换了谁进来、谁被挤出去。
    """
    def top10(d: str) -> set[str]:
        rows = _query(conn, 
            """
            SELECT ts_code, SUM(hold_amount) AS amt FROM fund_holdings
            WHERE end_date = ? AND hold_amount IS NOT NULL
            GROUP BY ts_code ORDER BY amt DESC LIMIT 10
            """,
            (d,),
        )
        return {r[0] for r in rows}

    cur = top10(end_date)
    prev = top10(prev_date) if prev_date else set()
    # 名称映射（从 stock_basic 全量取一次）
    names = {r[0]: r[1] for r in _query(conn, "SELECT ts_code, name FROM stock_basic")}
    return {
        "new_entries": [(c, names.get(c, c)) for c in sorted(cur - prev)],
        "exits": [(c, names.get(c, c)) for c in sorted(prev - cur)],
        "current_top10": [(c, names.get(c, c)) for c in sorted(cur)],
    }


def smart_money_flow(conn, end_date: str, prev_date: str | None = None) -> dict:
    """社保/汇金/养老金：行业分布 + 增减持（top10_holders 全量明细）。

    大白话：国家队/社保直接持有的股票，按行业加总**市值**，看钱往哪个行业走；
    再对比相邻季度，看具体增持/减持了多少股。

    注意：top10_holders.hold_amount 是持股**股数**（不是市值），
    行业分布需乘最新收盘价换算成市值；增减持则看股数变化（原值相减）。

    报告期自动对齐：top10_holders 披露滞后（如公募已到 2026Q2 时股东数据
    可能只到 2025Q4），这里自动取 top10_holders 的最新可用季度，避免空报告。
    """
    avail = _query(conn, "SELECT MAX(end_date) FROM top10_holders")
    avail = avail[0][0] if avail and avail[0][0] else None
    if not avail:
        return {"industry": pd.DataFrame(), "changes": [], "used_end": None}
    if end_date > avail:
        end_date = avail
    if prev_date and prev_date > avail:
        # 自动取 avail 的上一季度（与 end_date fallback 对齐）
        y, m = int(avail[:4]), int(avail[4:6])
        q_end = {"03": "1231", "06": "0331", "09": "0630", "12": "0930"}
        prev_date = f"{y - 1 if m == 3 else y}{q_end[avail[4:6]]}"

    latest_date = _query(conn, "SELECT MAX(trade_date) FROM daily")
    latest_date = latest_date[0][0] if latest_date else None
    industry_map = _industry_map(conn)
    names = {r[0]: r[1] for r in _query(conn, "SELECT ts_code, name FROM stock_basic")}

    def holders_for(d: str) -> pd.DataFrame:
        rows = _query(conn, 
            """
            SELECT h.ts_code, h.holder_name, h.hold_amount AS hold_shares,
                   h.hold_amount * d.close AS hold_amount, h.hold_ratio
            FROM top10_holders h
            JOIN stock_basic s ON h.ts_code = s.ts_code
            LEFT JOIN daily d ON h.ts_code = d.ts_code AND d.trade_date = ?
            WHERE h.end_date = ? AND s.industry IS NOT NULL
            """,
            (latest_date, d),
        )
        if not rows:
            return pd.DataFrame(columns=["ts_code", "holder_name", "hold_shares", "hold_amount", "hold_ratio"])
        df = pd.DataFrame(rows, columns=["ts_code", "holder_name", "hold_shares", "hold_amount", "hold_ratio"])
        mask = df["holder_name"].str.contains("社保|汇金|养老", na=False)
        return df[mask]

    cur = holders_for(end_date)
    if cur.empty:
        return {"industry": pd.DataFrame(), "changes": [], "used_end": end_date}

    # 行业分布
    cur = cur.copy()
    cur["industry"] = cur["ts_code"].map(industry_map).fillna("未分类")
    ind = cur.groupby("industry")["hold_amount"].sum().sort_values(ascending=False)
    ind_df = pd.DataFrame({"amount": ind, "ratio": ind / ind.sum()}).reset_index()

    # 增减持（对比相邻季度，同 holder_name + ts_code）
    changes: list[dict] = []
    if prev_date:
        prev = holders_for(prev_date)
        if not prev.empty:
            merged = cur.merge(
                prev,
                on=["ts_code", "holder_name"],
                how="outer",
                suffixes=("_cur", "_prev"),
            )
            merged["delta"] = merged["hold_shares_cur"].fillna(0) - merged["hold_shares_prev"].fillna(0)
            moved = merged[merged["delta"].abs() > 0].copy()
            moved["name"] = moved["ts_code"].map(names)
            moved = moved.sort_values("delta", ascending=False)
            changes = [
                {
                    "ts_code": r.ts_code,
                    "name": r.name,
                    "holder": r.holder_name,
                    "delta": r.delta,
                }
                for r in moved.itertuples()
            ]
    return {"industry": ind_df, "changes": changes, "used_end": end_date, "used_prev": prev_date}


def report(conn, end_date: str, prev_date: str | None = None) -> str:
    """季度《聪明钱市场大方向报告》文本输出。"""
    lines: list[str] = []
    lines.append(f"===== 聪明钱市场大方向报告（{end_date}）=====")
    lines.append("（数据口径：公募=主动权益基金前十大重仓股聚合，为近似行业配置，非全仓精确值）")
    lines.append("")

    # 1. 公募行业配置
    ind = industry_allocation(conn, end_date)
    if not ind.empty:
        lines.append("【1】公募行业配置（持仓市值加权）")
        lines.append(ind.head(10).to_string(index=False))
        # QoQ 变化：加仓 TOP5 / 减仓 TOP5
        if prev_date:
            prev_ind = industry_allocation(conn, prev_date)
            if not prev_ind.empty:
                merged = ind.merge(
                    prev_ind[["industry", "ratio"]],
                    on="industry",
                    how="outer",
                    suffixes=("_cur", "_prev"),
                ).fillna(0)
                merged["delta"] = merged["ratio_cur"] - merged["ratio_prev"]
                merged = merged.sort_values("delta", ascending=False)
                add_top = merged.head(5)
                cut_top = merged.tail(5).iloc[::-1]
                lines.append(f"  QoQ 加仓 TOP5（{prev_date} → {end_date}，占比变化）:")
                for r in add_top.itertuples():
                    lines.append(f"    {r.industry}: {r.delta:+.1%}")
                lines.append(f"  QoQ 减仓 TOP5:")
                for r in cut_top.itertuples():
                    lines.append(f"    {r.industry}: {r.delta:+.1%}")
        lines.append("")

    # 2. 抱团集中度
    conc = concentration(conn, end_date)
    if conc["cr10"] is not None:
        lines.append("【2】抱团集中度")
        lines.append(
            f"  CR10（前十大重仓股占比）: {conc['cr10']:.1%}"
            f"  | 前3大行业占比: {conc['top3_industry']:.1%}"
            f"  | HHI: {conc['hhi']:.3f}（>0.25 高度集中）"
        )
        lines.append("")

    # 3. 重仓股换血
    if prev_date:
        chg = top_holdings_change(conn, end_date, prev_date)
        lines.append(f"【3】公募重仓股换血（{prev_date} → {end_date}）")
        lines.append(f"  新进前十: {', '.join(n for _, n in chg['new_entries']) or '无'}")
        lines.append(f"  退出前十: {', '.join(n for _, n in chg['exits']) or '无'}")
        lines.append("")

    # 4. 社保/汇金
    flow = smart_money_flow(conn, end_date, prev_date)
    if not flow["industry"].empty:
        used_end = flow.get("used_end", end_date)
        lines.append(f"【4】社保/汇金/养老金行业分布（直接持股，按市值聚合，单位亿元，报告期 {used_end}）")
        ind_view = flow["industry"].head(8).copy()
        ind_view["市值(亿)"] = (ind_view["amount"] / 1e8).round(1)
        ind_view["占比"] = (ind_view["ratio"] * 100).round(2).astype(str) + "%"
        lines.append(ind_view[["industry", "市值(亿)", "占比"]].to_string(index=False))
        if flow["changes"]:
            used_prev = flow.get("used_prev", prev_date)
            lines.append(f"  增减持 TOP5（{used_prev} → {used_end}，单位：万股）:")
            for c in flow["changes"][:5]:
                lines.append(f"    {c['name']}({c['ts_code']}) {c['holder']}: {c['delta'] / 1e4:+,.0f} 万股")
        lines.append("")

    # 5. 结论段（自动生成）
    lines.append("【5】结论")
    if not ind.empty:
        top_ind = ind.iloc[0]
        lines.append(
            f"  公募资金最集中的方向是「{top_ind['industry']}」（占 {top_ind['ratio']:.1%}）。"
        )
        conc_text = []
        if conc["top3_industry"] is not None and conc["top3_industry"] > 0.5:
            conc_text.append(f"前3大行业合计 {conc['top3_industry']:.0%}，方向高度集中")
        if conc["hhi"] is not None and conc["hhi"] > 0.25:
            conc_text.append("HHI 偏高，抱团拥挤")
        if conc_text:
            lines.append(f"  拥挤度提示：{'；'.join(conc_text)}——止盈纪律优先，不追高。")
        else:
            lines.append("  行业集中度尚可，抱团风险相对可控。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML 报告（内置模板，纯 Python 渲染，无 jinja2 依赖）
# ---------------------------------------------------------------------------

_HTML_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--tx:#1a1f26;--mut:#5b6472;--acc:#2563eb;
      --up:#16a34a;--down:#e74c3c;--line:#e5e8ec;--warn:#d97706}
@media(prefers-color-scheme:dark){:root{--bg:#12161c;--card:#1b2129;--tx:#e6e9ee;
      --mut:#98a1ad;--acc:#60a5fa;--up:#4ade80;--down:#f87171;--line:#2a323d}}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);
     font:15px/1.75 -apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
     padding:28px 14px}
main{max-width:760px;margin:0 auto}
header{background:linear-gradient(135deg,#1e3a5f,#2563eb);border-radius:14px;
       padding:22px 26px;color:#fff;margin-bottom:18px}
header h1{font-size:22px;font-weight:700;letter-spacing:.5px}
header .sub{opacity:.85;font-size:13px;margin-top:4px}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
.kpi{flex:1;min-width:130px;background:var(--card);border:1px solid var(--line);
     border-radius:12px;padding:12px 16px}
.kpi b{display:block;color:var(--mut);font-weight:500;font-size:12px;
       letter-spacing:.5px;margin-bottom:4px}
.kpi span{font-size:20px;font-weight:700}
.kpi .warn{color:var(--warn)}
.kpi .ok{color:var(--up)}
section{margin-top:22px}
h2{font-size:15px;font-weight:600;color:var(--acc);margin-bottom:10px;
   letter-spacing:1px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
      padding:14px 18px;margin-bottom:12px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{color:var(--mut);font-weight:500;font-size:12px;text-align:left;
   padding:6px 8px;border-bottom:1px solid var(--line);letter-spacing:.5px}
td{padding:7px 8px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:none}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{display:inline-block;width:90px;height:8px;background:var(--line);
     border-radius:4px;vertical-align:middle;margin-right:8px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--acc);border-radius:4px}
.two{display:flex;gap:16px;flex-wrap:wrap}
.two>div{flex:1;min-width:220px}
h3{font-size:13px;font-weight:600;color:var(--mut);margin-bottom:8px;
   letter-spacing:.5px}
ul{list-style:none}
li{padding:4px 0;font-size:14px}
.up{color:var(--up)} .down{color:var(--down)}
.conclusion{background:linear-gradient(135deg,#fef3c7,#fde68a);
            border:1px solid #f59e0b;border-radius:12px;padding:16px 20px;
            margin-top:22px;font-size:15px;line-height:1.9}
@media(prefers-color-scheme:dark){.conclusion{background:#3a2f12;border-color:#b45309}}
.conclusion b{color:#92400e}
@media(prefers-color-scheme:dark){.conclusion b{color:#fbbf24}}
footer{margin-top:26px;padding-top:12px;border-top:1px solid var(--line);
       color:var(--mut);font-size:12px;line-height:1.8}
"""


def _esc(s) -> str:
    import html as _html
    return _html.escape(str(s), quote=True)


def report_html(conn, end_date: str, prev_date: str | None = None) -> str:
    """季度《聪明钱市场大方向报告》HTML 页面（内置模板，无外部依赖）。

    结构：指标卡（CR10/前3行业/HHI）→ 公募行业配置（占比条）→ QoQ 变化 →
    重仓股换血 → 社保/汇金分布 → 结论高亮。
    """
    ind = industry_allocation(conn, end_date)
    conc = concentration(conn, end_date)
    chg = top_holdings_change(conn, end_date, prev_date) if prev_date else None
    flow = smart_money_flow(conn, end_date, prev_date)

    # 指标卡
    kpi_html = ""
    if conc["cr10"] is not None:
        top3_cls = "warn" if conc["top3_industry"] and conc["top3_industry"] > 0.5 else "ok"
        hhi_cls = "warn" if conc["hhi"] and conc["hhi"] > 0.25 else "ok"
        kpi_html = f"""
<div class="kpis">
  <div class="kpi"><b>CR10 · 前十大重仓股占比</b><span>{conc['cr10']:.1%}</span></div>
  <div class="kpi"><b>前3大行业占比</b><span class="{top3_cls}">{conc['top3_industry']:.1%}</span></div>
  <div class="kpi"><b>HHI 行业集中度</b><span class="{hhi_cls}">{conc['hhi']:.3f}</span></div>
</div>"""

    # 公募行业配置表（带占比条）
    ind_html = ""
    if not ind.empty:
        max_ratio = ind["ratio"].max()
        rows = []
        for r in ind.head(10).itertuples():
            pct = r.ratio * 100
            bar_w = (r.ratio / max_ratio * 100) if max_ratio else 0
            rows.append(
                f"<tr><td>{_esc(r.industry)}</td>"
                f"<td><span class='bar'><i style='width:{bar_w:.0f}%'></i></span>{pct:.1f}%</td>"
                f"<td class='num'>{r.amount / 1e4:.1f} 亿</td></tr>"
            )
        ind_html = f"""
<section><h2>公募行业配置（持仓市值加权）</h2><div class="card">
<table><tr><th>行业</th><th>占比</th><th>持仓市值</th></tr>{''.join(rows)}</table>
</div></section>"""

    # QoQ 变化
    qoq_html = ""
    if prev_date and not ind.empty:
        prev_ind = industry_allocation(conn, prev_date)
        if not prev_ind.empty:
            merged = ind.merge(prev_ind[["industry", "ratio"]], on="industry", how="outer",
                               suffixes=("_cur", "_prev")).fillna(0)
            merged["delta"] = merged["ratio_cur"] - merged["ratio_prev"]
            merged = merged.sort_values("delta", ascending=False)
            add_items = "".join(
                f"<li class='up'>▲ {_esc(r.industry)} <b>{r.delta:+.1%}</b></li>"
                for r in merged.head(5).itertuples()
            )
            cut_items = "".join(
                f"<li class='down'>▼ {_esc(r.industry)} <b>{r.delta:+.1%}</b></li>"
                for r in merged.tail(5).iloc[::-1].itertuples()
            )
            qoq_html = f"""
<section><h2>QoQ 行业配置变化（{prev_date} → {end_date}）</h2><div class="card two">
<div><h3>加仓 TOP5</h3><ul>{add_items}</ul></div>
<div><h3>减仓 TOP5</h3><ul>{cut_items}</ul></div>
</div></section>"""

    # 重仓股换血
    chg_html = ""
    if chg:
        new_items = "".join(f"<li class='up'>▲ {_esc(n)}</li>" for _, n in chg["new_entries"]) or "<li>无</li>"
        exit_items = "".join(f"<li class='down'>▼ {_esc(n)}</li>" for _, n in chg["exits"]) or "<li>无</li>"
        chg_html = f"""
<section><h2>公募重仓股换血（{prev_date} → {end_date}）</h2><div class="card two">
<div><h3>新进前十</h3><ul>{new_items}</ul></div>
<div><h3>退出前十</h3><ul>{exit_items}</ul></div>
</div></section>"""

    # 社保/汇金
    flow_html = ""
    if not flow["industry"].empty:
        used_end = flow.get("used_end", end_date)
        rows = []
        for r in flow["industry"].head(8).itertuples():
            rows.append(
                f"<tr><td>{_esc(r.industry)}</td>"
                f"<td class='num'>{r.amount / 1e8:.1f} 亿</td>"
                f"<td class='num'>{r.ratio:.1%}</td></tr>"
            )
        chg_items = ""
        if flow["changes"]:
            used_prev = flow.get("used_prev", prev_date)
            chg_items = "".join(
                f"<li>{_esc(c['name'])}（{_esc(c['holder'])}）"
                f"<b class='{'up' if c['delta'] > 0 else 'down'}'>"
                f"{c['delta'] / 1e4:+,.0f} 万股</b></li>"
                for c in flow["changes"][:5]
            )
        flow_html = f"""
<section><h2>社保 / 汇金 / 养老金（直接持股，报告期 {used_end}）</h2>
<div class="card"><table><tr><th>行业</th><th>市值</th><th>占比</th></tr>{''.join(rows)}</table></div>
{f'<div class="card"><h3>增减持 TOP5（{used_prev} → {used_end}）</h3><ul>{chg_items}</ul></div>' if chg_items else ''}
</section>"""

    # 结论
    concl_html = ""
    if not ind.empty:
        top_ind = ind.iloc[0]
        parts = [f"公募资金最集中的方向是「{_esc(top_ind['industry'])}」（占 {top_ind['ratio']:.1%}）"]
        if conc["top3_industry"] is not None and conc["top3_industry"] > 0.5:
            parts.append(f"前3大行业合计 {conc['top3_industry']:.0%}，方向高度集中")
        if conc["hhi"] is not None and conc["hhi"] > 0.25:
            parts.append("HHI 偏高，抱团拥挤")
        if len(parts) > 1:
            parts.append("止盈纪律优先，不追高")
        concl_html = f'<div class="conclusion"><b>结论：</b>{"；".join(parts)}。</div>'

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>聪明钱市场大方向报告 · {end_date}</title>
<style>{_HTML_CSS}</style></head><body><main>
<header>
  <h1>🧠 聪明钱市场大方向报告</h1>
  <div class="sub">报告期 {end_date} · 自动生成 · 数据口径：公募=主动权益基金前十大重仓股聚合（近似行业配置，非全仓精确值）</div>
</header>
{kpi_html}
{ind_html}
{qoq_html}
{chg_html}
{flow_html}
{concl_html}
<footer>数据源：天天基金 fundf10（公募持仓）· tushare top10_holders（社保/汇金/养老金）· 东财行业映射<br>
定位：第二层证据（资金面共识验证），非买入信号。季度频率，披露滞后 1-2 个月。</footer>
</main></body></html>"""