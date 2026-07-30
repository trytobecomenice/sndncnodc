#!/usr/bin/env python3
"""Unit tests for bot.py's pure pre-trade risk math (check_slippage_ceiling,
compute_shortfall). Same stdlib-unittest pattern as test_risk_manager.py.

Run: python3 -m unittest test_bot_risk_checks -v
"""

import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import bot
import config


class TestMarkTradeSeen(unittest.TestCase):
    """_mark_trade_seen() (2026-07-31) — per-wallet dedup, replacing a single
    global deque(maxlen=2000). Confirmed live: the old design let one busy
    wallet's volume evict a quiet wallet's older trade_ids, which then
    resurfaced as "new" copies of month-old trades on the next bot.py
    restart. seen_set must stay in sync with each wallet's own bounded
    deque, not just grow forever or share one global bound."""

    def setUp(self):
        self._saved_cap = config.SEEN_TRADE_IDS_PER_WALLET_CAP
        config.SEEN_TRADE_IDS_PER_WALLET_CAP = 3

    def tearDown(self):
        config.SEEN_TRADE_IDS_PER_WALLET_CAP = self._saved_cap

    def test_a_busy_wallet_does_not_evict_a_quiet_wallets_entry_from_seen_set(self):
        seen_by_wallet, seen_set = {}, set()
        bot._mark_trade_seen(seen_by_wallet, seen_set, "0xQuiet", "quiet-trade-1")
        for i in range(10):
            bot._mark_trade_seen(seen_by_wallet, seen_set, "0xBusy", f"busy-{i}")
        self.assertIn("quiet-trade-1", seen_set)

    def test_own_wallets_oldest_entry_evicted_once_its_own_cap_is_exceeded(self):
        seen_by_wallet, seen_set = {}, set()
        for i in range(4):  # cap is 3 -- the 4th push evicts the 1st
            bot._mark_trade_seen(seen_by_wallet, seen_set, "0xTrader", f"t{i}")
        self.assertNotIn("t0", seen_set)
        self.assertIn("t1", seen_set)
        self.assertIn("t2", seen_set)
        self.assertIn("t3", seen_set)

    def test_wallet_address_is_case_normalized(self):
        seen_by_wallet, seen_set = {}, set()
        bot._mark_trade_seen(seen_by_wallet, seen_set, "0xABC", "t1")
        bot._mark_trade_seen(seen_by_wallet, seen_set, "0xabc", "t2")
        # Same wallet under different casing must share one bucket/cap, not
        # two separate ones that could each independently reach the cap.
        self.assertEqual(len(seen_by_wallet), 1)

    def test_none_wallet_address_falls_into_the_shared_unknown_bucket(self):
        seen_by_wallet, seen_set = {}, set()
        bot._mark_trade_seen(seen_by_wallet, seen_set, None, "legacy-1")
        self.assertIn(bot._UNKNOWN_WALLET_KEY, seen_by_wallet)
        self.assertIn("legacy-1", seen_set)


class TestSlippageCeiling(unittest.TestCase):
    def setUp(self):
        self._saved_tolerance = config.SLIPPAGE_TOLERANCE
        config.SLIPPAGE_TOLERANCE = 0.05

    def tearDown(self):
        config.SLIPPAGE_TOLERANCE = self._saved_tolerance

    def test_buy_allowed_when_price_unchanged(self):
        ok, reason = bot.check_slippage_ceiling(0.40, 0.40, "BUY")
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_buy_allowed_exactly_at_the_ceiling(self):
        ok, _ = bot.check_slippage_ceiling(0.40, 0.42, "BUY")  # +5.0% exactly
        self.assertTrue(ok)

    def test_buy_aborted_just_past_the_ceiling(self):
        ok, reason = bot.check_slippage_ceiling(0.40, 0.4201, "BUY")  # +5.025%
        self.assertFalse(ok)
        self.assertIn("aborting", reason)
        self.assertIn("5%", reason)

    def test_buy_allowed_when_price_moved_favorably_down(self):
        # A BUY where the price DROPPED since the source trade is a better
        # deal, never a slippage concern.
        ok, _ = bot.check_slippage_ceiling(0.40, 0.30, "BUY")
        self.assertTrue(ok)

    def test_sell_aborted_when_price_drops_past_the_ceiling(self):
        # Direction-correctness check, even though SELL is never called in
        # practice (see the function's docstring) — the math itself must
        # still be right if it's ever deliberately used.
        ok, reason = bot.check_slippage_ceiling(0.40, 0.37, "SELL")  # -7.5%
        self.assertFalse(ok)
        self.assertIn("aborting", reason)

    def test_sell_allowed_when_price_rises(self):
        ok, _ = bot.check_slippage_ceiling(0.40, 0.50, "SELL")
        self.assertTrue(ok)

    def test_respects_a_tighter_configured_tolerance(self):
        config.SLIPPAGE_TOLERANCE = 0.01
        ok, _ = bot.check_slippage_ceiling(0.40, 0.405, "BUY")  # +1.25% > 1%
        self.assertFalse(ok)


class TestComputeEntrySlippageCeilingPct(unittest.TestCase):
    """Entry-side (BUY) FAK ceiling, 2026-07-26 — clamp(protection_fraction
    * live_edge_pct, floor=SLIPPAGE_TOLERANCE, cap=ENTRY_SLIPPAGE_CEILING_CAP_PCT).
    Mirrors compute_slippage_floor_price's construction on the exit side."""

    def setUp(self):
        self._saved = (config.SLIPPAGE_TOLERANCE, config.ENTRY_SLIPPAGE_CEILING_CAP_PCT,
                        config.SLIPPAGE_PROTECTION_FRACTION, config.ORDER_PEG_FALLBACK_EDGE_PCT)
        config.SLIPPAGE_TOLERANCE = 0.05
        config.ENTRY_SLIPPAGE_CEILING_CAP_PCT = 0.30
        config.SLIPPAGE_PROTECTION_FRACTION = 0.30
        config.ORDER_PEG_FALLBACK_EDGE_PCT = 0.05

    def tearDown(self):
        (config.SLIPPAGE_TOLERANCE, config.ENTRY_SLIPPAGE_CEILING_CAP_PCT,
         config.SLIPPAGE_PROTECTION_FRACTION, config.ORDER_PEG_FALLBACK_EDGE_PCT) = self._saved

    def test_mid_range_edge_lands_between_floor_and_cap(self):
        # live edge 40% -> raw = 0.30*0.40 = 12%, comfortably between 5%/30%.
        result = bot.compute_entry_slippage_ceiling_pct(live_edge_pct=0.40)
        self.assertAlmostEqual(result, 0.12)

    def test_weak_edge_clamps_to_the_floor(self):
        # live edge 2% -> raw = 0.6%, below the 5% floor.
        result = bot.compute_entry_slippage_ceiling_pct(live_edge_pct=0.02)
        self.assertAlmostEqual(result, 0.05)

    def test_strong_edge_clamps_to_the_cap(self):
        # live edge 500% -> raw = 150%, way above the 30% cap.
        result = bot.compute_entry_slippage_ceiling_pct(live_edge_pct=5.0)
        self.assertAlmostEqual(result, 0.30)

    def test_negative_edge_floors_at_zero_before_scaling_not_below_floor(self):
        result = bot.compute_entry_slippage_ceiling_pct(live_edge_pct=-0.50)
        self.assertAlmostEqual(result, 0.05)  # never goes below the static floor

    def test_none_edge_falls_back_to_configured_fallback_edge(self):
        # No live_edge_pct -> ORDER_PEG_FALLBACK_EDGE_PCT (5%) -> raw = 0.30*0.05 = 1.5%,
        # clamped up to the 5% floor.
        result = bot.compute_entry_slippage_ceiling_pct(live_edge_pct=None)
        self.assertAlmostEqual(result, 0.05)

    def test_custom_protection_fraction_overrides_config(self):
        result = bot.compute_entry_slippage_ceiling_pct(live_edge_pct=0.40, protection_fraction=0.50)
        self.assertAlmostEqual(result, 0.20)  # 0.50*0.40


class TestComputeShrunkWinRate(unittest.TestCase):
    """compute_shrunk_win_rate() — empirical-Bayes shrinkage of an observed
    win rate toward the market's own price, weighted by sample size (Rule
    25, 2026-07-24)."""

    def setUp(self):
        self._saved = config.KELLY_SHRINKAGE_PSEUDO_COUNT
        config.KELLY_SHRINKAGE_PSEUDO_COUNT = 25

    def tearDown(self):
        config.KELLY_SHRINKAGE_PSEUDO_COUNT = self._saved

    def test_zero_track_record_shrinks_all_the_way_to_market_price(self):
        # n=0 -> weight on observed win_rate is 0/(0+25)=0%, fully the prior.
        self.assertAlmostEqual(bot.compute_shrunk_win_rate(0.99, 0, 0.42), 0.42)

    def test_identical_win_rate_and_price_stays_put_regardless_of_n(self):
        self.assertAlmostEqual(bot.compute_shrunk_win_rate(0.5, 5, 0.5), 0.5)
        self.assertAlmostEqual(bot.compute_shrunk_win_rate(0.5, 500, 0.5), 0.5)

    def test_small_sample_stays_close_to_market_price(self):
        # n=5 (this codebase's own hard minimum sample) -> weight 5/30 ≈ 16.7%
        # on the observed win rate. A wallet showing 100% win rate over 5
        # trades should land far closer to the market's own price than to 1.0.
        shrunk = bot.compute_shrunk_win_rate(1.0, 5, 0.5)
        self.assertAlmostEqual(shrunk, (5 * 1.0 + 25 * 0.5) / 30)  # = 0.5833...
        self.assertLess(shrunk, 0.6)

    def test_large_sample_trusts_the_observed_win_rate_closely(self):
        # n=200 (Polyburg's cited "bankable" sample) -> weight 200/225 ≈ 88.9%.
        shrunk = bot.compute_shrunk_win_rate(0.65, 200, 0.5)
        self.assertAlmostEqual(shrunk, (200 * 0.65 + 25 * 0.5) / 225)
        self.assertGreater(shrunk, 0.62)  # much closer to 0.65 than to 0.5

    def test_negative_trade_count_degrades_to_zero_not_a_crash(self):
        self.assertAlmostEqual(bot.compute_shrunk_win_rate(0.9, -3, 0.5), 0.5)


class TestComputeKellyFraction(unittest.TestCase):
    """compute_kelly_fraction() — Kelly criterion for a binary payout at a
    given market price (Rule 25, 2026-07-24)."""

    def test_zero_edge_when_win_rate_equals_market_price(self):
        # This is the key self-consistency property compute_shrunk_win_rate's
        # n=0 case relies on: no track record -> shrunk win rate == price ->
        # Kelly correctly says "no edge, no bet" without a special case.
        self.assertAlmostEqual(bot.compute_kelly_fraction(0.5, 0.5), 0.0)
        self.assertAlmostEqual(bot.compute_kelly_fraction(0.2, 0.2), 0.0)
        self.assertAlmostEqual(bot.compute_kelly_fraction(0.8, 0.8), 0.0)

    def test_positive_edge_when_win_rate_exceeds_market_price(self):
        # price 0.5 -> b=1.0 -> f* = win_rate - (1-win_rate) = 2*win_rate-1
        self.assertAlmostEqual(bot.compute_kelly_fraction(0.7, 0.5), 0.4)

    def test_negative_edge_when_win_rate_is_below_market_price(self):
        self.assertAlmostEqual(bot.compute_kelly_fraction(0.3, 0.5), -0.4)

    def test_degenerate_price_at_or_below_zero_returns_zero_not_a_crash(self):
        self.assertEqual(bot.compute_kelly_fraction(0.9, 0.0), 0.0)
        self.assertEqual(bot.compute_kelly_fraction(0.9, -0.1), 0.0)

    def test_degenerate_price_at_or_above_one_returns_zero_not_a_crash(self):
        self.assertEqual(bot.compute_kelly_fraction(0.9, 1.0), 0.0)
        self.assertEqual(bot.compute_kelly_fraction(0.9, 1.1), 0.0)


class TestComputeTradeSizeUsd(unittest.TestCase):
    """compute_trade_size_usd() — half-Kelly sizing (2026-07-24, replacing
    the 2026-07-22 linear confidence ramp — see config.py's sizing comment
    and Rule 25 for the full formula)."""

    def setUp(self):
        self._saved = (
            config.BASE_TRADE_USD, config.MIN_TRADE_USD, config.MAX_TRADE_USD,
            config.KELLY_SHRINKAGE_PSEUDO_COUNT, config.KELLY_FRACTION_MULTIPLIER,
        )
        config.BASE_TRADE_USD, config.MIN_TRADE_USD, config.MAX_TRADE_USD = 5.0, 3.0, 10.0
        config.KELLY_SHRINKAGE_PSEUDO_COUNT = 25
        config.KELLY_FRACTION_MULTIPLIER = 0.5

    def tearDown(self):
        (config.BASE_TRADE_USD, config.MIN_TRADE_USD, config.MAX_TRADE_USD,
         config.KELLY_SHRINKAGE_PSEUDO_COUNT, config.KELLY_FRACTION_MULTIPLIER) = self._saved

    @staticmethod
    def _entry(composite=None, composite_win_rate=None, composite_trade_count=None, categories=None,
               capital_multiplier=None):
        """categories is {category: (win_rate_or_None, trade_count)}
        shorthand — wraps each into the real {"score":..., "pnl_t_stat":...,
        "win_rate":..., "trade_count":...} shape
        db.get_wallet_composite_scores() actually returns."""
        wrapped = {
            cat: {"score": None, "pnl_t_stat": None, "win_rate": wr, "trade_count": tc}
            for cat, (wr, tc) in (categories or {}).items()
        }
        return {
            "composite": composite,
            "composite_win_rate": composite_win_rate,
            "composite_trade_count": composite_trade_count,
            "capital_multiplier": capital_multiplier,
            "categories": wrapped,
        }

    def test_no_wallet_row_at_all_gets_the_base_amount(self):
        self.assertEqual(bot.compute_trade_size_usd(None, 0.5), 5.0)

    def test_unscored_wallet_gets_the_base_amount(self):
        self.assertEqual(bot.compute_trade_size_usd(self._entry(), 0.5), 5.0)

    def test_zero_track_record_composite_gets_skipped_not_a_guess(self):
        # composite_trade_count=0 -> shrunk win rate == price exactly ->
        # zero Kelly edge -> skipped (0.0), regardless of the
        # (untrustworthy, zero-sample) observed win_rate. "No track
        # record, no assumed edge" (Rule 25) means no trade, not a floor
        # guess (2026-07-28).
        entry = self._entry(composite=0.5, composite_win_rate=0.95, composite_trade_count=0)
        self.assertEqual(bot.compute_trade_size_usd(entry, 0.5), 0.0)

    def test_win_rate_equal_to_price_gets_skipped(self):
        entry = self._entry(composite_win_rate=0.5, composite_trade_count=100)
        self.assertEqual(bot.compute_trade_size_usd(entry, 0.5), 0.0)

    def test_win_rate_above_price_with_deep_sample_sizes_above_the_floor(self):
        # n=1000 -> barely shrunk -> a real, sizeable edge -> above the floor.
        # (Not "above BASE" — see test_half_kelly_fraction_can_never_reach_the_full_ceiling
        # below for why half-Kelly's practical ceiling sits well under $10.)
        entry = self._entry(composite_win_rate=0.7, composite_trade_count=1000)
        trade_usd = bot.compute_trade_size_usd(entry, 0.5)
        self.assertGreater(trade_usd, 3.0)
        self.assertLessEqual(trade_usd, 10.0)

    def test_win_rate_below_price_gets_skipped_not_a_negative_size(self):
        entry = self._entry(composite_win_rate=0.2, composite_trade_count=1000)
        self.assertEqual(bot.compute_trade_size_usd(entry, 0.5), 0.0)

    def test_stronger_edge_sizes_larger_than_a_weaker_edge_monotonic(self):
        weak = self._entry(composite_win_rate=0.55, composite_trade_count=1000)
        strong = self._entry(composite_win_rate=0.9, composite_trade_count=1000)
        self.assertLess(
            bot.compute_trade_size_usd(weak, 0.5),
            bot.compute_trade_size_usd(strong, 0.5),
        )

    def test_half_kelly_fraction_can_never_reach_the_full_ceiling(self):
        # A real, worth-documenting mathematical property (see Rule 25): raw
        # Kelly f* = p - (1-p)/b is bounded above by p <= 1, so HALF-Kelly is
        # bounded above by 0.5 — it can never clamp to the full [0,1] range
        # this maps into MIN..MAX_TRADE_USD, even for a near-certain edge at
        # a huge sample size. The practical ceiling under default settings is
        # MIN + 0.5*(MAX-MIN) = 3 + 0.5*7 = 6.5, not 10 — a deliberate
        # consequence of always halving, not a bug.
        entry = self._entry(composite_win_rate=0.99, composite_trade_count=100000)
        trade_usd = bot.compute_trade_size_usd(entry, 0.05)
        self.assertLess(trade_usd, 6.5)
        self.assertGreater(trade_usd, 6.0)  # still a strong, near-the-practical-ceiling size

    def test_category_tier_used_over_composite_when_both_present(self):
        entry = self._entry(
            composite_win_rate=0.5, composite_trade_count=1000,  # zero edge if used
            categories={"crypto": (0.9, 1000)},  # strong edge if used instead
        )
        trade_usd = bot.compute_trade_size_usd(entry, 0.5, category="crypto")
        self.assertGreater(trade_usd, 3.0)  # category tier won, not the zero-edge composite

    def test_falls_back_to_composite_when_category_has_no_win_rate_yet(self):
        entry = self._entry(
            composite_win_rate=0.5, composite_trade_count=1000,  # zero edge
            categories={"crypto": (0.9, 1000)},
        )
        # "sports" isn't in categories at all -> falls back to composite (zero edge -> skipped)
        self.assertEqual(bot.compute_trade_size_usd(entry, 0.5, category="sports"), 0.0)

    def test_category_present_but_win_rate_none_falls_back_to_composite(self):
        # e.g. a category that WAS looked up but had too few samples to score
        entry = self._entry(
            composite_win_rate=0.9, composite_trade_count=1000,
            categories={"crypto": (None, None)},
        )
        trade_usd = bot.compute_trade_size_usd(entry, 0.5, category="crypto")
        self.assertGreater(trade_usd, 3.0)  # used the composite's real edge, not stuck at floor

    def test_category_win_rate_used_even_when_composite_is_none(self):
        entry = self._entry(composite=None, categories={"politics": (0.9, 1000)})
        trade_usd = bot.compute_trade_size_usd(entry, 0.5, category="politics")
        self.assertGreater(trade_usd, 3.0)

    def test_neither_composite_nor_category_gets_base_amount(self):
        entry = self._entry(composite=None, categories={})
        self.assertEqual(bot.compute_trade_size_usd(entry, 0.5, category="crypto"), 5.0)

    def test_missing_capital_multiplier_behaves_identically_to_1_0(self):
        # No capital_multiplier key at all (e.g. a wallet scored before v7)
        # must size EXACTLY the same as an explicit 1.0 — never treated as
        # None/0 collapsing the range to nothing.
        entry = self._entry(composite_win_rate=0.7, composite_trade_count=1000)
        without_key = bot.compute_trade_size_usd(entry, 0.5)
        entry["capital_multiplier"] = 1.0
        with_explicit_one = bot.compute_trade_size_usd(entry, 0.5)
        self.assertAlmostEqual(without_key, with_explicit_one, places=6)

    def test_capital_multiplier_stretches_the_sizing_range_proportionally(self):
        entry = self._entry(composite_win_rate=0.7, composite_trade_count=1000, capital_multiplier=2.0)
        trade_usd = bot.compute_trade_size_usd(entry, 0.5)
        # Same shape as test_half_kelly_fraction's floor/ceiling checks,
        # just against the DOUBLED range (config.MIN/MAX_TRADE_USD are 3.0/10.0
        # in setUp, so the effective range is 6.0-20.0).
        self.assertGreater(trade_usd, 6.0)
        self.assertLessEqual(trade_usd, 20.0)

    def test_capital_multiplier_never_inflates_the_no_evidence_base_amount(self):
        # A capital_multiplier present on an otherwise-unscored entry must
        # NOT inflate config.BASE_TRADE_USD -- the multiplier rewards
        # proven edge, it must never apply when there's no win-rate
        # evidence to size against at all.
        entry = self._entry(capital_multiplier=2.0)  # no win_rate anywhere
        self.assertEqual(bot.compute_trade_size_usd(entry, 0.5), 5.0)

    def test_capital_multiplier_does_not_rescue_a_non_positive_kelly_edge(self):
        # A capital multiplier rewards proven edge; it must never turn a
        # zero/negative Kelly edge into a trade (2026-07-28) — skipped
        # (0.0) regardless of how large the multiplier is.
        entry = self._entry(composite_win_rate=0.5, composite_trade_count=100, capital_multiplier=2.0)
        self.assertEqual(bot.compute_trade_size_usd(entry, 0.5), 0.0)


class TestShouldSkipCategory(unittest.TestCase):
    """should_skip_category() — hard-skip on STATISTICALLY SIGNIFICANT
    evidence of harm (2026-07-23), a stricter/separate decision from
    compute_trade_size_usd()'s floor-sizing. Not an arbitrary score cutoff
    — a one-tailed t-test critical value (see config.CATEGORY_SKIP_Z_CRITICAL's
    own comment)."""

    def setUp(self):
        self._saved = config.CATEGORY_SKIP_Z_CRITICAL
        config.CATEGORY_SKIP_Z_CRITICAL = 1.645

    def tearDown(self):
        config.CATEGORY_SKIP_Z_CRITICAL = self._saved

    @staticmethod
    def _entry(categories):
        return {"composite": 0.5, "categories": categories}

    def test_skips_when_t_stat_is_significantly_negative(self):
        entry = self._entry({"crypto": {"score": 0.1, "pnl_t_stat": -3.2}})
        self.assertTrue(bot.should_skip_category(entry, "crypto"))

    def test_does_not_skip_when_t_stat_is_not_significant_even_with_a_low_score(self):
        # e.g. a small, noisy sample that happens to look bad but isn't
        # statistically distinguishable from zero — should still just be
        # sized down (compute_trade_size_usd's floor), not hard-skipped.
        entry = self._entry({"crypto": {"score": 0.05, "pnl_t_stat": -0.8}})
        self.assertFalse(bot.should_skip_category(entry, "crypto"))

    def test_does_not_skip_a_significantly_positive_category(self):
        entry = self._entry({"crypto": {"score": 0.95, "pnl_t_stat": 4.1}})
        self.assertFalse(bot.should_skip_category(entry, "crypto"))

    def test_exactly_at_the_critical_value_skips(self):
        entry = self._entry({"crypto": {"score": 0.1, "pnl_t_stat": -1.645}})
        self.assertTrue(bot.should_skip_category(entry, "crypto"))

    def test_no_pnl_t_stat_available_does_not_skip(self):
        entry = self._entry({"crypto": {"score": 0.1, "pnl_t_stat": None}})
        self.assertFalse(bot.should_skip_category(entry, "crypto"))

    def test_category_not_present_at_all_does_not_skip(self):
        entry = self._entry({"politics": {"score": 0.1, "pnl_t_stat": -5.0}})
        self.assertFalse(bot.should_skip_category(entry, "crypto"))

    def test_no_category_given_does_not_skip(self):
        entry = self._entry({"crypto": {"score": 0.1, "pnl_t_stat": -5.0}})
        self.assertFalse(bot.should_skip_category(entry, None))

    def test_no_wallet_score_entry_does_not_skip(self):
        self.assertFalse(bot.should_skip_category(None, "crypto"))


class TestComputeWalletEvTStatistic(unittest.TestCase):
    """Pure one-sample t-statistic, 2026-07-26 (Rule 35) — the same
    statistical test should_skip_category() already uses, computed live
    here over a wallet's own recent real-trade returns."""

    def test_clearly_negative_returns_give_a_strongly_negative_t_stat(self):
        # All losses, some variance -- should be significantly negative.
        returns = [-0.5, -0.6, -0.4, -0.55, -0.45, -0.5, -0.6, -0.4, -0.5, -0.55]
        t = bot.compute_wallet_ev_t_statistic(returns)
        self.assertLess(t, -1.645)

    def test_clearly_positive_returns_give_a_positive_t_stat(self):
        returns = [0.5, 0.6, 0.4, 0.55, 0.45, 0.5, 0.6, 0.4, 0.5, 0.55]
        t = bot.compute_wallet_ev_t_statistic(returns)
        self.assertGreater(t, 0)

    def test_high_win_rate_but_negative_ev_is_still_flagged(self):
        # geo-anon-3's real shape, 2026-07-26 audit: 10 small wins wiped
        # out by 2 total losses -- mean is negative even though most
        # individual trades were wins. Confirms the whole point of an
        # EV-based test over a win-rate-based one.
        returns = [0.12, 0.09, 0.07, 0.10, 0.08, -1.0, 0.11, 0.09, -1.0, 0.10]
        t = bot.compute_wallet_ev_t_statistic(returns)
        self.assertLess(t, 0)

    def test_fewer_than_two_samples_returns_none(self):
        self.assertIsNone(bot.compute_wallet_ev_t_statistic([]))
        self.assertIsNone(bot.compute_wallet_ev_t_statistic([0.5]))

    def test_zero_variance_returns_none(self):
        # Every return identical -- stdev is 0, t-stat undefined.
        self.assertIsNone(bot.compute_wallet_ev_t_statistic([0.1, 0.1, 0.1]))


class TestCheckCircuitBreakerEvBased(unittest.TestCase):
    """check_circuit_breaker() rewrite, 2026-07-26 (Rule 35) — replaces the
    consecutive-loss-streak/win-rate-floor design with a t-test on real
    (non-dust) returns. Motivated by, and confirmed to fix, a real
    live failure mode: a short losing streak false-muting a genuinely
    good wallet (strict-7, crypto-specialist-1). Does NOT fix the
    opposite failure mode (geo-anon-3: high win rate, negative EV from a
    rare catastrophic loss) -- verified against its real 12-trade history,
    t=-0.92, nowhere near significant. See
    test_a_single_large_outlier_among_small_wins_is_not_flagged_honest_limitation
    below -- that pattern still needs manual review, not automatic muting."""

    def setUp(self):
        self._saved_samples = config.MUTE_EV_MIN_SAMPLES
        self._saved_dust = config.MUTE_MIN_TRADE_COST_USD
        self._saved_z = config.CATEGORY_SKIP_Z_CRITICAL
        config.MUTE_EV_MIN_SAMPLES = 5
        config.MUTE_MIN_TRADE_COST_USD = 1.0
        config.CATEGORY_SKIP_Z_CRITICAL = 1.645

    def tearDown(self):
        config.MUTE_EV_MIN_SAMPLES = self._saved_samples
        config.MUTE_MIN_TRADE_COST_USD = self._saved_dust
        config.CATEGORY_SKIP_Z_CRITICAL = self._saved_z

    def test_dust_trade_is_ignored_entirely(self):
        trader_performance, muted_traders = {}, {}
        # cost_basis $0.01 is well below the $1 dust floor -- a real loss
        # in isolation, but must not enter tracking at all.
        bot.check_circuit_breaker("0xTrader", "nick", -0.005, 0.01, trader_performance, muted_traders)
        self.assertNotIn("0xtrader", trader_performance)
        self.assertNotIn("0xtrader", muted_traders)

    def test_a_few_bad_trades_within_a_good_wallet_does_not_mute(self):
        # 3 losses then wins, small sample -- must NOT behave like the old
        # 3-in-a-row streak trigger.
        trader_performance, muted_traders = {}, {}
        for pnl in (-5.0, -5.0, -5.0, 10.0, 10.0):
            bot.check_circuit_breaker("0xTrader", "nick", pnl, 10.0, trader_performance, muted_traders)
        self.assertNotIn("0xtrader", muted_traders)

    def test_short_losing_streak_inside_a_strong_history_does_not_mute(self):
        # strict-7's real shape: mostly wins, one rough patch. Confirms the
        # false-positive the old streak trigger produced is fixed.
        trader_performance, muted_traders = {}, {}
        for pnl in (5.0, 5.0, 5.0, 5.0, 5.0, -3.0, -3.0, -3.0, 5.0, 5.0):
            bot.check_circuit_breaker("0xTrader", "nick", pnl, 10.0, trader_performance, muted_traders)
        self.assertNotIn("0xtrader", muted_traders)

    def test_statistically_significant_negative_edge_mutes(self):
        trader_performance, muted_traders = {}, {}
        for pnl in (-5.0, -6.0, -4.0, -5.5, -4.5):
            bot.check_circuit_breaker("0xTrader", "nick", pnl, 10.0, trader_performance, muted_traders)
        self.assertIn("0xtrader", muted_traders)
        self.assertIn("statistically significant negative edge", muted_traders["0xtrader"]["reason"])

    def test_a_single_large_outlier_among_small_wins_is_not_flagged_honest_limitation(self):
        # Verified against geo-anon-3's REAL 12-trade history (2026-07-26
        # audit): t = -0.92, well short of -1.645. A t-test is powered to
        # catch a CONSISTENT negative edge, not a rare-catastrophic-loss /
        # fat-tailed pattern -- one huge outlier inflates variance enough
        # that 5-10 samples can't distinguish it from noise. This is an
        # honest, known limitation of the EV/t-test redesign, not a bug:
        # geo-anon-3-shaped wallets still need the kind of manual review
        # that caught it this session, not automatic muting.
        trader_performance, muted_traders = {}, {}
        for pnl in (1.2, 0.9, 0.7, 1.0, -10.0):
            bot.check_circuit_breaker("0xTrader", "nick", pnl, 10.0, trader_performance, muted_traders)
        self.assertNotIn("0xtrader", muted_traders)

    def test_already_muted_wallet_does_not_get_a_second_mute_reason(self):
        trader_performance = {}
        muted_traders = {"0xtrader": {"muted_at": "t0", "reason": "original reason"}}
        bot.check_circuit_breaker("0xTrader", "nick", 5.0, 10.0, trader_performance, muted_traders)
        self.assertEqual(muted_traders["0xtrader"]["reason"], "original reason")

    def test_already_muted_wallet_return_history_still_updates(self):
        # Confirmed live (fed-warren-buffett): sells against an
        # already-open position keep happening after a mute (mutes only
        # block NEW buys), and this history is exactly what a future
        # rehabilitation mechanism would need.
        trader_performance = {}
        muted_traders = {"0xtrader": {"muted_at": "t0", "reason": "x"}}
        bot.check_circuit_breaker("0xTrader", "nick", 5.0, 10.0, trader_performance, muted_traders)
        self.assertEqual(trader_performance["0xtrader"]["recent_returns"], [0.5])

    def test_below_min_samples_never_mutes_regardless_of_how_bad(self):
        trader_performance, muted_traders = {}, {}
        for pnl in (-9.0, -9.0, -9.0):  # only 3, below MUTE_EV_MIN_SAMPLES=5
            bot.check_circuit_breaker("0xTrader", "nick", pnl, 10.0, trader_performance, muted_traders)
        self.assertNotIn("0xtrader", muted_traders)


class TestComputeShortfall(unittest.TestCase):
    def test_buy_worse_execution_is_positive_shortfall(self):
        # Paid 0.42 instead of the source's 0.40 -> 5% worse, positive.
        pct, usd = bot.compute_shortfall("BUY", 0.40, 0.42, trade_usd=100.0)
        self.assertAlmostEqual(pct, 0.05, places=6)
        self.assertAlmostEqual(usd, 5.0, places=6)

    def test_buy_better_execution_is_negative_shortfall(self):
        pct, usd = bot.compute_shortfall("BUY", 0.40, 0.38, trade_usd=100.0)
        self.assertAlmostEqual(pct, -0.05, places=6)
        self.assertAlmostEqual(usd, -5.0, places=6)

    def test_sell_worse_execution_is_positive_shortfall(self):
        # Received 0.37 instead of the source's 0.40 -> worse, positive.
        pct, usd = bot.compute_shortfall("SELL", 0.40, 0.37, shares=100.0)
        self.assertAlmostEqual(pct, 0.075, places=6)
        self.assertAlmostEqual(usd, 3.0, places=6)

    def test_missing_trade_usd_or_shares_defaults_usd_to_zero_not_error(self):
        pct, usd = bot.compute_shortfall("BUY", 0.40, 0.42)
        self.assertAlmostEqual(pct, 0.05, places=6)
        self.assertEqual(usd, 0.0)


class TestCheckSpreadTolerance(unittest.TestCase):
    """Since the 2026-07-31 CLOB cutover, check_spread_tolerance() calls
    polymarket_simulator.simulate_fill() instead of `bullpen polymarket
    preview` — mocked at that call site rather than bot.run_bullpen_json."""

    def setUp(self):
        self._saved_tolerance = config.SPREAD_TOLERANCE
        config.SPREAD_TOLERANCE = 0.10

    def tearDown(self):
        config.SPREAD_TOLERANCE = self._saved_tolerance

    def test_tight_spread_passes_and_returns_executable_price(self):
        fake_preview = {"price": 0.40, "spread": 0.02}  # relative spread 5%
        with patch("bot.polymarket_simulator.simulate_fill", return_value=fake_preview):
            ok, reason, price = bot.check_spread_tolerance("some-market", "Yes", 100, "BUY")
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertEqual(price, 0.40)

    def test_wide_relative_spread_rejected_even_with_identical_absolute_spread(self):
        # Same absolute spread (0.02) as the passing case above, but near a
        # $0.05 longshot price the relative spread is 40%, well over tolerance.
        fake_preview = {"price": 0.05, "spread": 0.02}
        with patch("bot.polymarket_simulator.simulate_fill", return_value=fake_preview):
            ok, reason, price = bot.check_spread_tolerance("some-market", "Yes", 100, "BUY")
        self.assertFalse(ok)
        self.assertIn("relative spread", reason)
        self.assertIsNone(price)

    def test_simulate_fill_exception_fails_safe(self):
        with patch("bot.polymarket_simulator.simulate_fill", side_effect=RuntimeError("book fetch failed")):
            ok, reason, price = bot.check_spread_tolerance("some-market", "Yes", 100, "BUY")
        self.assertFalse(ok)
        self.assertIn("preview unavailable", reason)
        self.assertIsNone(price)

    def test_empty_book_rejected_not_a_crash(self):
        with patch("bot.polymarket_simulator.simulate_fill", return_value={}):
            ok, reason, price = bot.check_spread_tolerance("some-market", "Yes", 100, "BUY")
        self.assertFalse(ok)
        self.assertIsNone(price)

    def test_insufficient_liquidity_rejected_with_three_element_return(self):
        # Regression guard: the pre-migration bullpen branch for this case
        # returned only a 2-tuple (missing the executable_price slot), which
        # would have raised on unpack at every real call site.
        fake_preview = {"price": 0.40, "spread": 0.01, "insufficient_liquidity": True}
        with patch("bot.polymarket_simulator.simulate_fill", return_value=fake_preview):
            result = bot.check_spread_tolerance("some-market", "Yes", 100, "BUY")
        self.assertEqual(len(result), 3)
        ok, reason, price = result
        self.assertFalse(ok)
        self.assertIn("insufficient book depth", reason)
        self.assertIsNone(price)


class TestMeasurePaperShortfall(unittest.TestCase):
    """Since the 2026-07-22 order-book-simulator cutover, measure_paper_shortfall()
    calls polymarket_simulator.simulate_fill() instead of bullpen — mocked at that
    call site rather than bot.run_bullpen_json."""

    def test_captures_trading_fee_and_network_fee_separately_from_slippage(self):
        fake_preview = {
            "price": 0.42, "spread": 0.01,
            "trading_fee": 1.2, "network_fee": 0.0,
        }
        with patch("bot.polymarket_simulator.simulate_fill", return_value=fake_preview):
            result = bot.measure_paper_shortfall("some-market", "Yes", "BUY", 100, 0.40, trade_usd=100.0)

        self.assertEqual(result["shortfall_status"], "ok")
        self.assertAlmostEqual(result["shortfall_pct"], 0.05, places=6)
        self.assertAlmostEqual(result["shortfall_usd"], 5.0, places=6)
        self.assertEqual(result["trading_fee_usd"], 1.2)
        self.assertEqual(result["network_fee_usd"], 0.0)
        # all-in cost = slippage + trading fee + network fee
        self.assertAlmostEqual(result["total_cost_usd"], 5.0 + 1.2 + 0.0, places=6)

    def test_missing_fee_fields_default_to_zero_not_none_or_error(self):
        fake_preview = {"price": 0.42, "spread": 0.01}  # no trading_fee/network_fee at all
        with patch("bot.polymarket_simulator.simulate_fill", return_value=fake_preview):
            result = bot.measure_paper_shortfall("some-market", "Yes", "BUY", 100, 0.40, trade_usd=100.0)

        self.assertEqual(result["trading_fee_usd"], 0.0)
        self.assertEqual(result["network_fee_usd"], 0.0)
        self.assertAlmostEqual(result["total_cost_usd"], result["shortfall_usd"], places=6)

    def test_preview_failure_still_fails_soft(self):
        with patch("bot.polymarket_simulator.simulate_fill", side_effect=RuntimeError("simulation unavailable")):
            result = bot.measure_paper_shortfall("some-market", "Yes", "BUY", 100, 0.40, trade_usd=100.0)
        self.assertEqual(result["shortfall_status"], "preview_unavailable")
        self.assertNotIn("trading_fee_usd", result)

    def test_empty_book_returns_no_executable_price_not_a_crash(self):
        # simulate_fill() returns {} (no "price" key) when a side of the book is empty.
        with patch("bot.polymarket_simulator.simulate_fill", return_value={}):
            result = bot.measure_paper_shortfall("some-market", "Yes", "BUY", 100, 0.40, trade_usd=100.0)
        self.assertEqual(result["shortfall_status"], "no_executable_price")

    def test_insufficient_liquidity_is_surfaced_not_silently_dropped(self):
        fake_preview = {
            "price": 0.42, "spread": 0.01, "trading_fee": 0.9, "network_fee": 0.0,
            "insufficient_liquidity": True, "shares_filled": 50.0,
        }
        with patch("bot.polymarket_simulator.simulate_fill", return_value=fake_preview):
            result = bot.measure_paper_shortfall("some-market", "Yes", "BUY", 100, 0.40, trade_usd=100.0)
        self.assertTrue(result["insufficient_liquidity"])
        self.assertEqual(result["shares_filled"], 50.0)


class TestMeasurePaperShortfallSpreadGateFlag(unittest.TestCase):
    """would_have_passed_spread_gate (2026-07-31) — measure_paper_shortfall()
    now computes, but never enforces, the SAME verdict check_spread_tolerance()
    would have made against the same book read (via the shared
    _evaluate_spread_gate()), on every return path. Requested directly:
    paper mode already tracked real spread/slippage data (measure_paper_
    shortfall since 2026-07-22) but had no explicit "would a live copy have
    been allowed" signal recorded alongside it."""

    def setUp(self):
        self._saved_tolerance = config.SPREAD_TOLERANCE
        config.SPREAD_TOLERANCE = 0.05

    def tearDown(self):
        config.SPREAD_TOLERANCE = self._saved_tolerance

    def test_tight_spread_and_sufficient_liquidity_would_have_passed(self):
        fake_preview = {"price": 0.40, "spread": 0.01}  # 2.5% relative
        with patch("bot.polymarket_simulator.simulate_fill", return_value=fake_preview):
            result = bot.measure_paper_shortfall("some-market", "Yes", "BUY", 100, 0.40, trade_usd=100.0)
        self.assertTrue(result["would_have_passed_spread_gate"])
        self.assertNotIn("spread_gate_reason", result)

    def test_wide_relative_spread_would_not_have_passed(self):
        fake_preview = {"price": 0.05, "spread": 0.02}  # 40% relative, same absolute spread as above
        with patch("bot.polymarket_simulator.simulate_fill", return_value=fake_preview):
            result = bot.measure_paper_shortfall("some-market", "Yes", "BUY", 100, 0.05, trade_usd=100.0)
        self.assertFalse(result["would_have_passed_spread_gate"])
        self.assertIn("relative spread", result["spread_gate_reason"])

    def test_insufficient_liquidity_would_not_have_passed_even_with_tight_spread(self):
        fake_preview = {"price": 0.40, "spread": 0.01, "insufficient_liquidity": True, "shares_filled": 50.0}
        with patch("bot.polymarket_simulator.simulate_fill", return_value=fake_preview):
            result = bot.measure_paper_shortfall("some-market", "Yes", "BUY", 100, 0.40, trade_usd=100.0)
        self.assertFalse(result["would_have_passed_spread_gate"])
        self.assertIn("insufficient book depth", result["spread_gate_reason"])

    def test_preview_failure_would_not_have_passed(self):
        with patch("bot.polymarket_simulator.simulate_fill", side_effect=RuntimeError("simulation unavailable")):
            result = bot.measure_paper_shortfall("some-market", "Yes", "BUY", 100, 0.40, trade_usd=100.0)
        self.assertFalse(result["would_have_passed_spread_gate"])
        self.assertIn("preview unavailable", result["spread_gate_reason"])

    def test_empty_book_would_not_have_passed(self):
        with patch("bot.polymarket_simulator.simulate_fill", return_value={}):
            result = bot.measure_paper_shortfall("some-market", "Yes", "BUY", 100, 0.40, trade_usd=100.0)
        self.assertFalse(result["would_have_passed_spread_gate"])
        self.assertIn("spread_gate_reason", result)


class TestGetMarketPrices(unittest.TestCase):
    """get_market_prices() — migrated 2026-07-22 off `bullpen polymarket price`
    onto polymarket_simulator.fetch_order_book_for_outcome() (TTP monitoring)."""

    def test_best_bid_is_top_of_book(self):
        fake_book = {"bids": [(0.48, 100.0)], "asks": [(0.50, 100.0)], "last_trade_price": 0.49}
        with patch("bot.polymarket_simulator.fetch_order_book_for_outcome", return_value=({}, fake_book)):
            best_bid, indicative, err = bot.get_market_prices("some-market", "Yes")
        self.assertIsNone(err)
        self.assertAlmostEqual(best_bid, 0.48)
        self.assertAlmostEqual(indicative, 0.48)  # best_bid wins over midpoint/last_trade

    def test_no_bid_falls_through_midpoint_to_last_trade(self):
        # midpoint needs BOTH sides; only the ask side is present here, so
        # this exercises the fallback chain landing on last_trade_price.
        fake_book = {"bids": [], "asks": [(0.50, 100.0)], "last_trade_price": 0.49}
        with patch("bot.polymarket_simulator.fetch_order_book_for_outcome", return_value=({}, fake_book)):
            best_bid, indicative, err = bot.get_market_prices("some-market", "Yes")
        self.assertIsNone(best_bid)  # no live bid -> a TTP exit must never fire on this
        self.assertIsNone(err)
        self.assertAlmostEqual(indicative, 0.49)

    def test_midpoint_used_when_top_bid_is_a_degenerate_zero_price(self):
        # A real Polymarket book can't have a 0-priced bid level (tick_size
        # enforces a positive minimum) — this defends against a malformed
        # value from the API itself (a boundary case, not paranoia about
        # our own code), falling through to midpoint since bids/asks are
        # both otherwise present.
        fake_book = {"bids": [(0.0, 100.0)], "asks": [(0.52, 100.0)], "last_trade_price": 0.10}
        with patch("bot.polymarket_simulator.fetch_order_book_for_outcome", return_value=({}, fake_book)):
            best_bid, indicative, err = bot.get_market_prices("some-market", "Yes")
        self.assertIsNone(err)
        self.assertIsNone(best_bid)  # degenerate -> never fire a TTP exit on it
        self.assertAlmostEqual(indicative, 0.26)  # midpoint of 0.0/0.52, wins over last_trade

    def test_falls_back_to_last_trade_when_book_is_one_sided_the_other_way(self):
        fake_book = {"bids": [], "asks": [], "last_trade_price": 0.49}
        with patch("bot.polymarket_simulator.fetch_order_book_for_outcome", return_value=({}, fake_book)):
            best_bid, indicative, err = bot.get_market_prices("some-market", "Yes")
        self.assertIsNone(best_bid)
        self.assertAlmostEqual(indicative, 0.49)
        self.assertIsNone(err)

    def test_completely_empty_book_is_an_error_not_a_crash(self):
        fake_book = {"bids": [], "asks": [], "last_trade_price": None}
        with patch("bot.polymarket_simulator.fetch_order_book_for_outcome", return_value=({}, fake_book)):
            best_bid, indicative, err = bot.get_market_prices("some-market", "Yes")
        self.assertIsNone(best_bid)
        self.assertIsNone(indicative)
        self.assertIsNotNone(err)

    def test_fetch_failure_fails_soft_with_an_error_string(self):
        with patch("bot.polymarket_simulator.fetch_order_book_for_outcome", side_effect=RuntimeError("no market")):
            best_bid, indicative, err = bot.get_market_prices("some-market", "Yes")
        self.assertIsNone(best_bid)
        self.assertIsNone(indicative)
        self.assertIn("no market", err)


class TestFetchDirectFeed(unittest.TestCase):
    """fetch_direct_feed() — the tracking-feed cutover (Rule 14, 2026-07-22).
    Confirms the shape returned matches what the old bullpen-backed feed
    returned (so every line downstream — dedup, sort, process_trade — needs
    zero changes), and that a per-wallet fetch error is logged, not raised."""

    def test_returns_trades_in_the_same_shape_the_old_bullpen_feed_used(self):
        fake_result = {"trades": [{"trade_id": "abc", "user_address": "0x1"}], "errors": []}
        with patch("bot.fetch_all_wallets_concurrent", return_value=fake_result), \
             patch("bot.append_log") as mock_log:
            feed = bot.fetch_direct_feed(executor=None, wallet_addresses=["0x1"])
        self.assertEqual(feed, {"trades": [{"trade_id": "abc", "user_address": "0x1"}]})
        mock_log.assert_not_called()

    def test_per_wallet_errors_are_logged_not_raised(self):
        fake_result = {"trades": [], "errors": [{"wallet_address": "0xBAD", "error": "timeout"}]}
        with patch("bot.fetch_all_wallets_concurrent", return_value=fake_result), \
             patch("bot.append_log") as mock_log:
            feed = bot.fetch_direct_feed(executor=None, wallet_addresses=["0xBAD"])
        self.assertEqual(feed, {"trades": []})
        mock_log.assert_called_once()
        logged_event = mock_log.call_args[0][0]
        self.assertEqual(logged_event["event_type"], "error")
        self.assertIn("0xBAD", logged_event["error"])


class TestResolveMarketEvent(unittest.TestCase):
    """resolve_market_event() — extended 2026-07-23 (point 3.1) to also read
    holding_rewards_enabled off the SAME market-metadata response used for
    event_slug, at zero extra API cost. See bot_market_event.
    holding_rewards_enabled's schema comment: this is an audit/documentation
    field only, never consumed by scoring/sizing.

    Patches polymarket_simulator.fetch_market_metadata (not bot.
    run_bullpen_json) since 2026-07-28: resolve_market_event() was swapped
    from `bullpen polymarket market` to a direct Gamma read after an outage
    where bullpen wasn't installed on the server, silently fail-closing
    every buy forever (this risk gate has no LIVE_MODE guard, unlike actual
    order execution). fetch_market_metadata() reshapes Gamma's response to
    the exact same field names bullpen used, so the fake_info dicts below
    are unchanged."""

    def test_extracts_event_slug_and_holding_rewards_enabled_together(self):
        fake_info = {
            "events": [{"slug": "some-event"}],
            "holdingRewardsEnabled": True,
        }
        with patch("polymarket_simulator.fetch_market_metadata", return_value=fake_info):
            event_slug, holding_rewards_enabled = bot.resolve_market_event("some-market")
        self.assertEqual(event_slug, "some-event")
        self.assertEqual(holding_rewards_enabled, True)

    def test_holding_rewards_enabled_false_is_preserved_not_coerced_to_none(self):
        fake_info = {"events": [{"slug": "some-event"}], "holdingRewardsEnabled": False}
        with patch("polymarket_simulator.fetch_market_metadata", return_value=fake_info):
            _, holding_rewards_enabled = bot.resolve_market_event("some-market")
        self.assertEqual(holding_rewards_enabled, False)

    def test_missing_holding_rewards_field_defaults_to_none(self):
        fake_info = {"events": [{"slug": "some-event"}]}
        with patch("polymarket_simulator.fetch_market_metadata", return_value=fake_info):
            _, holding_rewards_enabled = bot.resolve_market_event("some-market")
        self.assertIsNone(holding_rewards_enabled)

    def test_non_boolean_holding_rewards_field_defaults_to_none(self):
        # Defensive: this endpoint's fields have arrived JSON-string-encoded
        # before (see events handling below) — never trust the type blindly.
        fake_info = {"events": [{"slug": "some-event"}], "holdingRewardsEnabled": "true"}
        with patch("polymarket_simulator.fetch_market_metadata", return_value=fake_info):
            _, holding_rewards_enabled = bot.resolve_market_event("some-market")
        self.assertIsNone(holding_rewards_enabled)

    def test_subprocess_failure_returns_none_none(self):
        with patch("polymarket_simulator.fetch_market_metadata", side_effect=RuntimeError("boom")):
            event_slug, holding_rewards_enabled = bot.resolve_market_event("some-market")
        self.assertIsNone(event_slug)
        self.assertIsNone(holding_rewards_enabled)

    def test_unresolvable_event_still_returns_holding_rewards_enabled(self):
        # events missing/malformed -> event_slug None, but the holding-rewards
        # field (already extracted from the same response) isn't thrown away.
        fake_info = {"holdingRewardsEnabled": True}
        with patch("polymarket_simulator.fetch_market_metadata", return_value=fake_info):
            event_slug, holding_rewards_enabled = bot.resolve_market_event("some-market")
        self.assertIsNone(event_slug)
        self.assertEqual(holding_rewards_enabled, True)


class TestProcessTradeScoreSnapshot(unittest.TestCase):
    """process_trade()'s score_breakdown snapshot + decision_journal linkage
    (2026-07-23, point 3.2 prerequisite — see docs/copy-trading/
    RISK_MANAGEMENT.md Rule 22). Drives a full paper BUY through
    process_trade() with market_to_event/market_to_category pre-populated
    (so resolve_market_event/resolve_market_category are never called —
    keeps the mock surface to just the pieces this test actually cares
    about) and asserts the score_breakdown passed to append_log() matches
    compute_trade_size_usd()'s own tier logic, and that the returned
    decision_journal id lands on the position as last_decision_journal_id."""

    def _base_kwargs(self, wallet_score_entry):
        return dict(
            trade={
                "user_address": "0xTrader", "market_slug": "some-market", "outcome": "Yes",
                "side": "BUY", "price": 0.5, "size_usd": 100.0, "trade_id": "tid1",
                "timestamp": "2026-07-23T00:00:00Z", "market_title": "Some Market",
            },
            positions={}, source_positions={}, source_cost_basis={}, trader_performance={}, muted_traders={},
            tracked_by_lower={"0xtrader": ("0xTrader", "nick")},
            risk_state={
                "market_to_event": {"some-market": "some-event"},
                "market_to_category": {"some-market": "crypto"},
                "kill_switch": None,
                "active_rule_set_version": 3,
            },
            wallet_scores={"0xtrader": wallet_score_entry},
        )

    def test_category_tier_snapshot_and_decision_journal_link(self):
        wallet_score_entry = {
            "composite": 0.4, "composite_win_rate": 0.5, "composite_trade_count": 1000,
            "categories": {"crypto": {"score": 0.9, "pnl_t_stat": 2.0, "win_rate": 0.9, "trade_count": 40}},
        }
        kwargs = self._base_kwargs(wallet_score_entry)
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value={}), \
             patch("bot.fetch_book_depth_usd", return_value=None), \
             patch("bot.append_log", return_value="decision-journal-id-123") as mock_log:
            bot.process_trade(**kwargs)

        buy_calls = [c for c in mock_log.call_args_list if c.args[0].get("event_type") == "paper_buy"]
        self.assertEqual(len(buy_calls), 1)
        event = buy_calls[0].args[0]
        self.assertEqual(event["rule_set_version"], 3)
        breakdown = event["score_breakdown"]
        self.assertEqual(breakdown["sizing_tier"], "category")
        self.assertEqual(breakdown["category"], "crypto")
        self.assertEqual(breakdown["composite_score"], 0.4)
        self.assertEqual(
            breakdown["category_score_detail"],
            {"score": 0.9, "pnl_t_stat": 2.0, "win_rate": 0.9, "trade_count": 40},
        )
        # Kelly fields recorded for the eventual Brier/structural-break analysis.
        self.assertIsNotNone(breakdown["shrunk_win_rate"])
        self.assertIsNotNone(breakdown["kelly_fraction"])
        self.assertGreater(breakdown["kelly_fraction"], 0)  # 90% win rate vs 50% price -> positive edge

        pos = kwargs["positions"]["0xtrader|some-market|Yes"]
        self.assertEqual(pos["last_decision_journal_id"], "decision-journal-id-123")

    def test_composite_tier_when_no_category_score_present(self):
        wallet_score_entry = {
            "composite": 0.6, "composite_win_rate": 0.7, "composite_trade_count": 500,
            "categories": {},
        }
        kwargs = self._base_kwargs(wallet_score_entry)
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value={}), \
             patch("bot.fetch_book_depth_usd", return_value=None), \
             patch("bot.append_log", return_value="decision-journal-id-456") as mock_log:
            bot.process_trade(**kwargs)

        buy_calls = [c for c in mock_log.call_args_list if c.args[0].get("event_type") == "paper_buy"]
        breakdown = buy_calls[0].args[0]["score_breakdown"]
        self.assertEqual(breakdown["sizing_tier"], "composite")
        self.assertIsNone(breakdown["category_score_detail"])
        self.assertEqual(breakdown["composite_score"], 0.6)
        self.assertIsNotNone(breakdown["shrunk_win_rate"])
        self.assertIsNotNone(breakdown["kelly_fraction"])

    def test_base_tier_when_wallet_has_no_score_entry_at_all(self):
        kwargs = self._base_kwargs(None)
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value={}), \
             patch("bot.fetch_book_depth_usd", return_value=None), \
             patch("bot.append_log", return_value=None) as mock_log:
            bot.process_trade(**kwargs)

        buy_calls = [c for c in mock_log.call_args_list if c.args[0].get("event_type") == "paper_buy"]
        breakdown = buy_calls[0].args[0]["score_breakdown"]
        self.assertEqual(breakdown["sizing_tier"], "base")
        self.assertIsNone(breakdown["composite_score"])
        self.assertIsNone(breakdown["shrunk_win_rate"])
        self.assertIsNone(breakdown["kelly_fraction"])
        pos = kwargs["positions"]["0xtrader|some-market|Yes"]
        self.assertIsNone(pos["last_decision_journal_id"])

    def test_non_positive_kelly_edge_skips_the_copy_entirely(self):
        # 2026-07-28: found live — a wallet with a weak, not-yet-
        # statistically-significant category track record kept getting
        # floor-sized copies despite Kelly reading negative, since
        # should_skip_category()'s bar is deliberately stricter. This
        # confirms process_trade() now skips at the sizing layer instead
        # of opening a position on a signal the model's own math disagrees
        # with — no paper_buy, no position, a skip event logged with the
        # score_breakdown that drove the decision.
        wallet_score_entry = {
            "composite": 0.1, "composite_win_rate": 0.1, "composite_trade_count": 1000,
            "categories": {},
        }
        kwargs = self._base_kwargs(wallet_score_entry)
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value={}), \
             patch("bot.fetch_book_depth_usd", return_value=None), \
             patch("bot.append_log", return_value=None) as mock_log:
            bot.process_trade(**kwargs)

        buy_calls = [c for c in mock_log.call_args_list if c.args[0].get("event_type") == "paper_buy"]
        self.assertEqual(len(buy_calls), 0)
        skip_calls = [c for c in mock_log.call_args_list
                      if c.args[0].get("event_type") == "skip_non_positive_kelly_edge"]
        self.assertEqual(len(skip_calls), 1)
        event = skip_calls[0].args[0]
        self.assertLessEqual(event["score_breakdown"]["kelly_fraction"], 0)
        self.assertEqual(event["score_breakdown"]["trade_size_usd"], 0.0)
        self.assertNotIn("0xtrader|some-market|Yes", kwargs["positions"])


class TestDepthAwareTradeSizing(unittest.TestCase):
    """Depth-Aware Trade Sizing (2026-07-28) integration through
    process_trade() — mirrors TestProcessTradeScoreSnapshot's drive-a-real-
    BUY-through-process_trade style, patching bot.fetch_book_depth_usd
    instead of hitting the network."""

    def setUp(self):
        self._saved = (config.ENABLE_DEPTH_AWARE_TRADE_SIZING, config.TRADE_SIZE_DEPTH_FRACTION)
        config.TRADE_SIZE_DEPTH_FRACTION = 0.05

    def tearDown(self):
        config.ENABLE_DEPTH_AWARE_TRADE_SIZING, config.TRADE_SIZE_DEPTH_FRACTION = self._saved

    def _base_kwargs(self):
        # composite_win_rate=0.9 vs price=0.5 -> a large positive Kelly
        # fraction, so trade_usd lands near MAX_TRADE_USD and a thin book
        # will actually bind the depth clamp in these tests.
        wallet_score_entry = {
            "composite": 0.9, "composite_win_rate": 0.9, "composite_trade_count": 1000,
            "categories": {},
        }
        return dict(
            trade={
                "user_address": "0xTrader", "market_slug": "some-market", "outcome": "Yes",
                "side": "BUY", "price": 0.5, "size_usd": 100.0, "trade_id": "tid1",
                "timestamp": "2026-07-23T00:00:00Z", "market_title": "Some Market",
            },
            positions={}, source_positions={}, source_cost_basis={}, trader_performance={}, muted_traders={},
            tracked_by_lower={"0xtrader": ("0xTrader", "nick")},
            risk_state={
                "market_to_event": {"some-market": "some-event"},
                "market_to_category": {"some-market": "crypto"},
                "kill_switch": None,
                "active_rule_set_version": 3,
            },
            wallet_scores={"0xtrader": wallet_score_entry},
        )

    def test_default_off_logs_would_apply_but_does_not_shrink_the_trade(self):
        config.ENABLE_DEPTH_AWARE_TRADE_SIZING = False
        kwargs = self._base_kwargs()
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value={}), \
             patch("bot.fetch_book_depth_usd", return_value=1.0), \
             patch("bot.append_log", return_value=None) as mock_log:
            bot.process_trade(**kwargs)

        would_apply = [c for c in mock_log.call_args_list
                       if c.args[0].get("event_type") == "depth_cap_would_apply"]
        self.assertEqual(len(would_apply), 1)
        buy_calls = [c for c in mock_log.call_args_list if c.args[0].get("event_type") == "paper_buy"]
        self.assertEqual(len(buy_calls), 1)
        # Depth would clamp to 1.0*0.05=0.05, but the flag is off -- the
        # recorded size must be the ORIGINAL (unclamped) Kelly size.
        self.assertGreater(buy_calls[0].args[0]["score_breakdown"]["trade_size_usd"], 0.05)

    def test_enabled_actually_shrinks_the_trade_when_it_would_bind(self):
        config.ENABLE_DEPTH_AWARE_TRADE_SIZING = True
        kwargs = self._base_kwargs()
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value={}), \
             patch("bot.fetch_book_depth_usd", return_value=1.0), \
             patch("bot.append_log", return_value=None) as mock_log:
            bot.process_trade(**kwargs)

        buy_calls = [c for c in mock_log.call_args_list if c.args[0].get("event_type") == "paper_buy"]
        self.assertEqual(len(buy_calls), 1)
        # book_depth_usd=1.0 * TRADE_SIZE_DEPTH_FRACTION=0.05 -> clamped to 0.05.
        self.assertAlmostEqual(buy_calls[0].args[0]["score_breakdown"]["trade_size_usd"], 0.05)

    def test_none_book_depth_fetch_failure_leaves_trade_unclamped_even_when_enabled(self):
        config.ENABLE_DEPTH_AWARE_TRADE_SIZING = True
        kwargs = self._base_kwargs()
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value={}), \
             patch("bot.fetch_book_depth_usd", return_value=None), \
             patch("bot.append_log", return_value=None) as mock_log:
            bot.process_trade(**kwargs)

        buy_calls = [c for c in mock_log.call_args_list if c.args[0].get("event_type") == "paper_buy"]
        would_apply = [c for c in mock_log.call_args_list
                       if c.args[0].get("event_type") == "depth_cap_would_apply"]
        self.assertEqual(len(would_apply), 0)
        self.assertEqual(len(buy_calls), 1)
        self.assertGreater(buy_calls[0].args[0]["score_breakdown"]["trade_size_usd"], 0.05)

    def test_ample_depth_never_logs_would_apply(self):
        config.ENABLE_DEPTH_AWARE_TRADE_SIZING = False
        kwargs = self._base_kwargs()
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value={}), \
             patch("bot.fetch_book_depth_usd", return_value=100_000.0), \
             patch("bot.append_log", return_value=None) as mock_log:
            bot.process_trade(**kwargs)

        would_apply = [c for c in mock_log.call_args_list
                       if c.args[0].get("event_type") == "depth_cap_would_apply"]
        self.assertEqual(len(would_apply), 0)


class TestComputeAnchorPrice(unittest.TestCase):
    """The 'no high-chasing' rule (Rule 29) — anchor only ratchets down."""

    def test_no_existing_anchor_takes_observed_price(self):
        self.assertEqual(bot.compute_anchor_price(None, 0.45), 0.45)

    def test_lower_observed_price_ratchets_down(self):
        self.assertEqual(bot.compute_anchor_price(0.45, 0.40), 0.40)

    def test_higher_observed_price_does_not_raise_anchor(self):
        self.assertEqual(bot.compute_anchor_price(0.40, 0.45), 0.40)

    def test_equal_observed_price_is_a_no_op(self):
        self.assertEqual(bot.compute_anchor_price(0.45, 0.45), 0.45)


class TestComputeReboundThreshold(unittest.TestCase):
    """Hybrid tick-floor/percentage rebound confirmation (Rule 29)."""

    def setUp(self):
        self._saved = (config.LIMIT_ORDER_REBOUND_TICK_FLOOR, config.LIMIT_ORDER_REBOUND_PCT,
                       config.LIMIT_ORDER_TICK_SIZE)
        config.LIMIT_ORDER_REBOUND_TICK_FLOOR = 2
        config.LIMIT_ORDER_REBOUND_PCT = 0.05
        config.LIMIT_ORDER_TICK_SIZE = 0.01

    def tearDown(self):
        (config.LIMIT_ORDER_REBOUND_TICK_FLOOR, config.LIMIT_ORDER_REBOUND_PCT,
         config.LIMIT_ORDER_TICK_SIZE) = self._saved

    def test_tick_floor_dominates_at_longshot_prices(self):
        # 5% of $0.05 = $0.0025 (a quarter of one tick) -- the 2-tick floor
        # ($0.02) must govern instead, or "confirmation" would be noise.
        self.assertAlmostEqual(bot.compute_rebound_threshold(0.05), 0.02)

    def test_percentage_dominates_at_mid_to_high_prices(self):
        # 5% of $0.90 = $0.045, bigger than the $0.02 tick floor.
        self.assertAlmostEqual(bot.compute_rebound_threshold(0.90), 0.045)

    def test_crossover_point_is_continuous(self):
        # At the price where 5% == 2 ticks (price=0.40), both components
        # agree -- no discontinuity at the switch-over.
        self.assertAlmostEqual(bot.compute_rebound_threshold(0.40), 0.02)


class TestHasRebounded(unittest.TestCase):
    def setUp(self):
        self._saved = (config.LIMIT_ORDER_REBOUND_TICK_FLOOR, config.LIMIT_ORDER_REBOUND_PCT,
                       config.LIMIT_ORDER_TICK_SIZE)
        config.LIMIT_ORDER_REBOUND_TICK_FLOOR = 2
        config.LIMIT_ORDER_REBOUND_PCT = 0.05
        config.LIMIT_ORDER_TICK_SIZE = 0.01

    def tearDown(self):
        (config.LIMIT_ORDER_REBOUND_TICK_FLOOR, config.LIMIT_ORDER_REBOUND_PCT,
         config.LIMIT_ORDER_TICK_SIZE) = self._saved

    def test_price_still_at_the_low_has_not_rebounded(self):
        self.assertFalse(bot.has_rebounded(0.40, 0.40))

    def test_price_below_threshold_has_not_rebounded(self):
        self.assertFalse(bot.has_rebounded(0.41, 0.40))  # threshold is 0.02 at this price

    def test_price_at_threshold_has_rebounded(self):
        # Computed via the function itself rather than a hand-typed 0.42 --
        # float multiplication (0.05 * 0.40) doesn't land on exactly 0.02,
        # so a hand-typed boundary value is one float-precision hair off the
        # real threshold. This asserts the >= boundary correctly regardless.
        threshold = bot.compute_rebound_threshold(0.40)
        self.assertTrue(bot.has_rebounded(0.40 + threshold, 0.40))

    def test_price_well_above_threshold_has_rebounded(self):
        self.assertTrue(bot.has_rebounded(0.50, 0.40))


class TestPositionKeyCasing(unittest.TestCase):
    """2026-07-26 fix: position_key() lowercases `trader` -- otherwise the
    same real wallet reported in two different casings by two detection
    sources fragments into two different keys."""

    def test_lowercases_trader(self):
        self.assertEqual(bot.position_key("0xAbCd", "some-market", "Yes"), "0xabcd|some-market|Yes")

    def test_same_wallet_different_casing_produces_same_key(self):
        k1 = bot.position_key("0xAAAA", "m", "Yes")
        k2 = bot.position_key("0xaaaa", "m", "Yes")
        self.assertEqual(k1, k2)

    def test_market_slug_and_outcome_are_not_lowercased(self):
        # Only the address is case-ambiguous -- slugs/outcomes are not.
        self.assertEqual(bot.position_key("0xabcd", "Some-Market", "Yes"), "0xabcd|Some-Market|Yes")


class TestFindCrossTraderPositionCasing(unittest.TestCase):
    """2026-07-26 fix: trader comparison is case-insensitive, so stale data
    in a different casing than the current trader's casing isn't misread as
    a genuinely different trader."""

    def test_same_wallet_different_casing_is_not_treated_as_cross_trader(self):
        positions = {
            "0xAAAA|m1|Yes": {"shares": 10.0},
        }
        result = bot.find_cross_trader_position(positions, "0xaaaa|m1|Yes", "0xaaaa", "m1", "Yes")
        self.assertIsNone(result)

    def test_genuinely_different_trader_still_detected(self):
        positions = {
            "0xBBBB|m1|Yes": {"shares": 10.0},
        }
        result = bot.find_cross_trader_position(positions, "0xaaaa|m1|Yes", "0xaaaa", "m1", "Yes")
        self.assertEqual(result, "0xBBBB")


class TestWhaleStillHolding(unittest.TestCase):
    def setUp(self):
        self._saved = config.LIMIT_ORDER_WHALE_HOLD_MIN_FRACTION
        config.LIMIT_ORDER_WHALE_HOLD_MIN_FRACTION = 0.5

    def tearDown(self):
        config.LIMIT_ORDER_WHALE_HOLD_MIN_FRACTION = self._saved

    def test_no_baseline_trivially_passes(self):
        self.assertTrue(bot.whale_still_holding(0.0, None))
        self.assertTrue(bot.whale_still_holding(0.0, 0.0))

    def test_full_hold_passes(self):
        self.assertTrue(bot.whale_still_holding(100.0, 100.0))

    def test_small_trim_above_min_fraction_passes(self):
        self.assertTrue(bot.whale_still_holding(60.0, 100.0))  # still holding 60%

    def test_majority_exit_below_min_fraction_fails(self):
        self.assertFalse(bot.whale_still_holding(40.0, 100.0))  # dropped to 40%

    def test_exactly_at_min_fraction_passes(self):
        self.assertTrue(bot.whale_still_holding(50.0, 100.0))


class TestExecuteBuyExtraction(unittest.TestCase):
    """_execute_buy (Rule 29 extraction) is the shared fill path for both
    the immediate-copy flow and sweep_pending_executions()'s rebound fire —
    this exercises it directly, independent of process_trade, so a future
    change to either caller can't silently stop covering it."""

    def _base_event(self):
        return {"timestamp": "t", "trader_address": "0xTrader", "trader_nickname": "nick",
                "market_slug": "some-market", "outcome": "Yes", "side": "BUY",
                "mode": "paper"}

    def test_risk_gate_block_returns_none_and_logs_reason(self):
        positions = {}
        risk_state = {"market_to_event": {"some-market": "some-event"}, "kill_switch": None}
        with patch("bot.risk_manager.check_buy",
                   return_value=(False, "skip_risk_exposure_ceiling", "over cap")), \
             patch("bot.append_log") as mock_log:
            result = bot._execute_buy(
                self._base_event(), "0xTrader|some-market|Yes", "0xTrader", "some-market", "Yes",
                0.5, 10.0, "some-event", {}, positions, risk_state,
            )
        self.assertIsNone(result)
        self.assertEqual(positions, {})
        event = mock_log.call_args_list[0].args[0]
        self.assertEqual(event["event_type"], "skip_risk_exposure_ceiling")

    def test_successful_paper_buy_writes_ledger_and_returns_journal_id(self):
        positions = {}
        risk_state = {"market_to_event": {"some-market": "some-event"}, "kill_switch": None}
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value={}), \
             patch("bot.append_log", return_value="journal-id-1") as mock_log:
            result = bot._execute_buy(
                self._base_event(), "0xTrader|some-market|Yes", "0xTrader", "some-market", "Yes",
                0.5, 10.0, "some-event", {"sizing_tier": "base"}, positions, risk_state,
            )
        self.assertEqual(result, "journal-id-1")
        pos = positions["0xTrader|some-market|Yes"]
        self.assertAlmostEqual(pos["shares"], 20.0)  # 10 usd / 0.5 price
        self.assertEqual(pos["last_decision_journal_id"], "journal-id-1")
        buy_event = mock_log.call_args_list[0].args[0]
        self.assertEqual(buy_event["event_type"], "paper_buy")
        self.assertEqual(buy_event["our_trade_usd"], 10.0)

    def test_price_source_is_stamped_onto_the_position(self):
        positions = {}
        risk_state = {"market_to_event": {"some-market": "some-event"}, "kill_switch": None}
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value={}), \
             patch("bot.append_log", return_value="journal-id-1"):
            bot._execute_buy(
                self._base_event(), "0xTrader|some-market|Yes", "0xTrader", "some-market", "Yes",
                0.5, 10.0, "some-event", {}, positions, risk_state, price_source="wss_estimated",
            )
        self.assertEqual(positions["0xTrader|some-market|Yes"]["price_source"], "wss_estimated")

    def test_no_price_source_argument_leaves_position_untagged(self):
        positions = {}
        risk_state = {"market_to_event": {"some-market": "some-event"}, "kill_switch": None}
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value={}), \
             patch("bot.append_log", return_value="journal-id-1"):
            bot._execute_buy(
                self._base_event(), "0xTrader|some-market|Yes", "0xTrader", "some-market", "Yes",
                0.5, 10.0, "some-event", {}, positions, risk_state,
            )
        self.assertNotIn("price_source", positions["0xTrader|some-market|Yes"])

    def test_ok_shortfall_wires_real_price_and_fees_into_ledger(self):
        # source price is 0.5, but the live book can only actually fill at
        # 0.55 -- ledger must book the WORSE real price, not the optimistic
        # source price (2026-07-26 accounting fix).
        positions = {}
        risk_state = {"market_to_event": {"some-market": "some-event"}, "kill_switch": None}
        shortfall = {
            "shortfall_status": "ok", "executable_price": 0.55,
            "trading_fee_usd": 0.10, "network_fee_usd": 0.02,
        }
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value=shortfall), \
             patch("bot.append_log", return_value="journal-id-1"):
            bot._execute_buy(
                self._base_event(), "0xTrader|some-market|Yes", "0xTrader", "some-market", "Yes",
                0.5, 10.0, "some-event", {}, positions, risk_state,
            )
        pos = positions["0xTrader|some-market|Yes"]
        self.assertAlmostEqual(pos["shares"], 10.0 / 0.55)
        self.assertAlmostEqual(pos["cost_basis_usd"], 10.0 + 0.10 + 0.02)
        self.assertAlmostEqual(pos["avg_entry_price"], (10.0 + 0.10 + 0.02) / (10.0 / 0.55))

    def test_unmeasurable_shortfall_falls_back_to_source_price(self):
        positions = {}
        risk_state = {"market_to_event": {"some-market": "some-event"}, "kill_switch": None}
        shortfall = {"shortfall_status": "no_executable_price", "shortfall_raw_preview": {}}
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value=shortfall), \
             patch("bot.append_log", return_value="journal-id-1"):
            bot._execute_buy(
                self._base_event(), "0xTrader|some-market|Yes", "0xTrader", "some-market", "Yes",
                0.5, 10.0, "some-event", {}, positions, risk_state,
            )
        pos = positions["0xTrader|some-market|Yes"]
        self.assertAlmostEqual(pos["shares"], 20.0)  # 10 usd / source price 0.5, unchanged
        self.assertAlmostEqual(pos["cost_basis_usd"], 10.0)


class TestExecuteShadowBuy(unittest.TestCase):
    """_execute_shadow_buy() — Shadow Rehab (2026-07-27, Rule 37). Books a
    hypothetical copy for a MUTED wallet into an isolated shadow_positions
    dict, never risk_manager, never the real positions ledger."""

    def _base_event(self):
        return {"timestamp": "t", "trader_address": "0xTrader", "trader_nickname": "nick",
                "market_slug": "some-market", "outcome": "Yes", "side": "BUY", "mode": "paper"}

    def test_ok_shortfall_wires_real_price_into_shadow_ledger(self):
        shadow_positions = {}
        shortfall = {"shortfall_status": "ok", "executable_price": 0.55,
                     "trading_fee_usd": 0.10, "network_fee_usd": 0.02}
        with patch("bot.measure_paper_shortfall", return_value=shortfall), \
             patch("bot.append_log") as mock_log:
            bot._execute_shadow_buy(self._base_event(), "0xtrader|some-market|Yes",
                                     "some-market", "Yes", 0.5, shadow_positions)
        pos = shadow_positions["0xtrader|some-market|Yes"]
        self.assertAlmostEqual(pos["shares"], config.SHADOW_REHAB_TRADE_USD / 0.55)
        self.assertAlmostEqual(pos["cost_basis_usd"], config.SHADOW_REHAB_TRADE_USD + 0.10 + 0.02)
        event = mock_log.call_args_list[0].args[0]
        self.assertEqual(event["event_type"], "shadow_rehab_buy")

    def test_unmeasurable_shortfall_falls_back_to_source_price(self):
        shadow_positions = {}
        shortfall = {"shortfall_status": "preview_unavailable", "shortfall_error": "boom"}
        with patch("bot.measure_paper_shortfall", return_value=shortfall), \
             patch("bot.append_log"):
            bot._execute_shadow_buy(self._base_event(), "0xtrader|some-market|Yes",
                                     "some-market", "Yes", 0.5, shadow_positions)
        pos = shadow_positions["0xtrader|some-market|Yes"]
        self.assertAlmostEqual(pos["shares"], config.SHADOW_REHAB_TRADE_USD / 0.5)
        self.assertAlmostEqual(pos["cost_basis_usd"], config.SHADOW_REHAB_TRADE_USD)

    def test_multiple_buys_average_up_freely_no_max_buys_cap(self):
        # Deliberately no MAX_BUYS_PER_TRADER_OUTCOME enforcement here --
        # see the function's own docstring for why.
        shadow_positions = {}
        with patch("bot.measure_paper_shortfall", return_value={"shortfall_status": "preview_unavailable"}), \
             patch("bot.append_log"):
            for _ in range(5):
                bot._execute_shadow_buy(self._base_event(), "0xtrader|some-market|Yes",
                                         "some-market", "Yes", 0.5, shadow_positions)
        pos = shadow_positions["0xtrader|some-market|Yes"]
        self.assertEqual(pos["buy_count"], 5)
        self.assertAlmostEqual(pos["cost_basis_usd"], 5 * config.SHADOW_REHAB_TRADE_USD)


class TestExecuteBuyFakIntegration(unittest.TestCase):
    """Entry-side Marketable Limit Order (FAK) integration, 2026-07-26 --
    _execute_buy's LIVE_MODE branch when config.ENABLE_ENTRY_SLIPPAGE_CEILING_FAK
    is on. No live FAK order has ever actually been placed by this bot
    (paper-only all session) -- these are the first tests of this path at
    all, so they lean on the documented, UNVERIFIED-against-a-real-response
    status of extract_fill_price/extract_filled_shares rather than assuming
    a specific response shape."""

    def _base_event(self):
        return {"timestamp": "t", "trader_address": "0xTrader", "trader_nickname": "nick",
                "market_slug": "some-market", "outcome": "Yes", "side": "BUY",
                "mode": "live"}

    def setUp(self):
        self._saved_live_mode = config.LIVE_MODE
        self._saved_fak_flag = config.ENABLE_ENTRY_SLIPPAGE_CEILING_FAK
        config.LIVE_MODE = True
        config.ENABLE_ENTRY_SLIPPAGE_CEILING_FAK = True

    def tearDown(self):
        config.LIVE_MODE = self._saved_live_mode
        config.ENABLE_ENTRY_SLIPPAGE_CEILING_FAK = self._saved_fak_flag

    def _run(self, bullpen_response, trade_usd=10.0, price=0.5):
        positions = {}
        risk_state = {"market_to_event": {"some-market": "some-event"}, "kill_switch": None}
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.check_spread_tolerance", return_value=(True, None, price)), \
             patch("bot.check_slippage_ceiling", return_value=(True, None)), \
             patch("bot.compute_live_edge_pct", return_value=0.40), \
             patch("bot.run_bullpen_json", return_value=bullpen_response), \
             patch("bot.append_log", return_value="journal-id-1") as mock_log:
            result = bot._execute_buy(
                self._base_event(), "0xTrader|some-market|Yes", "0xTrader", "some-market", "Yes",
                price, trade_usd, "some-event", {}, positions, risk_state,
            )
        return result, positions, mock_log

    def test_places_limit_buy_fak_with_ceiling_price(self):
        # live edge 0.40 -> ceiling_pct = clamp(0.30*0.40, 0.05, 0.30) = 0.12
        # ceiling_price = 0.5 * 1.12 = 0.56
        response = {"status": "MATCHED", "transaction_hashes": ["0xabc"],
                    "fill_price": 0.55, "filled_shares": 18.0}
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.check_spread_tolerance", return_value=(True, None, 0.5)), \
             patch("bot.check_slippage_ceiling", return_value=(True, None)), \
             patch("bot.compute_live_edge_pct", return_value=0.40), \
             patch("bot.run_bullpen_json", return_value=response) as mock_run, \
             patch("bot.append_log", return_value="journal-id-1"):
            bot._execute_buy(
                self._base_event(), "0xTrader|some-market|Yes", "0xTrader", "some-market", "Yes",
                0.5, 10.0, "some-event", {}, {}, {"market_to_event": {"some-market": "some-event"},
                                                    "kill_switch": None},
            )
        args = mock_run.call_args.args[0]
        self.assertIn("limit-buy", args)
        self.assertIn("--price", args)
        self.assertEqual(args[args.index("--price") + 1], "0.56")
        self.assertIn("--expiration", args)
        self.assertEqual(args[args.index("--expiration") + 1], "fak")

    def test_partial_fill_books_only_actually_filled_shares(self):
        # Requested up to 10/0.56≈17.86 shares; book only had 12 within the
        # ceiling -- must book exactly 12, not the full-budget assumption.
        response = {"status": "MATCHED", "transaction_hashes": ["0xabc"],
                    "fill_price": 0.54, "filled_shares": 12.0}
        result, positions, _ = self._run(response)
        self.assertIsNotNone(result)
        pos = positions["0xTrader|some-market|Yes"]
        self.assertAlmostEqual(pos["shares"], 12.0)
        self.assertAlmostEqual(pos["cost_basis_usd"], 12.0 * 0.54)

    def test_full_fill_books_full_requested_size(self):
        response = {"status": "MATCHED", "transaction_hashes": ["0xabc"],
                    "fill_price": 0.55, "filled_shares": 18.0}
        result, positions, _ = self._run(response)
        pos = positions["0xTrader|some-market|Yes"]
        self.assertAlmostEqual(pos["shares"], 18.0)
        self.assertAlmostEqual(pos["cost_basis_usd"], 18.0 * 0.55)

    def test_price_present_but_shares_missing_falls_back_flagged(self):
        response = {"status": "MATCHED", "transaction_hashes": ["0xabc"], "fill_price": 0.55}
        result, positions, mock_log = self._run(response)
        pos = positions["0xTrader|some-market|Yes"]
        self.assertAlmostEqual(pos["shares"], 10.0 / 0.55)  # full-budget fallback
        buy_event = mock_log.call_args_list[0].args[0]
        self.assertEqual(buy_event["fill_accounting"], "fak_shares_unknown_assumed_full_budget")

    def test_zero_fill_is_treated_as_no_fill_not_silently_recorded(self):
        # require_filled() rejects unless status is MATCHED with real
        # tx_hashes -- a fully-killed FAK order (nothing filled) must raise,
        # not silently book a position.
        response = {"status": "CANCELLED", "transaction_hashes": []}
        result, positions, mock_log = self._run(response)
        self.assertIsNone(result)
        self.assertEqual(positions, {})
        event = mock_log.call_args_list[0].args[0]
        self.assertEqual(event["event_type"], "failed_trade")


class TestLiveBuyRealSpreadCheckWiring(unittest.TestCase):
    """Simulated-live-order coverage for the 2026-07-31 CLOB cutover
    (RISK_MANAGEMENT.md Rule 4 / SAFETY.md §49): every other LIVE_MODE test
    in this file mocks check_spread_tolerance() itself as a black box, which
    would pass even if the real simulate_fill wiring were completely broken.
    These tests mock only polymarket_simulator.simulate_fill and exercise
    the REAL check_spread_tolerance() through _execute_buy()'s plain
    (non-FAK) live-buy branch — config.ENABLE_ENTRY_SLIPPAGE_CEILING_FAK
    default is False, so this is the path a real live order actually takes
    today, unlike the FAK path TestExecuteBuyFakIntegration covers."""

    def setUp(self):
        self._saved_live_mode = config.LIVE_MODE
        self._saved_fak_flag = config.ENABLE_ENTRY_SLIPPAGE_CEILING_FAK
        self._saved_tolerance = config.SPREAD_TOLERANCE
        config.LIVE_MODE = True
        config.ENABLE_ENTRY_SLIPPAGE_CEILING_FAK = False
        config.SPREAD_TOLERANCE = 0.05

    def tearDown(self):
        config.LIVE_MODE = self._saved_live_mode
        config.ENABLE_ENTRY_SLIPPAGE_CEILING_FAK = self._saved_fak_flag
        config.SPREAD_TOLERANCE = self._saved_tolerance

    def _base_event(self):
        return {"timestamp": "t", "trader_address": "0xTrader", "trader_nickname": "nick",
                "market_slug": "some-market", "outcome": "Yes", "side": "BUY", "mode": "live"}

    def _run(self, book_preview):
        positions = {}
        risk_state = {"market_to_event": {"some-market": "some-event"}, "kill_switch": None}
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.polymarket_simulator.simulate_fill", return_value=book_preview), \
             patch("bot.check_slippage_ceiling", return_value=(True, None)), \
             patch("bot.run_bullpen_json", return_value={"status": "MATCHED", "transaction_hashes": ["0xabc"], "price": 0.50}) as mock_run, \
             patch("bot.append_log") as mock_log:
            result = bot._execute_buy(
                self._base_event(), "0xTrader|some-market|Yes", "0xTrader", "some-market", "Yes",
                0.50, 10.0, "some-event", {}, positions, risk_state,
            )
        return result, mock_run, mock_log

    def test_wide_book_spread_blocks_the_live_buy_before_any_order_is_placed(self):
        # price=0.50, spread=0.10 -> relative spread 20%, well over the 5% tolerance.
        book_preview = {"price": 0.50, "spread": 0.10}
        result, mock_run, mock_log = self._run(book_preview)
        self.assertIsNone(result)
        mock_run.assert_not_called()
        event = mock_log.call_args_list[0].args[0]
        self.assertEqual(event["event_type"], "skip_wide_spread")

    def test_tight_book_spread_lets_the_real_order_through(self):
        # price=0.50, spread=0.01 -> relative spread 2%, within the 5% tolerance.
        book_preview = {"price": 0.50, "spread": 0.01}
        result, mock_run, mock_log = self._run(book_preview)
        self.assertIsNotNone(result)
        mock_run.assert_called_once()
        args = mock_run.call_args.args[0]
        self.assertIn("buy", args)

    def test_insufficient_book_depth_blocks_the_live_buy(self):
        book_preview = {"price": 0.50, "spread": 0.01, "insufficient_liquidity": True}
        result, mock_run, mock_log = self._run(book_preview)
        self.assertIsNone(result)
        mock_run.assert_not_called()
        event = mock_log.call_args_list[0].args[0]
        self.assertEqual(event["event_type"], "skip_wide_spread")

    def test_book_fetch_failure_fails_safe_and_blocks_the_live_buy(self):
        positions = {}
        risk_state = {"market_to_event": {"some-market": "some-event"}, "kill_switch": None}
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.polymarket_simulator.simulate_fill", side_effect=RuntimeError("clob timeout")), \
             patch("bot.run_bullpen_json") as mock_run, \
             patch("bot.append_log") as mock_log:
            result = bot._execute_buy(
                self._base_event(), "0xTrader|some-market|Yes", "0xTrader", "some-market", "Yes",
                0.50, 10.0, "some-event", {}, positions, risk_state,
            )
        self.assertIsNone(result)
        mock_run.assert_not_called()
        event = mock_log.call_args_list[0].args[0]
        self.assertEqual(event["event_type"], "skip_wide_spread")
        self.assertIn("preview unavailable", event["reason"])


class TestLiveSellRealSpreadCheckWiring(unittest.TestCase):
    """Same simulated-live-order coverage as TestLiveBuyRealSpreadCheckWiring,
    for process_trade()'s live SELL branch (the proportional whale-mirroring
    exit, distinct from close_position_trailing_tp's own SELL call site)."""

    def setUp(self):
        self._saved_live_mode = config.LIVE_MODE
        self._saved_tolerance = config.SPREAD_TOLERANCE
        config.LIVE_MODE = True
        config.SPREAD_TOLERANCE = 0.05

    def tearDown(self):
        config.LIVE_MODE = self._saved_live_mode
        config.SPREAD_TOLERANCE = self._saved_tolerance

    def _kwargs(self, positions, source_positions, source_cost_basis):
        trade = {
            "user_address": "0xTrader", "market_slug": "some-market", "outcome": "Yes",
            "side": "SELL", "price": 0.50, "size_usd": 5.0, "trade_id": "polling-tid-1",
            "timestamp": "t", "market_title": "", "detected_by": "polling",
        }
        return dict(
            trade=trade, positions=positions, source_positions=source_positions,
            source_cost_basis=source_cost_basis, trader_performance={}, muted_traders={},
            tracked_by_lower={"0xtrader": ("0xTrader", "nick")},
            risk_state={"market_to_event": {"some-market": "some-event"},
                        "market_to_category": {"some-market": "crypto"}, "kill_switch": None},
            wallet_scores={},
        )

    def _positions(self):
        key = "0xtrader|some-market|Yes"
        return ({key: {"shares": 20.0, "cost_basis_usd": 8.0, "avg_entry_price": 0.4,
                        "buy_count": 1, "peak_profit_pct": 0.0}},
                {key: 20.0}, {key: 8.0})

    def test_wide_book_spread_blocks_the_live_sell_before_any_order_is_placed(self):
        positions, source_positions, source_cost_basis = self._positions()
        book_preview = {"price": 0.50, "spread": 0.10}  # 20% relative, over tolerance
        with patch("bot.polymarket_simulator.simulate_fill", return_value=book_preview), \
             patch("bot.run_bullpen_json") as mock_run, \
             patch("bot.append_log") as mock_log:
            bot.process_trade(**self._kwargs(positions, source_positions, source_cost_basis))
        mock_run.assert_not_called()
        logged_types = [c.args[0]["event_type"] for c in mock_log.call_args_list]
        self.assertIn("skip_wide_spread", logged_types)
        # position must remain untouched -- a blocked live sell must not be booked.
        self.assertEqual(positions["0xtrader|some-market|Yes"]["shares"], 20.0)

    def test_tight_book_spread_lets_the_real_sell_order_through(self):
        positions, source_positions, source_cost_basis = self._positions()
        book_preview = {"price": 0.50, "spread": 0.01}  # 2% relative, within tolerance
        with patch("bot.polymarket_simulator.simulate_fill", return_value=book_preview), \
             patch("bot.run_bullpen_json", return_value={"status": "MATCHED", "transaction_hashes": ["0xabc"], "price": 0.50}) as mock_run, \
             patch("bot.append_log"):
            bot.process_trade(**self._kwargs(positions, source_positions, source_cost_basis))
        mock_run.assert_called_once()
        args = mock_run.call_args.args[0]
        self.assertIn("sell", args)


class TestProcessTradeDualTrackReconciliation(unittest.TestCase):
    """'Dual-Track' WSS-as-primary-trigger reconciliation (2026-07-25) —
    process_trade()'s BUY branch, when it observes a polling trade for a
    key WSS already opened with only an ESTIMATED price. The single most
    important property under test: source_positions[key] (the share count)
    must NEVER be incremented a second time for the same real-world trade
    observed twice through two different feeds — only the dollar cost
    basis gets corrected."""

    def _kwargs(self, positions, source_positions, source_cost_basis, **trade_overrides):
        trade = {
            "user_address": "0xTrader", "market_slug": "some-market", "outcome": "Yes",
            "side": "BUY", "price": 0.55, "size_usd": 5.5, "trade_id": "polling-tid-1",
            "timestamp": "t", "market_title": "", "detected_by": "polling",
        }
        trade.update(trade_overrides)
        return dict(
            trade=trade, positions=positions, source_positions=source_positions,
            source_cost_basis=source_cost_basis, trader_performance={}, muted_traders={},
            tracked_by_lower={"0xtrader": ("0xTrader", "nick")},
            risk_state={"market_to_event": {"some-market": "some-event"},
                        "market_to_category": {"some-market": "crypto"}, "kill_switch": None},
            wallet_scores={},
        )

    def test_reconciles_cost_basis_without_double_counting_shares(self):
        key = "0xtrader|some-market|Yes"
        # WSS already opened this with an ESTIMATED price -- 10 real shares
        # (exact, from the on-chain event), valued at our own $0.60 guess.
        positions = {key: {"shares": 10.0, "cost_basis_usd": 6.0, "avg_entry_price": 0.6,
                            "buy_count": 1, "peak_profit_pct": 0.0, "price_source": "wss_estimated"}}
        source_positions = {key: 10.0}
        source_cost_basis = {key: 6.0}  # 10 shares * $0.60 estimate

        with patch("bot.risk_manager.check_buy") as mock_check_buy, \
             patch("bot.append_log") as mock_log:
            bot.process_trade(**self._kwargs(positions, source_positions, source_cost_basis))

        # The real fix under test: no new buy was attempted at all.
        mock_check_buy.assert_not_called()
        # Share count UNCHANGED -- this is the same real-world trade seen a
        # second time, not a new one; incrementing it here would double-count.
        self.assertEqual(source_positions[key], 10.0)
        # Cost basis corrected to the now-known-accurate price ($0.55, not $0.60).
        self.assertAlmostEqual(source_cost_basis[key], 5.5)  # 10 shares * real $0.55
        # OUR OWN position (shares/cost_basis_usd) is untouched -- it was
        # already accurate from the real WSS fill, nothing to correct.
        self.assertEqual(positions[key]["shares"], 10.0)
        self.assertEqual(positions[key]["cost_basis_usd"], 6.0)
        self.assertEqual(positions[key]["price_source"], "reconciled")

        recon_events = [c for c in mock_log.call_args_list
                        if c.args[0].get("event_type") == "whale_price_reconciled"]
        self.assertEqual(len(recon_events), 1)
        self.assertAlmostEqual(recon_events[0].args[0]["old_estimated_cost_basis"], 6.0)
        self.assertAlmostEqual(recon_events[0].args[0]["reconciled_cost_basis"], 5.5)

    def test_no_reconciliation_when_existing_position_was_already_wss_derived(self):
        """A position priced from a REAL on-chain collateral match
        (price_source="wss_derived") needs no correction -- this must fall
        through to the normal duplicate-position gate instead."""
        key = "0xtrader|some-market|Yes"
        positions = {key: {"shares": 10.0, "cost_basis_usd": 5.5, "avg_entry_price": 0.55,
                            "buy_count": 2, "peak_profit_pct": 0.0, "price_source": "wss_derived"}}
        source_positions = {key: 10.0}
        source_cost_basis = {key: 5.5}

        with patch("bot.append_log") as mock_log:
            bot.process_trade(**self._kwargs(positions, source_positions, source_cost_basis))

        recon_events = [c for c in mock_log.call_args_list
                        if c.args[0].get("event_type") == "whale_price_reconciled"]
        self.assertEqual(recon_events, [])
        # Falls through to the normal MAX_BUYS_PER_TRADER_OUTCOME cap (2) instead.
        dup_events = [c for c in mock_log.call_args_list
                     if c.args[0].get("event_type") == "skip_duplicate_position"]
        self.assertEqual(len(dup_events), 1)

    def test_no_reconciliation_when_this_event_is_itself_wss_sourced(self):
        """Reconciliation is specifically a polling-catches-up operation --
        a second WSS-detected event (detected_by="wss") for an already
        wss_estimated position must NOT be treated as a correction."""
        key = "0xTrader|some-market|Yes"
        positions = {key: {"shares": 10.0, "cost_basis_usd": 6.0, "avg_entry_price": 0.6,
                            "buy_count": 1, "peak_profit_pct": 0.0, "price_source": "wss_estimated"}}
        source_positions = {key: 10.0}
        source_cost_basis = {key: 6.0}

        with patch("bot.measure_paper_shortfall", return_value={}), \
             patch("bot.fetch_book_depth_usd", return_value=None), \
             patch("bot.append_log") as mock_log:
            bot.process_trade(**self._kwargs(
                positions, source_positions, source_cost_basis,
                detected_by="wss", price_source="wss_estimated",
            ))

        recon_events = [c for c in mock_log.call_args_list
                        if c.args[0].get("event_type") == "whale_price_reconciled"]
        self.assertEqual(recon_events, [])
        # A second WSS buy at buy_count=1 is still under the cap (2) -- falls
        # through to a normal (second) buy attempt, not silently dropped.
        dup_events = [c for c in mock_log.call_args_list
                     if c.args[0].get("event_type") == "skip_duplicate_position"]
        self.assertEqual(dup_events, [])

    def test_no_reconciliation_when_no_existing_position_at_all(self):
        """The reconciliation branch must not fire on a genuinely first-ever
        buy for a key -- there's nothing to reconcile against. Short-circuits
        via a risk-gate block (not mocking measure_paper_shortfall/etc. below
        it) since this test only cares about the reconciliation decision,
        not a full successful buy."""
        with patch("bot.risk_manager.check_buy",
                   return_value=(False, "skip_risk_exposure_ceiling", "over cap")), \
             patch("bot.fetch_book_depth_usd", return_value=None), \
             patch("bot.append_log") as mock_log:
            bot.process_trade(**self._kwargs({}, {}, {}))

        recon_events = [c for c in mock_log.call_args_list
                        if c.args[0].get("event_type") == "whale_price_reconciled"]
        self.assertEqual(recon_events, [])


class TestProcessTradeSellShortfallWiring(unittest.TestCase):
    """process_trade()'s SELL branch (2026-07-26 accounting fix) — realized
    PnL is booked at the sell, so an unwired exit price would leave PnL just
    as optimistic as an unwired entry price did before this fix."""

    def _kwargs(self, positions, source_positions, source_cost_basis, **trade_overrides):
        trade = {
            "user_address": "0xTrader", "market_slug": "some-market", "outcome": "Yes",
            "side": "SELL", "price": 0.50, "size_usd": 5.0, "trade_id": "polling-tid-1",
            "timestamp": "t", "market_title": "", "detected_by": "polling",
        }
        trade.update(trade_overrides)
        return dict(
            trade=trade, positions=positions, source_positions=source_positions,
            source_cost_basis=source_cost_basis, trader_performance={}, muted_traders={},
            tracked_by_lower={"0xtrader": ("0xTrader", "nick")},
            risk_state={"market_to_event": {"some-market": "some-event"},
                        "market_to_category": {"some-market": "crypto"}, "kill_switch": None},
            wallet_scores={},
        )

    def test_ok_shortfall_books_real_exit_price_and_fees(self):
        key = "0xtrader|some-market|Yes"
        # We hold 20 shares, cost basis $8 total ($0.40 avg). Source SELLs
        # $5 worth at 0.50 (-> 10 shares, half our position). Live book can
        # only actually fill at 0.45 -- pnl must reflect the WORSE real
        # exit, not the optimistic source price.
        positions = {key: {"shares": 20.0, "cost_basis_usd": 8.0, "avg_entry_price": 0.4,
                            "buy_count": 1, "peak_profit_pct": 0.0}}
        source_positions = {key: 20.0}
        source_cost_basis = {key: 8.0}
        shortfall = {
            "shortfall_status": "ok", "executable_price": 0.45,
            "trading_fee_usd": 0.05, "network_fee_usd": 0.01,
        }
        logged = {}

        def _capture(event):
            if event.get("event_type") == "paper_sell":
                logged.update(event)

        with patch("bot.measure_paper_shortfall", return_value=shortfall), \
             patch("bot.append_log", side_effect=_capture):
            bot.process_trade(**self._kwargs(positions, source_positions, source_cost_basis))
        expected_proceeds = 10.0 * 0.45 - (0.05 + 0.01)
        self.assertAlmostEqual(logged["proceeds_usd"], expected_proceeds)
        self.assertAlmostEqual(logged["pnl_usd"], expected_proceeds - 4.0)

    def test_unmeasurable_shortfall_falls_back_to_source_price_on_exit(self):
        key = "0xtrader|some-market|Yes"
        positions = {key: {"shares": 20.0, "cost_basis_usd": 8.0, "avg_entry_price": 0.4,
                            "buy_count": 1, "peak_profit_pct": 0.0}}
        source_positions = {key: 20.0}
        source_cost_basis = {key: 8.0}
        shortfall = {"shortfall_status": "preview_unavailable", "shortfall_error": "boom"}
        logged = {}

        def _capture(event):
            if event.get("event_type") == "paper_sell":
                logged.update(event)

        with patch("bot.measure_paper_shortfall", return_value=shortfall), \
             patch("bot.append_log", side_effect=_capture):
            bot.process_trade(**self._kwargs(positions, source_positions, source_cost_basis))
        # Falls back to the source price 0.50, no fee deduction.
        self.assertAlmostEqual(logged["proceeds_usd"], 10.0 * 0.50)
        self.assertAlmostEqual(logged["pnl_usd"], 10.0 * 0.50 - 4.0)


class TestProcessTradeShadowRehabWiring(unittest.TestCase):
    """process_trade()'s Shadow Rehab wiring (2026-07-27, Rule 37) — a
    muted wallet's BUY still books a hypothetical copy into
    shadow_positions instead of a real one; a SELL closes/reduces
    whichever shadow position exists, regardless of current mute status."""

    def setUp(self):
        self._saved = config.ENABLE_SHADOW_REHAB
        config.ENABLE_SHADOW_REHAB = True

    def tearDown(self):
        config.ENABLE_SHADOW_REHAB = self._saved

    def _kwargs(self, positions, source_positions, source_cost_basis, shadow_positions,
                muted_traders, **trade_overrides):
        trade = {
            "user_address": "0xTrader", "market_slug": "some-market", "outcome": "Yes",
            "side": "BUY", "price": 0.50, "size_usd": 5.0, "trade_id": "tid-1",
            "timestamp": "t", "market_title": "", "detected_by": "polling",
        }
        trade.update(trade_overrides)
        return dict(
            trade=trade, positions=positions, source_positions=source_positions,
            source_cost_basis=source_cost_basis, trader_performance={}, muted_traders=muted_traders,
            tracked_by_lower={"0xtrader": ("0xTrader", "nick")},
            risk_state={"market_to_event": {"some-market": "some-event"},
                        "market_to_category": {"some-market": "crypto"}, "kill_switch": None},
            wallet_scores={}, shadow_positions=shadow_positions,
        )

    def test_muted_wallet_buy_books_a_shadow_position_not_a_real_one(self):
        shadow_positions = {}
        muted_traders = {"0xtrader": {"muted_at": "t0", "reason": "x"}}
        with patch("bot.measure_paper_shortfall", return_value={"shortfall_status": "preview_unavailable"}), \
             patch("bot.append_log"):
            bot.process_trade(**self._kwargs({}, {}, {}, shadow_positions, muted_traders))
        self.assertIn("0xtrader|some-market|Yes", shadow_positions)

    def test_non_muted_wallet_buy_never_touches_shadow_positions(self):
        shadow_positions = {}
        with patch("bot.risk_manager.check_buy", return_value=(True, None, None)), \
             patch("bot.measure_paper_shortfall", return_value={"shortfall_status": "preview_unavailable"}), \
             patch("bot.fetch_book_depth_usd", return_value=None), \
             patch("bot.append_log"):
            bot.process_trade(**self._kwargs({}, {}, {}, shadow_positions, {}))
        self.assertEqual(shadow_positions, {})

    def test_sell_closes_shadow_position_even_with_no_real_position(self):
        key = "0xtrader|some-market|Yes"
        shadow_positions = {key: {"shares": 10.0, "cost_basis_usd": 5.0,
                                    "avg_entry_price": 0.5, "buy_count": 1}}
        source_positions = {key: 10.0}
        source_cost_basis = {key: 5.0}
        logged = {}

        def _capture(event):
            if event.get("event_type") == "shadow_rehab_sell":
                logged.update(event)

        with patch("bot.measure_paper_shortfall", return_value={"shortfall_status": "preview_unavailable"}), \
             patch("bot.append_log", side_effect=_capture):
            bot.process_trade(**self._kwargs(
                {}, source_positions, source_cost_basis, shadow_positions, {},
                side="SELL", size_usd=5.0,
            ))
        # Full sell (source_shares_sold == source_shares_held) -> fully closed.
        self.assertNotIn(key, shadow_positions)
        self.assertAlmostEqual(logged["pnl_usd"], 10.0 * 0.5 - 5.0)

    def test_disabled_flag_skips_shadow_buy_entirely(self):
        config.ENABLE_SHADOW_REHAB = False
        shadow_positions = {}
        muted_traders = {"0xtrader": {"muted_at": "t0", "reason": "x"}}
        with patch("bot.append_log"):
            bot.process_trade(**self._kwargs({}, {}, {}, shadow_positions, muted_traders))
        self.assertEqual(shadow_positions, {})

    def test_none_shadow_positions_is_safe_noop(self):
        # Callers that don't pass shadow_positions at all (default None)
        # must not crash the mute-block path.
        muted_traders = {"0xtrader": {"muted_at": "t0", "reason": "x"}}
        with patch("bot.append_log"):
            bot.process_trade(
                trade={"user_address": "0xTrader", "market_slug": "some-market", "outcome": "Yes",
                       "side": "BUY", "price": 0.5, "size_usd": 5.0, "trade_id": "tid-2",
                       "timestamp": "t", "market_title": ""},
                positions={}, source_positions={}, source_cost_basis={}, trader_performance={},
                muted_traders=muted_traders, tracked_by_lower={"0xtrader": ("0xTrader", "nick")},
                risk_state={"market_to_event": {}, "market_to_category": {}, "kill_switch": None},
                wallet_scores={},
            )  # no exception == pass


class TestSweepShadowRehab(unittest.TestCase):
    """sweep_shadow_rehab() (2026-07-27, Rule 37) — evidence-based mute
    recovery, the t-test machinery from Rule 36 run in reverse."""

    def setUp(self):
        self._saved_enable = config.ENABLE_SHADOW_REHAB
        self._saved_samples = config.MUTE_EV_MIN_SAMPLES
        self._saved_z = config.CATEGORY_SKIP_Z_CRITICAL
        config.ENABLE_SHADOW_REHAB = True
        config.MUTE_EV_MIN_SAMPLES = 5
        config.CATEGORY_SKIP_Z_CRITICAL = 1.645

    def tearDown(self):
        config.ENABLE_SHADOW_REHAB = self._saved_enable
        config.MUTE_EV_MIN_SAMPLES = self._saved_samples
        config.CATEGORY_SKIP_Z_CRITICAL = self._saved_z

    def test_significant_positive_shadow_edge_reinstates(self):
        muted_traders = {"0xtrader": {"muted_at": "t0", "reason": "was bad"}}
        returns = [0.5, 0.6, 0.4, 0.55, 0.45]
        with patch("bot.get_shadow_rehab_returns", return_value=returns), \
             patch("bot.append_log") as mock_log:
            bot.sweep_shadow_rehab(muted_traders)
        self.assertNotIn("0xtrader", muted_traders)
        event = mock_log.call_args_list[0].args[0]
        self.assertEqual(event["event_type"], "shadow_rehab_reinstated")

    def test_insignificant_shadow_returns_stays_muted(self):
        muted_traders = {"0xtrader": {"muted_at": "t0", "reason": "was bad"}}
        returns = [0.05, -0.05, 0.05, -0.05, 0.05]
        with patch("bot.get_shadow_rehab_returns", return_value=returns), \
             patch("bot.append_log"):
            bot.sweep_shadow_rehab(muted_traders)
        self.assertIn("0xtrader", muted_traders)

    def test_below_min_samples_stays_muted_regardless_of_returns(self):
        muted_traders = {"0xtrader": {"muted_at": "t0", "reason": "was bad"}}
        with patch("bot.get_shadow_rehab_returns", return_value=[0.9, 0.8]), \
             patch("bot.append_log"):
            bot.sweep_shadow_rehab(muted_traders)
        self.assertIn("0xtrader", muted_traders)

    def test_disabled_flag_never_reinstates(self):
        config.ENABLE_SHADOW_REHAB = False
        muted_traders = {"0xtrader": {"muted_at": "t0", "reason": "was bad"}}
        with patch("bot.get_shadow_rehab_returns", return_value=[0.5, 0.6, 0.4, 0.55, 0.45]), \
             patch("bot.append_log"):
            bot.sweep_shadow_rehab(muted_traders)
        self.assertIn("0xtrader", muted_traders)

    def test_empty_muted_traders_is_a_noop(self):
        with patch("bot.get_shadow_rehab_returns") as mock_get:
            bot.sweep_shadow_rehab({})
        mock_get.assert_not_called()


class TestSweepLiveWhaleEvents(unittest.TestCase):
    """sweep_live_whale_events() (Rule 29's consumer sweep, 2026-07-24) —
    the routing/skip decisions and, critically, that mark_whale_event_consumed()
    is ALWAYS called (the try/finally idempotency guarantee) regardless of
    what happened to the event."""

    def _event(self, **overrides):
        base = {
            "id": "ev1", "wallet_address": "0xTrader", "direction": "buy",
            "usdc_amount": 5.0, "price": 0.5, "share_amount": "10000000",  # 10 shares @ 6 decimals
            "tx_hash": "0xhash", "log_index": 0,
            "block_number": 100, "market_slug": "some-market", "outcome": "Yes",
        }
        base.update(overrides)
        return base

    def _call_sweep(self, events, tracked_by_lower=None, unmatched_events=None,
                     own_price_result=(None, "not mocked")):
        tracked_by_lower = tracked_by_lower or {"0xtrader": ("0xTrader", "nick")}
        positions, source_positions, source_cost_basis = {}, {}, {}
        with patch("bot.get_unconsumed_whale_events", return_value=events), \
             patch("bot.get_unconsumed_whale_events_without_registry_match",
                   return_value=unmatched_events or []), \
             patch("bot.mark_whale_event_consumed") as mock_consume, \
             patch("bot.process_trade") as mock_process_trade, \
             patch("bot.get_market_ask_price", return_value=own_price_result), \
             patch("bot.append_log") as mock_log:
            bot.sweep_live_whale_events(
                positions, source_positions, source_cost_basis, {}, {},
                tracked_by_lower, {"market_to_event": {}}, {},
            )
        return mock_consume, mock_process_trade, mock_log

    def test_valid_buy_event_routes_into_process_trade_with_correct_shape(self):
        mock_consume, mock_process_trade, _ = self._call_sweep([self._event()])

        mock_process_trade.assert_called_once()
        trade = mock_process_trade.call_args.args[0]
        self.assertEqual(trade["user_address"], "0xTrader")
        self.assertEqual(trade["market_slug"], "some-market")
        self.assertEqual(trade["outcome"], "Yes")
        self.assertEqual(trade["side"], "BUY")
        self.assertEqual(trade["price"], 0.5)
        self.assertEqual(trade["size_usd"], 5.0)  # usdc_amount, not share_amount
        self.assertEqual(trade["trade_id"], "onchain:0xhash:0")
        self.assertEqual(trade["price_source"], "wss_derived")
        self.assertEqual(trade["detected_by"], "wss")
        mock_consume.assert_called_once_with("ev1")

    def test_sell_direction_skips_process_trade_but_still_consumes(self):
        mock_consume, mock_process_trade, _ = self._call_sweep([self._event(direction="sell")])
        mock_process_trade.assert_not_called()
        mock_consume.assert_called_once_with("ev1")

    def test_null_price_falls_back_to_our_own_market_price_and_still_executes(self):
        """'Dual-Track' (2026-07-25): WSS is the primary trigger now -- a
        missing whale price no longer means skip, it means price OUR OWN
        current market read instead."""
        mock_consume, mock_process_trade, _ = self._call_sweep(
            [self._event(price=None, usdc_amount=None)], own_price_result=(0.6, None),
        )
        mock_process_trade.assert_called_once()
        trade = mock_process_trade.call_args.args[0]
        self.assertEqual(trade["price"], 0.6)  # OUR price, not the whale's (unknown)
        # size_usd back-derived from the exact on-chain share_amount (10 shares) at
        # OUR price, so source_shares = size_usd/price reproduces the real share count.
        self.assertAlmostEqual(trade["size_usd"], 10.0 * 0.6)
        self.assertEqual(trade["price_source"], "wss_estimated")
        mock_consume.assert_called_once_with("ev1")

    def test_null_price_and_no_live_market_price_either_skips_but_still_consumes(self):
        mock_consume, mock_process_trade, _ = self._call_sweep(
            [self._event(price=None, usdc_amount=None)],
            own_price_result=(None, "order book empty"),
        )
        mock_process_trade.assert_not_called()
        mock_consume.assert_called_once_with("ev1")

    def test_null_price_with_no_share_amount_on_record_skips_but_still_consumes(self):
        mock_consume, mock_process_trade, _ = self._call_sweep(
            [self._event(price=None, usdc_amount=None, share_amount=None)],
            own_price_result=(0.6, None),
        )
        mock_process_trade.assert_not_called()
        mock_consume.assert_called_once_with("ev1")

    def test_untracked_wallet_skips_process_trade_but_still_consumes(self):
        mock_consume, mock_process_trade, _ = self._call_sweep(
            [self._event(wallet_address="0xSomeoneElse")],
            tracked_by_lower={"0xtrader": ("0xTrader", "nick")},
        )
        mock_process_trade.assert_not_called()
        mock_consume.assert_called_once_with("ev1")

    def test_process_trade_exception_still_marks_consumed(self):
        """The core idempotency guarantee: whatever happens inside
        process_trade(), the event must never be left to retry forever."""
        events = [self._event()]
        with patch("bot.get_unconsumed_whale_events", return_value=events), \
             patch("bot.get_unconsumed_whale_events_without_registry_match", return_value=[]), \
             patch("bot.mark_whale_event_consumed") as mock_consume, \
             patch("bot.process_trade", side_effect=RuntimeError("boom")), \
             patch("bot.append_log") as mock_log:
            bot.sweep_live_whale_events({}, {}, {}, {}, {}, {"0xtrader": ("0xTrader", "nick")},
                                         {"market_to_event": {}}, {})
        mock_consume.assert_called_once_with("ev1")
        error_calls = [c for c in mock_log.call_args_list if c.args[0].get("event_type") == "error"]
        self.assertEqual(len(error_calls), 1)
        self.assertIn("boom", error_calls[0].args[0]["error"])

    def test_multiple_events_each_processed_and_consumed_independently(self):
        events = [self._event(id="ev1"), self._event(id="ev2", direction="sell"), self._event(id="ev3")]
        mock_consume, mock_process_trade, _ = self._call_sweep(events)
        self.assertEqual(mock_process_trade.call_count, 2)  # ev1, ev3 (ev2 is a sell)
        self.assertEqual(
            sorted(c.args[0] for c in mock_consume.call_args_list), ["ev1", "ev2", "ev3"]
        )


class TestSweepLiveWhaleEventsUnknownTokenFallback(unittest.TestCase):
    """The 'unknown token' on-demand fallback (2026-07-25, Rule 30
    addendum) — the second loop in sweep_live_whale_events() for events
    whose token_id has no token_registry match yet."""

    def _unmatched_event(self, **overrides):
        base = {
            "id": "ev-unmatched", "wallet_address": "0xTrader", "direction": "buy",
            "usdc_amount": 5.0, "price": 0.5, "tx_hash": "0xhash", "log_index": 0,
            "block_number": 100, "token_id": "999", "detected_at": int(time.time()),
        }
        base.update(overrides)
        return base

    def _call_sweep_with_unmatched(self, unmatched_events, fetch_result=None, fetch_side_effect=None,
                                    max_age_seconds=3600):
        tracked_by_lower = {"0xtrader": ("0xTrader", "nick")}
        positions, source_positions, source_cost_basis = {}, {}, {}
        with patch("bot.get_unconsumed_whale_events", return_value=[]), \
             patch("bot.get_unconsumed_whale_events_without_registry_match",
                   return_value=unmatched_events), \
             patch("bot.mark_whale_event_consumed") as mock_consume, \
             patch("bot.upsert_token_registry_row") as mock_upsert, \
             patch("bot.process_trade") as mock_process_trade, \
             patch("bot.append_log") as mock_log, \
             patch("bot.polymarket_simulator.fetch_market_by_token_id",
                   return_value=fetch_result, side_effect=fetch_side_effect), \
             patch.object(config, "WHALE_EVENT_FALLBACK_MAX_AGE_SECONDS", max_age_seconds):
            bot.sweep_live_whale_events(
                positions, source_positions, source_cost_basis, {}, {},
                tracked_by_lower, {"market_to_event": {}}, {},
            )
        return mock_consume, mock_upsert, mock_process_trade, mock_log

    def test_resolved_token_upserts_registry_and_processes_the_trade(self):
        mock_consume, mock_upsert, mock_process_trade, _ = self._call_sweep_with_unmatched(
            [self._unmatched_event()], fetch_result=("new-market-slug", "Yes"),
        )
        mock_upsert.assert_called_once_with("999", "new-market-slug", "Yes")
        mock_process_trade.assert_called_once()
        trade = mock_process_trade.call_args.args[0]
        self.assertEqual(trade["market_slug"], "new-market-slug")
        self.assertEqual(trade["outcome"], "Yes")
        mock_consume.assert_called_once_with("ev-unmatched")

    def test_unresolved_and_still_young_is_left_unconsumed_for_retry(self):
        mock_consume, mock_upsert, mock_process_trade, _ = self._call_sweep_with_unmatched(
            [self._unmatched_event(detected_at=int(time.time()))],  # just detected
            fetch_result=None, max_age_seconds=3600,
        )
        mock_upsert.assert_not_called()
        mock_process_trade.assert_not_called()
        mock_consume.assert_not_called()  # NOT marked consumed -- must retry next sweep

    def test_unresolved_past_max_age_gives_up_and_marks_consumed(self):
        old_event = self._unmatched_event(detected_at=int(time.time()) - 7200)  # 2h old
        mock_consume, mock_upsert, mock_process_trade, mock_log = self._call_sweep_with_unmatched(
            [old_event], fetch_result=None, max_age_seconds=3600,
        )
        mock_upsert.assert_not_called()
        mock_process_trade.assert_not_called()
        mock_consume.assert_called_once_with("ev-unmatched")
        error_calls = [c for c in mock_log.call_args_list if c.args[0].get("event_type") == "error"]
        self.assertEqual(len(error_calls), 1)
        self.assertIn("giving up", error_calls[0].args[0]["error"])

    def test_fetch_exception_is_treated_like_unresolved_not_a_crash(self):
        mock_consume, mock_upsert, mock_process_trade, _ = self._call_sweep_with_unmatched(
            [self._unmatched_event(detected_at=int(time.time()))],
            fetch_side_effect=RuntimeError("network error"), max_age_seconds=3600,
        )
        mock_upsert.assert_not_called()
        mock_process_trade.assert_not_called()
        mock_consume.assert_not_called()  # still young -- retried, not given up on

    def test_fetch_exception_past_max_age_still_gives_up(self):
        old_event = self._unmatched_event(detected_at=int(time.time()) - 7200)
        mock_consume, _, mock_process_trade, _ = self._call_sweep_with_unmatched(
            [old_event], fetch_side_effect=RuntimeError("network error"), max_age_seconds=3600,
        )
        mock_process_trade.assert_not_called()
        mock_consume.assert_called_once_with("ev-unmatched")


class TestComputeSlippageFloorPrice(unittest.TestCase):
    def setUp(self):
        self._saved = config.SLIPPAGE_PROTECTION_FRACTION, config.ORDER_PEG_FALLBACK_EDGE_PCT
        config.SLIPPAGE_PROTECTION_FRACTION = 0.30
        config.ORDER_PEG_FALLBACK_EDGE_PCT = 0.05

    def tearDown(self):
        config.SLIPPAGE_PROTECTION_FRACTION, config.ORDER_PEG_FALLBACK_EDGE_PCT = self._saved

    def test_floor_below_mid_by_protection_fraction_of_edge(self):
        # mid=0.50, edge=0.20 -> max_slippage = 0.30*0.20 = 0.06 -> floor = 0.50*0.94
        floor = bot.compute_slippage_floor_price(0.50, live_edge_pct=0.20)
        self.assertAlmostEqual(floor, 0.50 * 0.94)

    def test_none_edge_falls_back_to_conservative_default(self):
        floor_with_none = bot.compute_slippage_floor_price(0.50, live_edge_pct=None)
        floor_with_fallback = bot.compute_slippage_floor_price(0.50, live_edge_pct=0.05)
        self.assertAlmostEqual(floor_with_none, floor_with_fallback)

    def test_negative_edge_does_not_invert_the_floor_above_mid(self):
        floor = bot.compute_slippage_floor_price(0.50, live_edge_pct=-0.30)
        self.assertLessEqual(floor, 0.50)
        self.assertAlmostEqual(floor, 0.50)  # clamped to 0 slippage allowance, not negative


class TestComputeRepriceIntervalSeconds(unittest.TestCase):
    def setUp(self):
        self._saved = (config.ORDER_PEG_LOW_LIQUIDITY_SPREAD_RATIO_THRESHOLD,
                       config.ORDER_PEG_LOW_LIQUIDITY_INTERVAL_SECONDS,
                       config.ORDER_PEG_HIGH_LIQUIDITY_INTERVAL_SECONDS)
        config.ORDER_PEG_LOW_LIQUIDITY_SPREAD_RATIO_THRESHOLD = 0.05
        config.ORDER_PEG_LOW_LIQUIDITY_INTERVAL_SECONDS = 120
        config.ORDER_PEG_HIGH_LIQUIDITY_INTERVAL_SECONDS = 30

    def tearDown(self):
        (config.ORDER_PEG_LOW_LIQUIDITY_SPREAD_RATIO_THRESHOLD,
         config.ORDER_PEG_LOW_LIQUIDITY_INTERVAL_SECONDS,
         config.ORDER_PEG_HIGH_LIQUIDITY_INTERVAL_SECONDS) = self._saved

    def test_wide_spread_uses_the_slower_low_liquidity_interval(self):
        self.assertEqual(bot.compute_reprice_interval_seconds(0.10), 120)

    def test_tight_spread_uses_the_faster_high_liquidity_interval(self):
        self.assertEqual(bot.compute_reprice_interval_seconds(0.02), 30)

    def test_unknown_spread_fails_toward_the_slower_interval(self):
        self.assertEqual(bot.compute_reprice_interval_seconds(None), 120)


class TestComputePeggedPrice(unittest.TestCase):
    def test_no_ticks_elapsed_returns_init_price(self):
        self.assertAlmostEqual(bot.compute_pegged_price(0.50, 0.40, elapsed_seconds=10,
                                                          reprice_interval_seconds=30), 0.50)

    def test_decays_one_tick_per_completed_interval(self):
        price = bot.compute_pegged_price(0.50, 0.40, elapsed_seconds=65,
                                          reprice_interval_seconds=30, tick_decrement=0.01)
        self.assertAlmostEqual(price, 0.50 - 2 * 0.01)  # 65s / 30s = 2 completed intervals

    def test_never_decays_below_the_floor(self):
        price = bot.compute_pegged_price(0.50, 0.48, elapsed_seconds=10_000,
                                          reprice_interval_seconds=30, tick_decrement=0.01)
        self.assertAlmostEqual(price, 0.48)


class TestSweepPendingExitOrders(unittest.TestCase):
    """The safety-critical property under test: every pending_exit_order
    reaches a terminal state (filled or fallback_market_sell) once past its
    max wait -- NEVER left resting indefinitely."""

    def _order(self, **overrides):
        base = {
            "id": "exit1", "wallet_address": "0xTrader", "market_slug": "some-market",
            "outcome": "Yes", "position_key": "0xTrader|some-market|Yes", "shares": 10.0,
            "init_price": 0.50, "floor_price": 0.40, "current_price": 0.50,
            "bullpen_order_id": "order-abc", "close_reason": "trailing_tp",
            "created_at": time.time() - 10, "last_repriced_at": None,
        }
        base.update(overrides)
        return base

    def test_filled_order_books_the_exit_and_closes_it_out(self):
        positions = {"0xTrader|some-market|Yes": {"shares": 10.0, "cost_basis_usd": 4.0}}
        with patch("bot.get_pending_exit_orders", return_value=[self._order()]), \
             patch("bot.run_bullpen_json", return_value={"status": "FILLED", "price": 0.48}), \
             patch("bot.close_pending_exit_order") as mock_close, \
             patch("bot.check_circuit_breaker"), \
             patch("bot.append_log"):
            bot.sweep_pending_exit_orders(positions, {}, {})
        mock_close.assert_called_once()
        self.assertEqual(mock_close.call_args.args[1], "filled")
        self.assertNotIn("0xTrader|some-market|Yes", positions)

    def test_unfilled_past_max_wait_falls_back_to_market_sell_not_left_resting(self):
        old_order = self._order(created_at=time.time() - 99999)  # way past any reasonable max wait
        positions = {"0xTrader|some-market|Yes": {"shares": 10.0, "cost_basis_usd": 4.0}}
        with patch("bot.get_pending_exit_orders", return_value=[old_order]), \
             patch("bot.run_bullpen_json", side_effect=[
                 {"status": "OPEN"},           # poll-order: still unfilled
                 {"status": "ok"},              # orders --cancel
                 {"status": "MATCHED", "transaction_hashes": ["0xabc"], "price": 0.39},  # fallback sell
             ]), \
             patch("bot.close_pending_exit_order") as mock_close, \
             patch("bot.check_circuit_breaker"), \
             patch("bot.append_log") as mock_log:
            bot.sweep_pending_exit_orders(positions, {}, {})
        mock_close.assert_called_once()
        self.assertEqual(mock_close.call_args.args[1], "fallback_market_sell")
        self.assertNotIn("0xTrader|some-market|Yes", positions)
        fallback_events = [c for c in mock_log.call_args_list
                           if c.args[0].get("event_type") == "patient_exit_fallback_triggered"]
        self.assertEqual(len(fallback_events), 1)

    def test_fallback_sell_itself_failing_logs_error_does_not_crash_or_silently_retry(self):
        """The one truly dangerous outcome (canceled AND the fallback sell
        failed) must surface as a loud error, not be swallowed."""
        old_order = self._order(created_at=time.time() - 99999)
        positions = {"0xTrader|some-market|Yes": {"shares": 10.0, "cost_basis_usd": 4.0}}
        with patch("bot.get_pending_exit_orders", return_value=[old_order]), \
             patch("bot.run_bullpen_json", side_effect=[
                 {"status": "OPEN"},
                 {"status": "ok"},
                 RuntimeError("network down"),
             ]), \
             patch("bot.close_pending_exit_order") as mock_close, \
             patch("bot.append_log") as mock_log:
            bot.sweep_pending_exit_orders(positions, {}, {})
        mock_close.assert_not_called()  # never marked resolved -- stays 'pending', retried, AND flagged
        error_events = [c for c in mock_log.call_args_list if c.args[0].get("event_type") == "error"]
        self.assertEqual(len(error_events), 1)
        self.assertIn("needs manual review", error_events[0].args[0]["error"])
        # Position must still be open -- fallback failure must never silently
        # drop the position from the ledger.
        self.assertIn("0xTrader|some-market|Yes", positions)

    def test_unfilled_within_wait_and_reprice_not_due_does_nothing(self):
        order = self._order(created_at=time.time() - 10, last_repriced_at=time.time() - 5)
        with patch("bot.get_pending_exit_orders", return_value=[order]), \
             patch("bot.run_bullpen_json", return_value={"status": "OPEN"}), \
             patch("bot.get_market_bid_ask", return_value=(0.48, 0.50, None)), \
             patch("bot.update_pending_exit_order_price") as mock_update, \
             patch("bot.close_pending_exit_order") as mock_close, \
             patch("bot.append_log"):
            bot.sweep_pending_exit_orders({}, {}, {})
        mock_update.assert_not_called()
        mock_close.assert_not_called()

    def test_unfilled_and_reprice_due_cancels_and_replaces_at_decayed_price(self):
        order = self._order(created_at=time.time() - 40, last_repriced_at=time.time() - 35,
                             init_price=0.50, floor_price=0.40, current_price=0.50)
        with patch("bot.get_pending_exit_orders", return_value=[order]), \
             patch("bot.run_bullpen_json", side_effect=[
                 {"status": "OPEN"},                         # poll-order
                 {"status": "ok"},                            # cancel
                 {"order_id": "order-new"},                   # new limit-sell
             ]), \
             patch("bot.get_market_bid_ask", return_value=(0.47, 0.49, None)), \
             patch("bot.compute_reprice_interval_seconds", return_value=30), \
             patch("bot.update_pending_exit_order_price") as mock_update, \
             patch("bot.append_log"):
            bot.sweep_pending_exit_orders({}, {}, {})
        mock_update.assert_called_once()
        new_price = mock_update.call_args.args[1]
        self.assertLess(new_price, 0.50)
        self.assertGreaterEqual(new_price, 0.40)


class TestComputeDaysRemaining(unittest.TestCase):
    def test_parses_a_future_date_correctly(self):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        days = bot.compute_days_remaining("2026-07-27", now=now)
        self.assertAlmostEqual(days, 7.0)

    def test_past_date_floors_at_zero_not_negative(self):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        days = bot.compute_days_remaining("2026-07-01", now=now)
        self.assertEqual(days, 0.0)

    def test_none_input_returns_none(self):
        self.assertIsNone(bot.compute_days_remaining(None))

    def test_malformed_date_returns_none_not_an_exception(self):
        self.assertIsNone(bot.compute_days_remaining("not-a-date"))


class TestComputeThetaDecayActivationPct(unittest.TestCase):
    def setUp(self):
        self._saved = (config.THETA_DECAY_TP_MIN_ACTIVATION_PCT,
                       config.THETA_DECAY_TP_MAX_ACTIVATION_PCT, config.THETA_DECAY_TP_WINDOW_DAYS,
                       config.TRAILING_TP_ACTIVATION_PCT)
        config.THETA_DECAY_TP_MIN_ACTIVATION_PCT = 0.15
        config.THETA_DECAY_TP_MAX_ACTIVATION_PCT = 0.50
        config.THETA_DECAY_TP_WINDOW_DAYS = 7
        config.TRAILING_TP_ACTIVATION_PCT = 0.50

    def tearDown(self):
        (config.THETA_DECAY_TP_MIN_ACTIVATION_PCT, config.THETA_DECAY_TP_MAX_ACTIVATION_PCT,
         config.THETA_DECAY_TP_WINDOW_DAYS, config.TRAILING_TP_ACTIVATION_PCT) = self._saved

    def test_none_days_remaining_falls_back_to_static_threshold(self):
        self.assertEqual(bot.compute_theta_decay_activation_pct(None), 0.50)

    def test_at_or_beyond_the_window_uses_the_max_threshold(self):
        self.assertAlmostEqual(bot.compute_theta_decay_activation_pct(7), 0.50)
        self.assertAlmostEqual(bot.compute_theta_decay_activation_pct(30), 0.50)  # min(1, 30/7) clamps to 1

    def test_at_resolution_uses_the_min_threshold(self):
        self.assertAlmostEqual(bot.compute_theta_decay_activation_pct(0), 0.15)

    def test_scales_linearly_in_between(self):
        # 3.5 days = half the 7-day window -> halfway between min and max
        result = bot.compute_theta_decay_activation_pct(3.5)
        self.assertAlmostEqual(result, 0.15 + 0.5 * (0.50 - 0.15))


class TestCheckTrailingTakeProfitThetaDecayWiring(unittest.TestCase):
    """The opt-in flag's actual effect on the activation threshold used —
    not the full TTP exit flow (unrelated to this change), just that the
    dynamic threshold is (or isn't) consulted based on the flag."""

    def _position(self, peak=0.20):
        return {"shares": 10.0, "cost_basis_usd": 5.0, "avg_entry_price": 0.5,
                "buy_count": 1, "peak_profit_pct": peak}

    def test_flag_off_uses_the_static_threshold_unchanged(self):
        positions = {"0xTrader|some-market|Yes": self._position(peak=0.20)}
        with patch.object(config, "ENABLE_THETA_DECAY_TP_ACTIVATION", False), \
             patch.object(config, "TRAILING_TP_ACTIVATION_PCT", 0.50), \
             patch("bot.get_market_prices", return_value=(0.55, 0.60, None)), \
             patch("bot.resolve_market_end_date") as mock_resolve, \
             patch("bot.close_position_trailing_tp") as mock_close:
            bot.check_trailing_take_profit(positions, {}, {}, {})
        mock_resolve.assert_not_called()  # end date never even looked up when the flag is off
        mock_close.assert_not_called()  # peak 20% < static 50% -- not armed, unchanged from today

    def test_flag_on_uses_the_dynamic_threshold_and_can_arm_earlier(self):
        # Peak 20% would NOT arm the static 50% bar, but WOULD arm a
        # theta-decayed threshold near resolution (min 15%).
        positions = {"0xTrader|some-market|Yes": self._position(peak=0.20)}
        with patch.object(config, "ENABLE_THETA_DECAY_TP_ACTIVATION", True), \
             patch.object(config, "TRAILING_TP_DRAWDOWN_PCT", 0.10), \
             patch("bot.get_market_prices", return_value=(0.10, 0.10, None)), \
             patch("bot.resolve_market_end_date", return_value="2026-01-01"), \
             patch("bot.save_market_end_date"), \
             patch("bot.compute_days_remaining", return_value=0.0), \
             patch("bot.close_position_trailing_tp") as mock_close:
            bot.check_trailing_take_profit(positions, {}, {}, {}, market_to_end_date={})
        # armed (peak 20% >= dynamic ~15% threshold) AND price already
        # pulled back far enough (0.5 entry -> 0.10 bid is a big drawdown)
        # -- should have triggered a close.
        mock_close.assert_called_once()

    def test_flag_on_caches_the_resolved_end_date_for_reuse(self):
        positions = {"0xTrader|some-market|Yes": self._position(peak=0.05)}
        market_to_end_date = {}
        with patch.object(config, "ENABLE_THETA_DECAY_TP_ACTIVATION", True), \
             patch("bot.get_market_prices", return_value=(0.55, 0.55, None)), \
             patch("bot.resolve_market_end_date", return_value="2026-08-01") as mock_resolve, \
             patch("bot.save_market_end_date") as mock_save:
            bot.check_trailing_take_profit(positions, {}, {}, {}, market_to_end_date=market_to_end_date)
        mock_resolve.assert_called_once()
        mock_save.assert_called_once_with("some-market", "2026-08-01")
        self.assertEqual(market_to_end_date["some-market"], "2026-08-01")


class TestSweepZombiePositions(unittest.TestCase):
    """Zombie-position dump-exit fallback (2026-07-27) — see
    bot.sweep_zombie_positions/close_position_zombie_dump docstrings and
    RISK_MANAGEMENT.md's zombie-position rule. Mirrors
    TestCheckTrailingTakeProfitThetaDecayWiring's mocking style: patch the
    network/side-effecting boundaries (get_market_prices, append_log,
    close_position_zombie_dump), assert on what got called and with what.
    """

    def _position(self, age_seconds, last_priced_at=None):
        now = time.time()
        return {
            "shares": 10.0, "cost_basis_usd": 5.0, "avg_entry_price": 0.5, "buy_count": 1,
            "peak_profit_pct": 0.0,
            "last_priced_at": (now - age_seconds) if last_priced_at is None else last_priced_at,
        }

    def setUp(self):
        bot._zombie_unresolvable_failures.clear()

    def test_position_below_threshold_is_left_alone(self):
        positions = {"0xTrader|some-market|Yes": self._position(age_seconds=3600)}
        with patch.object(config, "ZOMBIE_POSITION_THRESHOLD_SECONDS", 86400), \
             patch("bot.get_market_prices") as mock_prices, \
             patch("bot.close_position_zombie_dump") as mock_dump:
            bot.sweep_zombie_positions(positions, {}, {}, {})
        mock_prices.assert_not_called()
        mock_dump.assert_not_called()

    def test_position_with_no_last_priced_at_is_skipped_not_guessed(self):
        pos = self._position(age_seconds=999999)
        del pos["last_priced_at"]
        positions = {"0xTrader|some-market|Yes": pos}
        with patch("bot.get_market_prices") as mock_prices:
            bot.sweep_zombie_positions(positions, {}, {}, {})
        mock_prices.assert_not_called()

    def test_flag_off_logs_dry_run_and_never_calls_dump(self):
        positions = {"0xTrader|some-market|Yes": self._position(age_seconds=90000)}
        with patch.object(config, "ZOMBIE_POSITION_THRESHOLD_SECONDS", 86400), \
             patch.object(config, "ENABLE_ZOMBIE_POSITION_DUMP", False), \
             patch("bot.get_market_prices", return_value=(0.10, 0.10, None)), \
             patch("bot.append_log") as mock_log, \
             patch("bot.close_position_zombie_dump") as mock_dump:
            bot.sweep_zombie_positions(positions, {}, {}, {})
        mock_dump.assert_not_called()
        logged_types = [c.args[0]["event_type"] for c in mock_log.call_args_list]
        self.assertIn("zombie_position_would_dump", logged_types)

    def test_flag_on_calls_dump_with_the_priced_market(self):
        positions = {"0xTrader|some-market|Yes": self._position(age_seconds=90000)}
        with patch.object(config, "ZOMBIE_POSITION_THRESHOLD_SECONDS", 86400), \
             patch.object(config, "ENABLE_ZOMBIE_POSITION_DUMP", True), \
             patch("bot.get_market_prices", return_value=(0.10, 0.10, None)) as mock_prices, \
             patch("bot.close_position_zombie_dump") as mock_dump:
            bot.sweep_zombie_positions(positions, {}, {}, {})
        # bypasses the staleness gate -- the whole point of this path.
        mock_prices.assert_called_once_with("some-market", "Yes", ignore_staleness=True)
        mock_dump.assert_called_once()
        called_key = mock_dump.call_args.args[0]
        self.assertEqual(called_key, "0xTrader|some-market|Yes")
        self.assertEqual(mock_dump.call_args.args[6], 0.10)  # indicative_price positional arg

    def test_unresolvable_market_never_calls_dump_even_when_flag_on(self):
        positions = {"0xTrader|some-market|Yes": self._position(age_seconds=90000)}
        with patch.object(config, "ZOMBIE_POSITION_THRESHOLD_SECONDS", 86400), \
             patch.object(config, "ENABLE_ZOMBIE_POSITION_DUMP", True), \
             patch("bot.get_market_prices", return_value=(None, None, "no market found for slug 'x'")), \
             patch("bot.append_log") as mock_log, \
             patch("bot.close_position_zombie_dump") as mock_dump:
            bot.sweep_zombie_positions(positions, {}, {}, {})
        mock_dump.assert_not_called()
        logged_types = [c.args[0]["event_type"] for c in mock_log.call_args_list]
        self.assertIn("error", logged_types)
        error_call = next(c.args[0] for c in mock_log.call_args_list if c.args[0]["event_type"] == "error")
        self.assertIn("zombie position unresolvable", error_call["error"])

    def test_unresolvable_failures_are_throttled_not_logged_every_sweep(self):
        key = "0xTrader|some-market|Yes"
        with patch.object(config, "ZOMBIE_POSITION_THRESHOLD_SECONDS", 86400), \
             patch.object(config, "ZOMBIE_UNRESOLVABLE_LOG_EVERY", 4), \
             patch("bot.get_market_prices", return_value=(None, None, "no market found")), \
             patch("bot.append_log") as mock_log:
            for _ in range(4):
                positions = {key: self._position(age_seconds=90000)}
                bot.sweep_zombie_positions(positions, {}, {}, {})
        # 1st failure logs, 2nd/3rd throttled, 4th logs again (4 % 4 == 0).
        self.assertEqual(mock_log.call_count, 2)

    def test_a_successful_price_read_clears_the_unresolvable_throttle_counter(self):
        key = "0xTrader|some-market|Yes"
        bot._zombie_unresolvable_failures[key] = 3
        positions = {key: self._position(age_seconds=90000)}
        with patch.object(config, "ZOMBIE_POSITION_THRESHOLD_SECONDS", 86400), \
             patch.object(config, "ENABLE_ZOMBIE_POSITION_DUMP", False), \
             patch("bot.get_market_prices", return_value=(0.10, 0.10, None)), \
             patch("bot.append_log"):
            bot.sweep_zombie_positions(positions, {}, {}, {})
        self.assertNotIn(key, bot._zombie_unresolvable_failures)


class TestClosePositionZombieDump(unittest.TestCase):
    """close_position_zombie_dump's own contract, independent of the sweep
    that calls it: paper mode closes at the given price with no network
    call; live mode uses ZOMBIE_EXIT_MAX_SLIPPAGE (NOT SLIPPAGE_TOLERANCE)
    as its floor and never calls check_spread_tolerance — see the
    function's docstring for why the spread gate is deliberately skipped.
    """

    def _position(self):
        return {"shares": 10.0, "cost_basis_usd": 5.0, "avg_entry_price": 0.5,
                "buy_count": 1, "peak_profit_pct": 0.0, "last_priced_at": time.time()}

    def test_paper_mode_closes_at_the_given_price_with_no_network_call(self):
        key = "0xTrader|some-market|Yes"
        positions = {key: self._position()}
        with patch.object(config, "LIVE_MODE", False), \
             patch("bot.run_bullpen_json") as mock_bullpen, \
             patch("bot.append_log") as mock_log, \
             patch("bot.check_circuit_breaker") as mock_cb:
            bot.close_position_zombie_dump(key, "0xTrader", "Trader", "some-market", "Yes",
                                            positions, 0.10, {}, {}, age_hours=25.0)
        mock_bullpen.assert_not_called()
        self.assertNotIn(key, positions)
        mock_cb.assert_called_once()
        close_event = next(c.args[0] for c in mock_log.call_args_list
                            if c.args[0]["event_type"] == "paper_sell_zombie_dump")
        # 10 shares * 0.10 - 5.0 cost basis = -4.0
        self.assertAlmostEqual(close_event["pnl_usd"], -4.0)
        self.assertEqual(close_event["age_hours"], 25.0)

    def test_live_mode_never_checks_spread_tolerance(self):
        key = "0xTrader|some-market|Yes"
        positions = {key: self._position()}
        with patch.object(config, "LIVE_MODE", True), \
             patch.object(config, "ZOMBIE_EXIT_MAX_SLIPPAGE", 0.25), \
             patch("bot.check_spread_tolerance") as mock_spread, \
             patch("bot.require_filled", return_value={"price": "0.08"}), \
             patch("bot.extract_fill_price", return_value=0.08), \
             patch("bot.run_bullpen_json", return_value={}), \
             patch("bot.append_log"), \
             patch("bot.check_circuit_breaker"):
            bot.close_position_zombie_dump(key, "0xTrader", "Trader", "some-market", "Yes",
                                            positions, 0.10, {}, {}, age_hours=25.0)
        mock_spread.assert_not_called()

    def test_live_mode_min_price_uses_zombie_slippage_not_the_normal_tolerance(self):
        key = "0xTrader|some-market|Yes"
        positions = {key: self._position()}
        captured_args = {}

        def _capture_bullpen(args, **kwargs):
            captured_args["args"] = args
            return {"price": "0.075"}

        with patch.object(config, "LIVE_MODE", True), \
             patch.object(config, "ZOMBIE_EXIT_MAX_SLIPPAGE", 0.25), \
             patch.object(config, "SLIPPAGE_TOLERANCE", 0.05), \
             patch("bot.run_bullpen_json", side_effect=_capture_bullpen), \
             patch("bot.require_filled", side_effect=lambda resp, label: resp), \
             patch("bot.extract_fill_price", return_value=0.075), \
             patch("bot.append_log"), \
             patch("bot.check_circuit_breaker"):
            bot.close_position_zombie_dump(key, "0xTrader", "Trader", "some-market", "Yes",
                                            positions, 0.10, {}, {}, age_hours=25.0)
        min_price_idx = captured_args["args"].index("--min-price") + 1
        # 0.10 * (1 - 0.25) = 0.075, NOT 0.10 * (1 - 0.05) = 0.095.
        self.assertAlmostEqual(float(captured_args["args"][min_price_idx]), 0.075)

    def test_failed_sell_leaves_the_position_open_for_the_next_sweep(self):
        key = "0xTrader|some-market|Yes"
        positions = {key: self._position()}
        with patch.object(config, "LIVE_MODE", True), \
             patch("bot.run_bullpen_json", side_effect=RuntimeError("bullpen sell exited 4")), \
             patch("bot.append_log") as mock_log, \
             patch("bot.check_circuit_breaker") as mock_cb:
            bot.close_position_zombie_dump(key, "0xTrader", "Trader", "some-market", "Yes",
                                            positions, 0.10, {}, {}, age_hours=25.0)
        self.assertIn(key, positions)  # NOT removed -- next sweep retries
        mock_cb.assert_not_called()
        logged_types = [c.args[0]["event_type"] for c in mock_log.call_args_list]
        self.assertIn("failed_trade", logged_types)


class TestMaybeSnapshotDailyPortfolio(unittest.TestCase):
    """Grafana personal-dashboard daily snapshot (2026-07-28) — mocks every
    DB/risk_manager boundary, same style as the zombie-position tests
    above. Real timezone/idempotency behavior is covered directly in
    test_db_daily_snapshot.py; this class only checks bot.py's own wiring
    (trigger-hour gate, piggybacking on prices_by_key, active_traders_
    followed = tracked minus muted, never crashing the caller).
    """

    def _breakdown(self):
        return {"total_equity": 1234.56, "total_cash": 500.0, "total_unrealized_pnl": 50.0}

    def test_does_nothing_before_the_trigger_hour(self):
        before_trigger = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)  # well before hour 23
        with patch.object(config, "DAILY_SNAPSHOT_TRIGGER_HOUR_UTC", 23), \
             patch("bot.datetime") as mock_dt, \
             patch("bot.has_snapshot_for_today") as mock_has_snapshot, \
             patch("bot.record_daily_snapshot") as mock_record:
            mock_dt.now.return_value = before_trigger
            bot.maybe_snapshot_daily_portfolio({}, {}, {"0xa": "n"}, {})
        mock_has_snapshot.assert_not_called()  # never even checks -- too early
        mock_record.assert_not_called()

    def test_does_nothing_if_already_snapshotted_today(self):
        at_trigger = datetime(2026, 7, 28, 23, 30, tzinfo=timezone.utc)
        with patch.object(config, "DAILY_SNAPSHOT_TRIGGER_HOUR_UTC", 23), \
             patch("bot.datetime") as mock_dt, \
             patch("bot.has_snapshot_for_today", return_value=True), \
             patch("bot.record_daily_snapshot") as mock_record:
            mock_dt.now.return_value = at_trigger
            bot.maybe_snapshot_daily_portfolio({}, {}, {"0xa": "n"}, {})
        mock_record.assert_not_called()

    def test_records_a_snapshot_past_the_trigger_hour_when_not_yet_done_today(self):
        at_trigger = datetime(2026, 7, 28, 23, 30, tzinfo=timezone.utc)
        with patch.object(config, "DAILY_SNAPSHOT_TRIGGER_HOUR_UTC", 23), \
             patch("bot.datetime") as mock_dt, \
             patch("bot.has_snapshot_for_today", return_value=False), \
             patch("bot.risk_manager.compute_equity_breakdown", return_value=self._breakdown()), \
             patch("bot.realized_pnl_total", return_value=999.0), \
             patch("bot.realized_pnl_today", return_value=42.0), \
             patch("bot.record_daily_snapshot") as mock_record, \
             patch("bot.append_log"):
            mock_dt.now.return_value = at_trigger
            tracked = {"0xa": "n1", "0xb": "n2", "0xc": "n3"}
            muted = {"0xb": {}}
            bot.maybe_snapshot_daily_portfolio({}, {"key": 0.5}, tracked, muted)
        mock_record.assert_called_once()
        call_kwargs = mock_record.call_args.kwargs
        self.assertAlmostEqual(call_kwargs["total_equity"], 1234.56)
        self.assertAlmostEqual(call_kwargs["total_cash"], 500.0)
        self.assertAlmostEqual(call_kwargs["total_unrealized_pnl"], 50.0)
        self.assertAlmostEqual(call_kwargs["realized_pnl_today"], 42.0)
        self.assertEqual(call_kwargs["active_traders_followed"], 2)  # 3 tracked - 1 muted

    def test_a_failure_inside_does_not_propagate_uncaught(self):
        # The main-loop call site wraps this in its own try/except, but the
        # function itself should still be well-behaved: a DB error here
        # must not be silently swallowed in a way that hides it either --
        # confirmed by NOT catching internally, letting the caller's own
        # try/except (already tested at the call site's own log line) see it.
        at_trigger = datetime(2026, 7, 28, 23, 30, tzinfo=timezone.utc)
        with patch.object(config, "DAILY_SNAPSHOT_TRIGGER_HOUR_UTC", 23), \
             patch("bot.datetime") as mock_dt, \
             patch("bot.has_snapshot_for_today", return_value=False), \
             patch("bot.risk_manager.compute_equity_breakdown", side_effect=RuntimeError("boom")):
            mock_dt.now.return_value = at_trigger
            with self.assertRaises(RuntimeError):
                bot.maybe_snapshot_daily_portfolio({}, {}, {"0xa": "n"}, {})


if __name__ == "__main__":
    unittest.main()
