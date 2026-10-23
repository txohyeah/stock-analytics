"""Synthetic state transitions and prefix invariance, without market fixtures."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'mainline_monitor', str(Path(__file__).resolve().parents[1] / 'scripts/mainline_monitor.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def days(values):
    return {'2026-01-{:02d}'.format(i + 1): v for i, v in enumerate(values)}


class MainlineTest(unittest.TestCase):
    def test_priority_and_events(self):
        signals, _ = m.replay(days([{'x': 18000, 'y': 19000, '并购重组': 20000}]))
        self.assertEqual([(s['board'], s['kind']) for s in signals], [('x', 'FAST'), ('y', 'FAST')])

    def test_fifth_session_survival(self):
        _, snapshots = m.replay(days([{'x': 18000}] + [{'x': 11000}] * 5))
        self.assertEqual(snapshots['2026-01-06'][0][8], 'ACTIVE')
        _, snapshots = m.replay(days([{'x': 18000}] + [{'x': 10000}] * 5))
        self.assertEqual(snapshots['2026-01-06'][0][8], 'REVOKED')

    def test_cooldown_counts_revocation_session(self):
        signals, _ = m.replay(days([{'x': 18000}] + [{'x': 10000}] * 5
                                  + [{'x': 18000}] * 11))
        self.assertEqual([s['date'] for s in signals if s['kind'] == 'FAST'],
                         ['2026-01-01', '2026-01-16'])

    def test_absence_pauses_survival(self):
        signals, snapshots = m.replay(days([{'x': 18000}] + [{}] * 4 + [{'x': 18000}]))
        # V2 退出规则: 缺席第 3 日(01-04) EXIT；01-06 回榜重新确认 → 两次 FAST
        self.assertEqual(len([s for s in signals if s['kind'] == 'FAST']), 2)
        self.assertEqual([r[2] for r in snapshots['2026-01-04'] if r[1] == 'x'], ['EXIT'])
        self.assertEqual(snapshots['2026-01-06'][0][8], 'ACTIVE')

    def test_peak_decline_stays_active_and_gap_resets(self):
        _, snapshots = m.replay(days([{'x': 18000}, {'x': 7000}]))
        self.assertEqual(snapshots['2026-01-02'][0][2], 'MAINLINE')
        self.assertEqual(snapshots['2026-01-02'][0][8], 'ACTIVE')
        # 在榜回落不退出；缺席 3 日即退出重置，回榜重新确认
        signals, _ = m.replay(days([{'x': 18000}] + [{}] * 5 + [{'x': 18000}]))
        self.assertEqual(len(signals), 2)
        signals, _ = m.replay(days([{'x': 18000}] + [{}] * 6 + [{'x': 18000}]))
        self.assertEqual(len(signals), 2)

    def test_frozen_ordered_signals(self):
        import json
        import sqlite3
        reference_spec = importlib.util.spec_from_file_location(
            'mainline_reference', str(Path(__file__).with_name('mainline_reference_impl.py')))
        reference = importlib.util.module_from_spec(reference_spec)
        reference_spec.loader.exec_module(reference)
        reference.BS = str(m.DB)
        with sqlite3.connect(str(m.DB)) as con:
            actual, _ = m.replay(m.load_days(con))
        self.assertEqual(m.EVENTS, reference.EVENT)
        self.assertEqual(actual, reference.run_reference())
        with open(str(m.DB.with_name('mainline_signals_reference.json')), encoding='utf-8') as f:
            self.assertEqual(actual, json.load(f)['signals'])

    def test_b_strict_peak_and_prefix(self):
        values = [{'x': 1000 + i * 100} for i in range(12)]
        signals, snapshots = m.replay(days(values))
        self.assertEqual(signals[-1]['kind'], 'B')
        for n in range(1, len(values)):
            small_signals, small_snapshots = m.replay(days(values[:n]))
            self.assertEqual(small_signals, [s for s in signals if s['date'] <= list(days(values))[n-1]])
            self.assertEqual(small_snapshots, {k: snapshots[k] for k in small_snapshots})
        values[-1] = {'x': 2000}
        self.assertFalse(any(s['kind'] == 'B' for s in m.replay(days(values))[0]))


    def test_exit_after_three_missed_sessions(self):
        vals = [{'x': 18000}, {}, {}, {}, {'x': 5000}]
        signals, snaps = m.replay(days(vals))
        exit_rows = [r for rs in snaps.values() for r in rs if r[2] == 'EXIT']
        self.assertEqual(len(exit_rows), 1)
        self.assertEqual(exit_rows[0][0], '2026-01-04')      # 第 3 个缺席日
        self.assertEqual(exit_rows[0][3], 'FAST')            # 原信号类型
        later_active = [r for d, rs in snaps.items() for r in rs
                        if r[1] == 'x' and r[2] == 'MAINLINE' and r[8] == 'ACTIVE'
                        and d > '2026-01-04']
        self.assertFalse(later_active)                       # 退出后不得再报 ACTIVE

    def test_return_within_two_misses_stays_mainline(self):
        vals = [{'x': 18000}, {}, {}, {'x': 5000}]
        signals, snaps = m.replay(days(vals))
        exit_rows = [r for rs in snaps.values() for r in rs if r[2] == 'EXIT']
        self.assertFalse(exit_rows)                          # 缺 2 日即回榜，不退出
        active = [r for d, rs in snaps.items() for r in rs
                  if r[1] == 'x' and r[2] == 'MAINLINE' and r[8] == 'ACTIVE' and d == '2026-01-04']
        self.assertTrue(active)                              # 回榜日仍为主线


if __name__ == '__main__':
    unittest.main()
