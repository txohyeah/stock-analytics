#!/usr/bin/env python3
"""五项审查修复的回归测试；仅用内存库和模拟客户端，不触碰生产数据。

用法：./venv/bin/python tests/test_review_regressions.py
"""
import argparse
import contextlib
import io
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import cli
from app.analytics.account import _parse_position_size_pct
from app.analytics.baolei import GREEN, RED, UNKNOWN, YELLOW, _format_report, evaluate
from app.analytics.inputs import code_to_ts_code, normalize_code
import manual_sim


class PositionSizeTests(unittest.TestCase):
    def test_percent_suffix_is_never_scaled(self):
        for value, expected in [("1%", 1), ("0.5%", 0.5), ("0%", 0),
                                ("100%", 100), (" 1 % ", 1)]:
            with self.subTest(value=value):
                self.assertEqual(_parse_position_size_pct(value), expected)

    def test_fraction_and_plain_percentage(self):
        for value, expected in [("1", 100), ("0.5", 50), ("0", 0),
                                ("1.01", 1.01), (10, 10), (0, 0), (0.001, 0.1)]:
            with self.subTest(value=value):
                self.assertEqual(_parse_position_size_pct(value), expected)

    def test_invalid_and_nonfinite_values(self):
        for value in [None, "", " ", "%", "abc", "1%%", "-1%", -0.5,
                      "nan", "NaN%", "inf", "-inf%", "1e309%"]:
            with self.subTest(value=value):
                self.assertIsNone(_parse_position_size_pct(value))


class StockCodeTests(unittest.TestCase):
    def test_exchange_rules(self):
        cases = {"600519": "SH", "000001": "SZ", "300750": "SZ",
                 "430047": "BJ", "830799": "BJ", "870001": "BJ",
                 "920001": "BJ", "400001": "BJ", "900901": "SH",
                 "200002": "SZ", "510300": "SH"}
        for code, exchange in cases.items():
            with self.subTest(code=code):
                self.assertEqual(code_to_ts_code(code), f"{code}.{exchange}")

    def test_explicit_exchange_is_preserved(self):
        for value, expected in [("830799.BJ", "830799.BJ"),
                                ("bj830799", "830799.BJ"),
                                ("SH000001", "000001.SH"),
                                ("000001.SH", "000001.SH"),
                                ("920001", "920001.BJ")]:
            with self.subTest(value=value):
                self.assertEqual(normalize_code(value).ts_code, expected)


class BaoleiMissingDataTests(unittest.TestCase):
    code = "600000.SH"

    def healthy(self, **overrides):
        row = dict(end_date="20251231", n_income_attr_p=100, profit_dedt=90,
                   dt_netprofit_yoy=10, netprofit_yoy=12, n_income=100,
                   n_cashflow_act=120, goodwill=0, total_hldr_eqy_exc_min_int=1000)
        row.update(overrides)
        trend = dict(latest_ed="20260630", rev=120, prev_rev=100,
                     np=12, prev_np=10)
        return evaluate(self.code, {}, [row],
                        {self.code: {"20251231": "标准无保留意见"}}, trend)

    def test_empty_and_shell_rows_are_unknown(self):
        for annual in [[], [dict(end_date="20251231", revenue=100)]]:
            with self.subTest(annual=annual):
                result = evaluate(self.code, {}, annual)
                self.assertEqual([getattr(result, f"r{i}") for i in range(5)],
                                 [UNKNOWN] * 5)
                self.assertEqual(result.rating, UNKNOWN)
                report = _format_report(result)
                self.assertEqual(report.count("- [数据不足] 雷区"), 5)

    def test_complete_healthy_checks_remain_green(self):
        result = self.healthy()
        self.assertEqual([getattr(result, f"r{i}") for i in range(5)], [GREEN] * 5)
        self.assertEqual(result.rating, "低")

    def test_missing_fields_cannot_claim_healthy(self):
        for field, check in [("profit_dedt", "r1"), ("dt_netprofit_yoy", "r1"),
                             ("netprofit_yoy", "r1"), ("n_income_attr_p", "r1"),
                             ("n_cashflow_act", "r2"), ("n_income", "r2"),
                             ("goodwill", "r3"), ("total_hldr_eqy_exc_min_int", "r3")]:
            for missing in [None, "", float("nan"), float("inf")]:
                with self.subTest(field=field, missing=missing):
                    result = self.healthy(**{field: missing})
                    self.assertEqual(getattr(result, check), UNKNOWN)
                    self.assertEqual(result.rating, UNKNOWN)
                    self.assertIn("数据不足", getattr(result, f"{check}_detail"))

    def test_missing_audit_or_trend_blocks_low_rating(self):
        row = self.healthy().annual
        for audit, trend in [({}, {}),
                             ({}, dict(latest_ed="20260630", rev=120,
                                       prev_rev=100, np=12, prev_np=10)),
                             ({self.code: {"20251231": "标准无保留意见"}},
                              dict(latest_ed="20260630", rev=float("nan"),
                                   prev_rev=100, np=12, prev_np=10))]:
            result = evaluate(self.code, {}, row, audit, trend)
            self.assertEqual(result.rating, UNKNOWN)
            self.assertIn(UNKNOWN, (result.r0, result.r4))

    def test_missing_history_cannot_establish_healthy_trends(self):
        row = self.healthy().annual[0]
        result = evaluate(self.code, {}, [row, {"end_date": "20241231"}])
        self.assertEqual(result.r1, UNKNOWN)
        self.assertEqual(result.r2, UNKNOWN)

    def test_known_risks_are_not_hidden_by_missing_data(self):
        for overrides, check, level, rating in [
            ({"profit_dedt": -1, "dt_netprofit_yoy": None}, "r1", RED, "高"),
            ({"n_cashflow_act": -1, "n_income": None}, "r2", YELLOW, "中"),
            ({"goodwill": 400, "profit_dedt": None}, "r3", RED, "高"),
        ]:
            with self.subTest(check=check):
                result = self.healthy(**overrides)
                self.assertEqual(getattr(result, check), level)
                self.assertEqual(result.rating, rating)


class PartialSaleTests(unittest.TestCase):
    def test_repeated_partial_sales_conserve_cost_and_pnl(self):
        with sqlite3.connect(":memory:") as con:
            con.executescript(manual_sim.MANUAL_SCHEMA)
            con.execute("INSERT INTO manual_account VALUES (1, 1000000, 0, '')")
            con.execute("""INSERT INTO manual_positions
                (ts_code, name, source, entry_date, entry_price_adj,
                 entry_price_raw, shares, cost)
                VALUES ('600000.SH', '测试', 'test', '20261001', 10, 10, 1000, 10010)""")
            with patch.object(manual_sim, "adj_at", return_value=1), \
                 patch.object(manual_sim.ps, "trading_days", return_value=5), \
                 contextlib.redirect_stdout(io.StringIO()):
                for qty, remaining, cost in [(300, 700, 7007), (200, 500, 5005), (500, 0, None)]:
                    order_id = con.execute("""INSERT INTO manual_orders
                        (order_date, side, ts_code, exec_type) VALUES (?, 'sell', ?, 'close')""",
                        ("20261010", "600000.SH")).lastrowid
                    order = dict(id=order_id, position_id=1, ts_code="600000.SH", qty=qty)
                    manual_sim.fill_sell(con, None, order, "20261010", 12)
                    shares, stored_cost, status = con.execute(
                        "SELECT shares, cost, status FROM manual_positions WHERE id=1"
                    ).fetchone()
                    self.assertEqual(shares, remaining)
                    if cost is not None:
                        self.assertAlmostEqual(stored_cost, cost)
                        self.assertEqual(status, "open")
                        allocated = con.execute("SELECT SUM(cost) FROM manual_trades").fetchone()[0]
                        self.assertAlmostEqual(allocated + stored_cost, 10010)
                    else:
                        self.assertEqual(status, "closed")
            allocated, proceeds, pnl = con.execute(
                "SELECT SUM(cost), SUM(proceeds), SUM(pnl) FROM manual_trades"
            ).fetchone()
            self.assertAlmostEqual(allocated, 10010)
            self.assertAlmostEqual(pnl, proceeds - 10010)
            self.assertAlmostEqual(con.execute("SELECT cash FROM manual_account").fetchone()[0], proceeds)


class GroupExitCodeTests(unittest.TestCase):
    def args(self, dataset):
        return argparse.Namespace(dataset=dataset, fallback=False, ts_codes=None,
                                  ts_code=None, start="20261001", end="20261009",
                                  mode=None, history=False)

    def test_group_success_partial_and_total_failure(self):
        for dataset in ["market", "finance", "all"]:
            for results, failures, expected in [({"daily": (1, 1)}, [], 0),
                                                ({"daily": (1, 1)}, ["adj_factor: failed"], 1),
                                                ({}, ["daily: failed"], 1)]:
                with self.subTest(dataset=dataset, failures=failures):
                    ctx = SimpleNamespace(store=Mock(), settings=Mock(), enable_fallback=False)
                    with patch.object(cli, "build_context", return_value=ctx), \
                         patch.object(cli, "run_many", return_value=(results, failures)), \
                         patch.object(cli, "send_text") as notify, \
                         contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(cli.cmd_sync(self.args(dataset)), expected)
                    ctx.store.close.assert_called_once()
                    self.assertEqual(notify.call_count, bool(failures))

    def test_single_dataset_success_stays_zero(self):
        ctx = SimpleNamespace(store=Mock(), settings=Mock(), enable_fallback=False)
        with patch.object(cli, "build_context", return_value=ctx), \
             patch.object(cli, "run_dataset", return_value=(1, 1)), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_sync(self.args("daily")), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
