# -*- coding: utf-8 -*-
"""
主线状态机参考实现（冻结版）——与 data/mainline_signals_reference.json 逐字对应。
mainline_monitor.py 的状态机边界必须与本文件完全一致。
本文件仅作对账基准，不接入生产。
"""
import sqlite3
from collections import defaultdict

BS = "/home/application/stock-analytics/data/board_strength.db"

EVENT = {"三季报增长","一季报增长","中报增长","年报增长","并购重组","股权转让","实控人变更",
         "次新股","高送转","ST板块","破净股","低价股","送转填权","年报预增","业绩增长",
         "再融资","举牌","可转债"}

def run_reference():
    db = sqlite3.connect(BS)
    rot = db.execute("select date, name, value, rank from board_strength "
                     "where scope='rotat' order by date").fetchall()
    db.close()
    cal = sorted({r[0] for r in rot})
    didx = {d: i for i, d in enumerate(cal)}
    byd = defaultdict(dict)
    for d, n, v, r in rot:
        byd[d][n] = v

    # 状态字段（每个板块）：
    #   on      : 活跃期内全部在榜日期列表（顺序）
    #   seg     : 活跃期起点日期（首次在榜日或最近一次重置日）
    #   peak    : 活跃期内强度最大值（含当日）
    #   conf    : None 或 确认信号类型字符串（FAST/A/B）
    #   surv    : None 或 剩余复核天数 int（确认当日设为 5）
    #   tv      : 触发日强度（trig_value）
    #   cd      : cooldown 剩余交易日数
    #   miss    : 确认板块连续不在榜天数（V2 退出规则：满 3 → EXITED）
    st = {}
    sigs = []

    for i, d in enumerate(cal):
        on = {n: v for n, v in byd[d].items() if n not in EVENT}

        # 1) 更新在榜（先于一切判定；在榜日 miss 清零）
        for n, v in on.items():
            s = st.setdefault(n, {"on": [], "seg": d, "peak": -1e18,
                                  "conf": None, "surv": None, "tv": None, "cd": 0, "miss": 0})
            s["on"].append(d)
            s["peak"] = max(s["peak"], v)
            s["miss"] = 0

        # 2) 段重置：不在榜 且 距上次在榜 > 5 个交易日（用交易日序号差）
        for n, s in st.items():
            if n not in on and s["on"] and i - didx[s["on"][-1]] > 5:
                st[n] = {"on": [], "seg": d, "peak": -1e18,
                         "conf": None, "surv": None, "tv": None, "cd": 0, "miss": 0}

        # 2.5) 主线退出（V2）：确认板块连续 3 个交易日不在榜 → EXITED + 重置
        for n, s in st.items():
            if s["conf"] and n not in on:
                s["miss"] = s.get("miss", 0) + 1
                if s["miss"] >= 3:
                    st[n] = {"on": [], "seg": d, "peak": -1e18,
                             "conf": None, "surv": None, "tv": None, "cd": 0, "miss": 0}

        # 3) 逐板块：存活复核 → cooldown → 确认信号
        for n, v in on.items():
            s = st[n]
            if s["conf"] and s["surv"] is not None:
                s["surv"] -= 1
                if s["surv"] <= 0:
                    on5 = [x for x in s["on"] if didx[x] > i - 5]
                    if len(on5) >= 3 and v >= s["tv"] * 0.6:
                        s["surv"] = None          # 复核通过，转为常态主线
                    else:
                        s["conf"] = None; s["tv"] = None; s["cd"] = 10
            if s["cd"] > 0:
                s["cd"] -= 1
                continue
            if s["conf"]:
                continue
            seg_len = i - didx[s["seg"]]
            on10 = sum(1 for x in s["on"] if didx[x] > i - 10)
            kind = None
            if v >= 17000:
                kind = "FAST"
            elif v >= 12000 and on10 >= 3:
                kind = "A"
            elif seg_len >= 11 and on10 >= 4:
                prevs = [byd[x][n] for x in s["on"] if x != d]
                if prevs and v > max(prevs):
                    kind = "B"
            if kind:
                s["conf"] = kind; s["tv"] = v; s["surv"] = 5
                sigs.append({"date": d, "board": n, "kind": kind})

        # 4) 观察池 PREP（在确认状态判定之后；未确认板块才可成为 PREP）
        for n, v in on.items():
            s = st[n]
            on5 = [x for x in s["on"] if didx[x] > i - 5]
            if len(on5) >= 2 and v >= 3000 and not s["conf"]:
                sigs.append({"date": d, "board": n, "kind": "PREP"})

    return sigs

if __name__ == "__main__":
    sigs = run_reference()
    import json
    from collections import Counter
    print(json.dumps({"total": len(sigs), "by_kind": dict(Counter(s["kind"] for s in sigs))}, ensure_ascii=False))
