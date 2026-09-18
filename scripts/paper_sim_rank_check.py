#!/usr/bin/env python3
"""模拟盘排序键定期复核：新排序（量能确认优先→离底高度降序）vs 实测结果。

背景（2026-09-18 定稿）：档内排序从「60日日均振幅降序」改为「量能确认优先→离底高度降序」，
依据 = 8990 笔信号分层回测（量能确认 +8.9%/胜率66% vs 无 +4.2%/58%；离底高度单调，组间差 8.6pp，
贴底组先止损率 49%）。振幅被否（与 f20 秩相关 -0.02，最高档先止损率 39%）。

本脚本：模拟盘全部超跌（ignition）交易按排序键分组对照——所有信号无差别建仓，无幸存者偏差。
  - 已平仓：剔 .BJ 后的纯策略出场（教训：北交所剔除清仓的 19 笔曾污染分组，见 memory/2026-09-18）
  - 在途持仓：浮盈快照（仅早期信号，未经完整出场周期）
  - 量能确认三分（量托/仅OBV/无）：8990 笔回测中 OBV 单看无区分，混计会稀释量托
判定（每月 20 日前后跑，首检=模拟盘交易日满 20 个，约 2026-10 下旬）：
  - 离底高组（在途+出场）持续优于离底低组 → 排序键维持
  - 连续两次复核组间方向反转 → 停下重议排序键（找用户）
局限：前 20 个交易日内任何组间差异均无统计力，只看方向，不下结论。

用法：cd /home/application/stock-analytics && ./venv/bin/python scripts/paper_sim_rank_check.py
"""
import glob
import sqlite3
from datetime import date

import pandas as pd

ROOT = "/home/application/stock-analytics"
TODAY = date.today().strftime("%Y%m%d")   # 浮盈快照基准日；库内无当日行情时自动回退


def load_sig() -> pd.DataFrame:
    sigs = []
    for f in sorted(glob.glob(f"{ROOT}/data/signals/ignition_*.csv")):
        d = pd.read_csv(f, dtype={"代码": str, "信号日": str})
        sigs.append(d[["代码", "信号日", "距60日低点%", "量托确认", "OBV金叉确认"]])
    sig = pd.concat(sigs).drop_duplicates(subset=["代码", "信号日"])
    sig.columns = ["ts_code", "signal_date", "off_low", "liangtuo", "obv"]
    return sig


def show(g: pd.DataFrame, label: str) -> None:
    if len(g) == 0:
        print(f"  {label:<18} 笔数   0")
        return
    print(f"  {label:<18} 笔数 {len(g):3d}｜胜率 {(g.ret>0).mean()*100:5.1f}%｜"
          f"平均ret {g.ret.mean()*100:+.2f}%｜中位 {g.ret.median()*100:+.2f}%｜pnl {g.pnl.sum():+,.0f}元")


def main() -> None:
    sig = load_sig()
    con = sqlite3.connect(f"{ROOT}/data/paper_sim.db")
    tr = pd.read_sql_query(
        "SELECT t.ts_code, t.name, t.ret, t.pnl, t.days, t.reason, s.signal_date "
        "FROM trades t JOIN signal_pool s ON s.id=t.signal_id WHERE t.strategy='ignition'", con)

    pure = tr[~tr.ts_code.str.endswith(".BJ")].merge(sig, on=["ts_code", "signal_date"], how="left")
    n_bj = len(tr) - len(pure)
    print(f"===== 已平仓·纯策略出场 {len(pure)} 笔（剔 .BJ 共 {n_bj} 笔：含北交所剔除清仓+存量 .BJ 正常出场）=====")
    print(pure.reason.value_counts().to_string().replace("\n", "、"))
    pure = pure.copy()
    pure["确认类"] = pure.apply(
        lambda r: "量托✓" if r.liangtuo else ("仅OBV✓" if r.obv else "无确认"), axis=1)
    for v, g in pure.groupby("确认类"):
        show(g, v)
    med = pure.off_low.median()
    show(pure[pure.off_low >= med], f"离底高(≥{med:.1f}%)")
    show(pure[pure.off_low < med], f"离底低(<{med:.1f}%)")

    op = pd.read_sql_query(
        "SELECT id, ts_code, signal_date, entry_price_adj, shares FROM signal_pool "
        "WHERE status='opened' AND strategy='ignition'", con)
    con.close()
    scon = sqlite3.connect(f"{ROOT}/data/stock.db")
    px = pd.read_sql_query(
        f"SELECT d.ts_code, d.close*a.adj_factor AS px_adj FROM daily d "
        f"JOIN adj_factor a ON a.ts_code=d.ts_code AND a.trade_date=d.trade_date "
        f"WHERE d.trade_date=(SELECT MAX(trade_date) FROM daily WHERE trade_date<='{TODAY}')", scon)
    scon.close()
    op = op.merge(px, on="ts_code", how="inner").merge(sig, on=["ts_code", "signal_date"], how="left")
    op["unreal"] = op.px_adj / op.entry_price_adj - 1
    op["确认类"] = op.apply(
        lambda r: "量托✓" if r.liangtuo else ("仅OBV✓" if r.obv else "无确认"), axis=1)
    med_o = op.off_low.median()
    print(f"\n===== 在途持仓浮盈快照 {len(op)} 笔（未完整周期，仅看方向）=====")
    for v, g in op.groupby("确认类"):
        print(f"  {v:<18} 笔数 {len(g):4d}｜浮盈均值 {g.unreal.mean()*100:+.2f}%｜中位 {g.unreal.median()*100:+.2f}%｜"
              f"浮盈>0 {(g.unreal>0).mean()*100:.1f}%｜浮亏<-5% {(g.unreal<-0.05).mean()*100:.1f}%")
    for name, g in [("离底高(≥中位)", op[op.off_low >= med_o]), ("离底低(<中位)", op[op.off_low < med_o])]:
        print(f"  {name:<18} 笔数 {len(g):4d}｜浮盈均值 {g.unreal.mean()*100:+.2f}%｜中位 {g.unreal.median()*100:+.2f}%｜"
              f"浮盈>0 {(g.unreal>0).mean()*100:.1f}%｜浮亏<-5% {(g.unreal<-0.05).mean()*100:.1f}%")
    print("\n判定：离底高组持续占优 → 维持排序；连续两次复核方向反转 → 重议排序键。"
          "前 20 个交易日内无统计力，只看方向。")


if __name__ == "__main__":
    main()
