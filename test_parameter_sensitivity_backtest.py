#!/usr/bin/env python3
"""Unit tests for parameter_sensitivity_backtest.py.

Uses a TEMPORARY SQLite file, never the real data/app.db -- same precedent
as test_db_daily_snapshot.py/test_db_categories.py.

Run: python3 -m unittest test_parameter_sensitivity_backtest -v
"""

import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import config
import parameter_sensitivity_backtest as psb


class TestRecomputeTradeSize(unittest.TestCase):
    """Pure function -- no DB needed. Mirrors bot.compute_trade_size_usd()'s
    tail exactly; verified against 10 real rows from data/app.db before this
    script was written (6/6 positive-kelly rows reproduced the recorded
    trade_size_usd to floating-point precision) -- these cases pin that
    behavior down as a regression test.
    """

    def test_reproduces_real_recorded_trade_size(self):
        # Real row pulled from data/app.db: kelly_fraction=0.4972655533777962,
        # multiplier=0.5 (current KELLY_FRACTION_MULTIPLIER), MIN=3.0/MAX=10.0
        # -> recorded trade_size_usd=4.740429436822287.
        size = psb.recompute_trade_size(0.4972655533777962, 0.5, 3.0, 10.0)
        self.assertAlmostEqual(size, 4.740429436822287, places=6)

    def test_negative_kelly_fraction_returns_zero(self):
        self.assertEqual(psb.recompute_trade_size(-0.2, 0.5, 3.0, 10.0), 0.0)

    def test_zero_kelly_fraction_returns_zero(self):
        self.assertEqual(psb.recompute_trade_size(0.0, 0.5, 3.0, 10.0), 0.0)

    def test_large_kelly_fraction_clamps_to_max(self):
        # kelly_fraction * multiplier > 1.0 clamps to the full range's top.
        size = psb.recompute_trade_size(5.0, 0.5, 3.0, 10.0)
        self.assertAlmostEqual(size, 10.0, places=6)

    def test_shifted_multiplier_changes_size_proportionally_within_range(self):
        low = psb.recompute_trade_size(0.5, 0.4, 3.0, 10.0)
        high = psb.recompute_trade_size(0.5, 0.6, 3.0, 10.0)
        self.assertLess(low, high)


class TestComputeCapitalMultiplier(unittest.TestCase):
    """Python port of scoreWallets.ts's computeCapitalMultiplier() -- pure
    function, no DB needed."""

    def test_zero_sharpe_gives_multiplier_of_one(self):
        self.assertAlmostEqual(psb.compute_capital_multiplier(0.0, 0.35, 2.0), 1.0)

    def test_sharpe_at_saturation_gives_full_cap(self):
        self.assertAlmostEqual(psb.compute_capital_multiplier(0.35, 0.35, 2.0), 2.0)

    def test_sharpe_above_saturation_clamps_at_cap_not_beyond(self):
        self.assertAlmostEqual(psb.compute_capital_multiplier(0.70, 0.35, 2.0), 2.0)

    def test_negative_sharpe_clamps_to_multiplier_of_one_never_below(self):
        self.assertAlmostEqual(psb.compute_capital_multiplier(-0.5, 0.35, 2.0), 1.0)

    def test_lower_saturation_reaches_cap_sooner(self):
        # Same sharpe_proxy, tighter saturation -> a bigger multiplier.
        loose = psb.compute_capital_multiplier(0.20, 0.35, 2.0)
        tight = psb.compute_capital_multiplier(0.20, 0.28, 2.0)
        self.assertGreater(tight, loose)


class _TempDbTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.tmp_path)
        conn.execute(
            "CREATE TABLE outcome_review (id TEXT PRIMARY KEY, market_slug TEXT, "
            "outcome TEXT, wallet_address TEXT, pnl_usd REAL, "
            "contributing_score_factors_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE wallet_profile (wallet_address TEXT PRIMARY KEY, "
            "win_rate REAL, trade_count_all_time INTEGER, score_breakdown_json TEXT)"
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.remove(self.tmp_path)

    def _insert_outcome_review(self, row_id, pnl_usd, score_breakdown, rule_set_version=4):
        conn = sqlite3.connect(self.tmp_path)
        payload = None
        if score_breakdown is not None:
            payload = json.dumps({"score_breakdown": score_breakdown, "rule_set_version": rule_set_version})
        conn.execute(
            "INSERT INTO outcome_review (id, market_slug, outcome, wallet_address, pnl_usd, "
            "contributing_score_factors_json) VALUES (?, 'm', 'Yes', '0xabc', ?, ?)",
            (row_id, pnl_usd, payload),
        )
        conn.commit()
        conn.close()


class TestFetchKellySensitiveTrades(_TempDbTestCase):
    """Confirms the three-way bucketing that Part 1a's real backtest
    depends on -- getting this wrong would silently corrupt the backtest by
    mixing trades sized under different formula versions.
    """

    def test_base_tier_excluded_as_unaffected(self):
        self._insert_outcome_review("t1", 5.0, {
            "sizing_tier": "base", "kelly_fraction": None, "trade_size_usd": 5.0,
        })
        replayable, base_tier, stale = psb.fetch_kelly_sensitive_trades()
        self.assertEqual(len(replayable), 0)
        self.assertEqual(base_tier, [5.0])
        self.assertEqual(stale, [])

    def test_negative_kelly_excluded_as_stale_floor_policy(self):
        self._insert_outcome_review("t1", -3.0, {
            "sizing_tier": "composite", "kelly_fraction": -0.2, "trade_size_usd": 3.0,
        })
        replayable, base_tier, stale = psb.fetch_kelly_sensitive_trades()
        self.assertEqual(len(replayable), 0)
        self.assertEqual(base_tier, [])
        self.assertEqual(stale, [-3.0])

    def test_positive_kelly_included_as_replayable(self):
        self._insert_outcome_review("t1", 4.74, {
            "sizing_tier": "composite", "kelly_fraction": 0.4972655533777962, "trade_size_usd": 4.740429436822287,
        })
        replayable, base_tier, stale = psb.fetch_kelly_sensitive_trades()
        self.assertEqual(len(replayable), 1)
        self.assertEqual(replayable[0]["kelly_fraction"], 0.4972655533777962)
        self.assertEqual(base_tier, [])
        self.assertEqual(stale, [])

    def test_rows_without_score_breakdown_json_are_never_returned(self):
        # Simulates a pre-Rule-22 (2026-07-24) closed trade, which
        # contributing_score_factors_json is NULL for -- the query itself
        # filters these out (WHERE ... IS NOT NULL), verified here.
        self._insert_outcome_review("t1", 1.0, None)
        replayable, base_tier, stale = psb.fetch_kelly_sensitive_trades()
        self.assertEqual((replayable, base_tier, stale), ([], [], []))

    def test_mixed_batch_buckets_correctly(self):
        self._insert_outcome_review("t1", 5.0, {"sizing_tier": "base", "kelly_fraction": None, "trade_size_usd": 5.0})
        self._insert_outcome_review("t2", -3.0, {"sizing_tier": "composite", "kelly_fraction": -0.5, "trade_size_usd": 3.0})
        self._insert_outcome_review("t3", 4.74, {"sizing_tier": "composite", "kelly_fraction": 0.497, "trade_size_usd": 4.74})
        replayable, base_tier, stale = psb.fetch_kelly_sensitive_trades()
        self.assertEqual(len(replayable), 1)
        self.assertEqual(len(base_tier), 1)
        self.assertEqual(len(stale), 1)


if __name__ == "__main__":
    unittest.main()
