#!/usr/bin/env python3
"""从 stocks 站点（CF Pages + D1）同步关注个股池到本地。

数据源：stocks-site 的 D1 `stocks` 表（借 projects/stocks-site/scripts/cf_d1.py 直连，
免登录，token 从 .secrets/cf_api_token 读取、不回显）。

产物（stock-analytics/data/ 下）：
  watchlist_site.txt  纯 ts_code 清单，一行一个（兼容旧脚本）
  watchlist_site.csv  code,name,category,subtype,synced_at（scan_pullback.py 默认读它）

输出：关注池总数、分类分布、与上次同步的差异（新增/移除明细）。

用法：
    ./venv/bin/python scripts/sync_watchlist.py
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CF_D1 = Path("/root/.qwenpaw/workspaces/default/projects/stocks-site/scripts/cf_d1.py")
SQL = "SELECT code, name, category, subtype FROM stocks ORDER BY code"
TXT_PATH = DATA / "watchlist_site.txt"
CSV_PATH = DATA / "watchlist_site.csv"


def fetch_from_site() -> list[dict]:
    r = subprocess.run(["python3", str(CF_D1), "query", "--one", SQL],
                       capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        sys.exit(f"cf_d1.py 查询失败：{(r.stderr or r.stdout).strip()[:400]}")
    rows = json.loads(r.stdout)
    if not isinstance(rows, list) or not rows:
        sys.exit(f"cf_d1.py 返回异常或空池：{r.stdout[:200]}")
    bad = [r.get("code", "?") for r in rows if not r.get("code")]
    if bad:
        sys.exit(f"stocks 表存在缺 code 的行：{bad[:5]}")
    return rows


def main() -> int:
    rows = fetch_from_site()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    first_sync = not TXT_PATH.exists()

    old = set(TXT_PATH.read_text().split()) if TXT_PATH.exists() else set()
    new_codes = [r["code"] for r in rows]

    DATA.mkdir(exist_ok=True)
    TXT_PATH.write_text("\n".join(new_codes) + "\n")
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "category", "subtype", "synced_at"])
        for r in rows:
            w.writerow([r["code"], r["name"], r.get("category", ""),
                        r.get("subtype") or "", now])

    stat: dict[str, int] = {}
    for r in rows:
        stat[r.get("category") or "?"] = stat.get(r.get("category") or "?", 0) + 1

    print(f"同步完成 {now}｜关注池 {len(rows)} 只："
          + "、".join(f"{k} {v}" for k, v in sorted(stat.items())))
    if first_sync:
        print("首次同步，建立本地基准（无历史可比）。")
        return 0
    added = [c for c in new_codes if c not in old]
    removed = sorted(old - set(new_codes))
    if added:
        print(f"新增 {len(added)} 只：{', '.join(added)}")
    if removed:
        print(f"移除 {len(removed)} 只：{', '.join(removed)}")
    if not added and not removed:
        print("与上次相比无变化。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
