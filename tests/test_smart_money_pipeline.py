"""Quarterly report regressions; use SQLite and mocked fund requests only."""
import argparse
import contextlib
import io
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from app.analytics.smart_money import _quarter_completeness, report, report_html, smart_money_flow
from app.analytics.cli import add_analytics_subparsers
from app.sync.base import sync_fund_holdings


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE fund_holdings(fund_code TEXT, ts_code TEXT, end_date TEXT, hold_amount REAL);
            CREATE TABLE top10_holders(ts_code TEXT, end_date TEXT, holder_name TEXT, ann_date TEXT, hold_amount REAL, hold_ratio REAL);
            CREATE TABLE stock_basic(ts_code TEXT, name TEXT, industry TEXT);
            CREATE TABLE daily(ts_code TEXT, trade_date TEXT, close REAL);
            INSERT INTO stock_basic VALUES ('A', 'Stock A', 'Industry');
            INSERT INTO daily VALUES ('A', '20260701', 10);
            INSERT INTO top10_holders VALUES ('A', '20260331', '社保', '20260401', 10, 1);
            INSERT INTO top10_holders VALUES ('A', '20260630', '社保', '20260701', 20, 1);
            INSERT INTO top10_holders VALUES ('A', '20260630', '社保', '20260702', 30, 1);
        ''')
        self.conn.executemany('INSERT INTO fund_holdings VALUES (?, ?, ?, ?)',
                              [(str(i), 'A', '20260331', 100) for i in range(10)] +
                              [(str(i), 'A', '20260630', 100) for i in range(4)])

    def test_current_site_shell(self):
        html = report_html(self.conn, '20260630', '20260331')
        for marker in (
            '<title>聪明钱分析报告 · 20260630 · Stocks 研究站</title>',
            '<link rel="icon" type="image/svg+xml" href="/assets/favicon.svg">',
            '<link rel="icon" type="image/png" href="/assets/favicon.png">',
            '<a href="/">Stocks 研究站</a>',
            '<a href="/industries">产业地图</a>',
            '<a href="/articles">文章</a>',
            '<a href="/smart-money" class="active">聪明钱</a>',
            '<a href="/macro">宏观</a>',
            '<a href="/qoq">预增环比</a>',
            '<a href="/q3">三季报</a>',
            '<button id="theme-btn" class="btn-theme" aria-label="切换深浅主题"></button>',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)

    def test_coverage_warning_and_fallback(self):
        coverage = _quarter_completeness(self.conn, '20260630')
        self.assertEqual(coverage['fund_count'], 4)
        self.assertFalse(coverage['data_complete'])
        self.assertEqual(coverage['fallback_end'], '20260331')
        text = report(self.conn, '20260630', '20260331')
        self.assertTrue(text.startswith('⚠️ 数据不完整（覆盖不足50%），以下结论仅供参考'))
        self.assertIn('可改用数据较完整的报告期：20260331', text)
        self.assertIn('role="alert"', report_html(self.conn, '20260630'))
        self.conn.execute("INSERT INTO fund_holdings VALUES ('4', 'A', '20260630', 100)")
        self.assertTrue(_quarter_completeness(self.conn, '20260630')['data_complete'])
        self.assertNotIn('role="alert"', report_html(self.conn, '20260630'))

    def test_downgrade_and_latest_announcement(self):
        flow = smart_money_flow(self.conn, '20260930', '20251231')
        self.assertEqual(flow['used_prev'], '20260331')
        self.assertEqual(flow['industry']['amount'].sum(), 300)
        self.assertEqual(flow['changes'][0]['delta'], 20)
        with self.assertRaises(ValueError):
            smart_money_flow(self.conn, '20260630', '20260630')

    def test_cli_validation(self):
        parser = argparse.ArgumentParser()
        add_analytics_subparsers(parser.add_subparsers())
        with contextlib.redirect_stderr(io.StringIO()):
            for date in ['20260230', '20260531', '20261331', '2026630', '00000331']:
                with self.subTest(date=date), self.assertRaises(SystemExit):
                    parser.parse_args(['smart-money', '--end', date])
            args = parser.parse_args(['smart-money', '--end', '20260630', '--prev', '20260930'])
            with self.assertRaises(SystemExit):
                args.func(args)

    def test_sync_requested_quarter_and_failed_metadata(self):
        class Store:
            def query(_, sql, params=()):
                if 'fund_basic' in sql:
                    return [('0.OF', 'Fund 0'), ('4.OF', 'Fund 4'), ('5.OF', 'Fund 5')]
                return self.conn.execute(sql, params).fetchall()
        frame = pd.DataFrame([{'fund_code': '4', 'ts_code': 'A', 'end_date': '20260630', 'hold_amount': 10}])
        with patch('app.providers.eastmoney_fund.fetch_fund_holdings', side_effect=[frame, RuntimeError('offline')]) as fetch, \
             patch('app.sync.base.upsert', return_value=1), patch('app.sync.base.time.sleep'):
            result = sync_fund_holdings(SimpleNamespace(store=Store()), None, '', '20260630', None)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(tuple(result), (1, 1))
        self.assertEqual(result.total_funds, 3)
        self.assertEqual(result.fetched_funds, 1)
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(result.failed_codes, ['5.OF'])
        self.assertFalse(result.data_complete)


if __name__ == '__main__':
    unittest.main()
