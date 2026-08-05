#!/usr/bin/env python3
"""Unit tests for risk_manager.py's pure decision logic.

Run: python3 -m unittest test_risk_manager -v

Stdlib unittest on purpose — the Python side of this repo has no
third-party dependencies (see requirements.txt) and these are pure
functions, so nothing more is needed. Config limits are patched per-test
and restored in tearDown so tests never depend on (or leak into) the real
config values.
"""

import unittest

import config
import risk_manager


def pos(cost_basis, shares=None):
    return {"cost_basis_usd": cost_basis, "shares": shares if shares is not None else cost_basis * 2}


class ConfigPatchingTestCase(unittest.TestCase):
    """Snapshots and restores every risk-related config value around each test."""

    PATCHED = ("MAX_TOTAL_EXPOSURE_USD", "MAX_EVENT_EXPOSURE_USD", "MAX_WALLET_EXPOSURE_USD",
               "PAPER_BANKROLL_USD", "EQUITY_FLOOR_USD", "MAX_DRAWDOWN_FROM_PEAK_USD",
               "VIP_WALLET_EXPOSURE_CAP_USD", "EQUITY_MARK_MIN_ENTRY_PRICE",
               "WALLET_EV_CAP_SCALE", "WALLET_EV_CAP_MIN_USD", "WALLET_EV_CAP_MAX_USD",
               "WALLET_EV_CAP_CLIP_PCT", "KELLY_SHRINKAGE_PSEUDO_COUNT")

    def setUp(self):
        self._saved = {name: getattr(config, name) for name in self.PATCHED}

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(config, name, value)


class TestExposure(ConfigPatchingTestCase):
    def test_total_exposure_sums_cost_basis(self):
        positions = {"a|m1|Yes": pos(5.0), "b|m2|No": pos(10.0)}
        self.assertAlmostEqual(risk_manager.total_exposure_usd(positions), 15.0)

    def test_total_exposure_empty_book_is_zero(self):
        self.assertEqual(risk_manager.total_exposure_usd({}), 0.0)

    def test_event_exposure_only_counts_matching_event(self):
        positions = {
            "a|btc-hit-70k|Yes": pos(5.0),
            "b|btc-hit-80k|Yes": pos(5.0),
            "c|election-x|Yes": pos(5.0),
        }
        market_to_event = {"btc-hit-70k": "btc-july", "btc-hit-80k": "btc-july",
                           "election-x": "election"}
        self.assertAlmostEqual(
            risk_manager.event_exposure_usd(positions, market_to_event, "btc-july"), 10.0)

    def test_event_exposure_ignores_unresolved_markets(self):
        positions = {"a|legacy-market|Yes": pos(5.0)}
        self.assertEqual(risk_manager.event_exposure_usd(positions, {}, "some-event"), 0.0)

    def test_wallet_exposure_only_counts_matching_trader(self):
        positions = {
            "0xAAA|m1|Yes": pos(5.0),
            "0xAAA|m2|No": pos(5.0),
            "0xBBB|m1|Yes": pos(5.0),  # same market, different trader
        }
        self.assertAlmostEqual(risk_manager.wallet_exposure_usd(positions, "0xAAA"), 10.0)

    def test_wallet_exposure_empty_book_is_zero(self):
        self.assertEqual(risk_manager.wallet_exposure_usd({}, "0xAAA"), 0.0)

    def test_wallet_exposure_ignores_other_wallets_entirely(self):
        positions = {"0xBBB|m1|Yes": pos(100.0)}
        self.assertEqual(risk_manager.wallet_exposure_usd(positions, "0xAAA"), 0.0)

    def test_wallet_exposure_matches_regardless_of_casing(self):
        # 2026-07-26 regression: the same real wallet split across two
        # casings (checksummed vs. lowercase) must still sum to ONE total,
        # not silently undercount whichever casing wasn't queried for.
        positions = {
            "0xAaAa|m1|Yes": pos(30.0),
            "0xaaaa|m2|No": pos(20.0),
        }
        self.assertAlmostEqual(risk_manager.wallet_exposure_usd(positions, "0xAAAA"), 50.0)
        self.assertAlmostEqual(risk_manager.wallet_exposure_usd(positions, "0xaaaa"), 50.0)


class TestVipWalletExposureCap(ConfigPatchingTestCase):
    """VIP per-wallet exposure cap override, 2026-07-26 (Rule 35)."""

    def setUp(self):
        super().setUp()
        config.MAX_WALLET_EXPOSURE_USD = 50.0
        config.VIP_WALLET_EXPOSURE_CAP_USD = {"0xvip": 150.0}

    def test_vip_wallet_gets_override_cap(self):
        self.assertEqual(risk_manager.wallet_exposure_cap_usd("0xVIP"), 150.0)

    def test_non_vip_wallet_gets_flat_cap(self):
        self.assertEqual(risk_manager.wallet_exposure_cap_usd("0xSomeoneElse"), 50.0)

    def test_none_wallet_gets_flat_cap(self):
        self.assertEqual(risk_manager.wallet_exposure_cap_usd(None), 50.0)

    def test_check_buy_allows_vip_wallet_past_the_flat_cap(self):
        positions = {"0xvip|m1|Yes": pos(120.0)}
        # $120 existing + $20 new = $140, over the flat $50 cap but under
        # the VIP override of $150 -- must be allowed.
        ok, event_type, reason = risk_manager.check_buy(
            positions, {}, "ev", 20.0, None, wallet_address="0xvip")
        self.assertTrue(ok)

    def test_check_buy_still_blocks_vip_wallet_past_its_own_cap(self):
        positions = {"0xvip|m1|Yes": pos(140.0)}
        ok, event_type, reason = risk_manager.check_buy(
            positions, {}, "ev", 20.0, None, wallet_address="0xvip")
        self.assertFalse(ok)
        self.assertEqual(event_type, "skip_risk_wallet_cap")
        self.assertIn("150.00", reason)

    def test_check_buy_blocks_non_vip_wallet_at_the_flat_cap_unchanged(self):
        positions = {"0xregular|m1|Yes": pos(40.0)}
        ok, event_type, reason = risk_manager.check_buy(
            positions, {}, "ev", 20.0, None, wallet_address="0xregular")
        self.assertFalse(ok)
        self.assertEqual(event_type, "skip_risk_wallet_cap")


class TestComputeWalletEvCapUsd(ConfigPatchingTestCase):
    """Automatic EV-scaled per-wallet exposure cap (2026-07-31) — replaces
    config.VIP_WALLET_EXPOSURE_CAP_USD's manual, one-time curation, found
    stale by 5 days the next time anyone checked it (a wallet's real sample
    had grown 31->200 trades and its EV +44.6%->+70.3%, cap frozen at $150
    the whole time). Investigated live the same night: of $699.63 total
    realized PnL across every tracked wallet, $907.65 came from ONE wallet
    -- without it the bot's whole track record would be net NEGATIVE
    (-$208.02) -- which is exactly why WALLET_EV_CAP_MAX_USD exists as a
    hard ceiling, not just "let the strongest wallet's cap grow unbounded."
    """

    def setUp(self):
        super().setUp()
        config.MAX_WALLET_EXPOSURE_USD = 50.0
        config.WALLET_EV_CAP_SCALE = 1.0
        config.WALLET_EV_CAP_MIN_USD = 20.0
        config.WALLET_EV_CAP_MAX_USD = 200.0
        config.WALLET_EV_CAP_CLIP_PCT = 1.0
        config.KELLY_SHRINKAGE_PSEUDO_COUNT = 25

    def test_no_trade_count_returns_flat_base_cap(self):
        self.assertEqual(risk_manager.compute_wallet_ev_cap_usd(0.703, None), 50.0)

    def test_zero_trade_count_returns_flat_base_cap(self):
        self.assertEqual(risk_manager.compute_wallet_ev_cap_usd(0.703, 0), 50.0)

    def test_none_ev_pct_returns_flat_base_cap(self):
        self.assertEqual(risk_manager.compute_wallet_ev_cap_usd(None, 200), 50.0)

    def test_strong_positive_ev_with_large_sample_raises_the_cap(self):
        # Real data: n=200, ev=+70.3% -> shrunk_ev = 200*0.703/225 = 0.625
        # -> cap = 50 + 1.0*0.625*50 = 81.25
        cap = risk_manager.compute_wallet_ev_cap_usd(0.703, 200)
        self.assertAlmostEqual(cap, 81.25, places=1)

    def test_negative_ev_with_large_sample_lowers_the_cap_below_base(self):
        # Real data: n=97, ev=-13.2% -> shrunk_ev = 97*-0.132/122 = -0.105
        # -> cap = 50 + 1.0*-0.105*50 = 44.74
        cap = risk_manager.compute_wallet_ev_cap_usd(-0.132, 97)
        self.assertLess(cap, 50.0)
        self.assertAlmostEqual(cap, 44.74, places=1)

    def test_tiny_sample_barely_moves_the_cap_regardless_of_extreme_ev(self):
        # Real data: n=1, ev=+1400% -- even fully un-clipped this would be
        # a huge number; clipped to +/-100% first, then n=1 shrinks it to
        # almost nothing: shrunk_ev = 1*1.0/26 = 0.0385 -> cap ~= 51.9.
        cap = risk_manager.compute_wallet_ev_cap_usd(14.0, 1)
        self.assertLess(cap, 55.0)
        self.assertGreater(cap, 50.0)

    def test_extreme_ev_is_clipped_before_shrinking(self):
        # Without clipping, n=1 ev=1400% would shrink to 1*14/26=0.538 ->
        # cap=76.9. With clipping to +/-100%, it's 1*1.0/26=0.0385 -> ~51.9.
        # Confirms the clip actually applies, not just present in the signature.
        clipped_cap = risk_manager.compute_wallet_ev_cap_usd(14.0, 1)
        self.assertLess(clipped_cap, 60.0)

    def test_extreme_negative_ev_with_large_sample_clamps_to_min_cap(self):
        # Real data: n=40, ev=-100% (every closed trade a total loss) ->
        # shrunk_ev = 40*-1.0/65 = -0.615 -> cap = 50-30.75=19.25, clamped
        # up to the MIN_CAP floor of 20.
        cap = risk_manager.compute_wallet_ev_cap_usd(-1.0, 40)
        self.assertEqual(cap, 20.0)

    def test_extreme_positive_ev_with_large_sample_clamps_to_max_cap(self):
        config.WALLET_EV_CAP_MAX_USD = 60.0  # a tight ceiling for this test
        cap = risk_manager.compute_wallet_ev_cap_usd(1.0, 1000)  # huge n, ev clipped to 100%
        self.assertEqual(cap, 60.0)

    def test_scale_zero_always_returns_the_flat_base_cap(self):
        config.WALLET_EV_CAP_SCALE = 0.0
        self.assertAlmostEqual(risk_manager.compute_wallet_ev_cap_usd(0.703, 200), 50.0)


class TestWalletExposureCapUsdWithEvStats(ConfigPatchingTestCase):
    """wallet_exposure_cap_usd()'s priority order (2026-07-31): manual VIP
    override > automatic EV-scaled formula > flat default."""

    def setUp(self):
        super().setUp()
        config.MAX_WALLET_EXPOSURE_USD = 50.0
        config.VIP_WALLET_EXPOSURE_CAP_USD = {}
        config.WALLET_EV_CAP_SCALE = 1.0
        config.WALLET_EV_CAP_MIN_USD = 20.0
        config.WALLET_EV_CAP_MAX_USD = 200.0
        config.WALLET_EV_CAP_CLIP_PCT = 1.0

    def test_no_ev_stats_at_all_falls_back_to_flat_cap(self):
        self.assertEqual(risk_manager.wallet_exposure_cap_usd("0xabc"), 50.0)

    def test_wallet_missing_from_ev_stats_falls_back_to_flat_cap(self):
        ev_stats = {"0xother": {"ev_pct": 0.7, "trade_count": 200}}
        self.assertEqual(risk_manager.wallet_exposure_cap_usd("0xabc", ev_stats=ev_stats), 50.0)

    def test_wallet_in_ev_stats_gets_the_computed_formula_cap(self):
        ev_stats = {"0xabc": {"ev_pct": 0.703, "trade_count": 200}}
        cap = risk_manager.wallet_exposure_cap_usd("0xabc", ev_stats=ev_stats)
        self.assertAlmostEqual(cap, 81.25, places=1)

    def test_ev_stats_lookup_is_case_insensitive(self):
        ev_stats = {"0xabc": {"ev_pct": 0.703, "trade_count": 200}}
        cap = risk_manager.wallet_exposure_cap_usd("0xABC", ev_stats=ev_stats)
        self.assertAlmostEqual(cap, 81.25, places=1)

    def test_manual_vip_override_still_wins_over_the_formula(self):
        config.VIP_WALLET_EXPOSURE_CAP_USD = {"0xabc": 999.0}
        ev_stats = {"0xabc": {"ev_pct": 0.703, "trade_count": 200}}
        self.assertEqual(risk_manager.wallet_exposure_cap_usd("0xabc", ev_stats=ev_stats), 999.0)

    def test_check_buy_uses_the_ev_scaled_cap(self):
        ev_stats = {"0xabc": {"ev_pct": 0.703, "trade_count": 200}}
        positions = {"0xabc|m1|Yes": pos(70.0)}
        # $70 existing + $10 new = $80, over the flat $50 but under the
        # formula's ~$81.25 -- must be allowed.
        ok, event_type, reason = risk_manager.check_buy(
            positions, {}, "ev", 10.0, None, wallet_address="0xabc", wallet_ev_stats=ev_stats)
        self.assertTrue(ok)


class TestDepthCappedTradeSizeUsd(unittest.TestCase):
    """Depth-Aware Trade Sizing, 2026-07-28. Pure function -- no config to
    patch, depth_fraction is passed explicitly at every call site."""

    def test_uncapped_when_trade_fits_within_depth_fraction(self):
        # $5 trade, 5% of $200 depth = $10 -- fits, unchanged.
        self.assertEqual(risk_manager.depth_capped_trade_size_usd(5.0, 200.0, 0.05), 5.0)

    def test_clamps_when_trade_exceeds_depth_fraction(self):
        # $10 trade, 5% of $100 depth = $5 -- clamps down to $5.
        self.assertEqual(risk_manager.depth_capped_trade_size_usd(10.0, 100.0, 0.05), 5.0)

    def test_landing_exactly_on_the_depth_fraction_is_unclamped(self):
        # $5 trade, 5% of $100 depth = exactly $5 -- not a clamp, same "would
        # exceed" semantics as check_buy's other limits.
        self.assertEqual(risk_manager.depth_capped_trade_size_usd(5.0, 100.0, 0.05), 5.0)

    def test_none_book_depth_returns_trade_size_unclamped(self):
        # Fetch failure/unavailable -- fail-open, never invents a worse
        # number or treats unknown as zero liquidity.
        self.assertEqual(risk_manager.depth_capped_trade_size_usd(7.5, None, 0.05), 7.5)

    def test_zero_book_depth_clamps_to_zero(self):
        # A real, successfully-fetched book with genuinely zero visible
        # depth (distinct from None/unavailable) correctly clamps to 0 --
        # this IS real information, not a fetch failure.
        self.assertEqual(risk_manager.depth_capped_trade_size_usd(5.0, 0.0, 0.05), 0.0)


class TestCheckBuy(ConfigPatchingTestCase):
    def setUp(self):
        super().setUp()
        config.MAX_TOTAL_EXPOSURE_USD = 20.0
        config.MAX_EVENT_EXPOSURE_USD = 10.0
        config.MAX_WALLET_EXPOSURE_USD = 15.0

    def test_allows_when_all_limits_clear(self):
        ok, event_type, reason = risk_manager.check_buy({}, {}, "ev", 5.0, None)
        self.assertTrue(ok)
        self.assertIsNone(event_type)
        self.assertIsNone(reason)

    def test_kill_switch_blocks_everything_first(self):
        # Even an empty book with zero exposure is blocked when latched.
        ks = {"triggered_at": "2026-07-18T00:00:00Z", "reasons": ["equity below floor"]}
        ok, event_type, reason = risk_manager.check_buy({}, {}, "ev", 5.0, ks)
        self.assertFalse(ok)
        self.assertEqual(event_type, "skip_risk_kill_switch")
        self.assertIn("equity below floor", reason)
        self.assertIn("reset_kill_switch", reason)

    def test_entry_interlock_blocks_new_buy_but_is_not_a_persistent_kill(self):
        interlock = {
            "active": True,
            "status": "interlocked",
            "reasons": ["event_loop_lag_exceeded"],
        }
        ok, event_type, reason = risk_manager.check_buy(
            {}, {}, "ev", 5.0, None, entry_interlock=interlock
        )
        self.assertFalse(ok)
        self.assertEqual(event_type, "skip_risk_entry_interlock")
        self.assertIn("event_loop_lag_exceeded", reason)
        self.assertIn("exits continue", reason)

    def test_healthy_entry_interlock_value_does_not_block(self):
        ok, event_type, reason = risk_manager.check_buy(
            {}, {}, "ev", 5.0, None,
            entry_interlock={"active": False, "status": "healthy"},
        )
        self.assertTrue(ok)
        self.assertIsNone(event_type)
        self.assertIsNone(reason)

    def test_malformed_persisted_interlock_state_fails_closed(self):
        ok, event_type, reason = risk_manager.check_buy(
            {}, {}, "ev", 5.0, None, entry_interlock={"active": "unknown"}
        )
        self.assertFalse(ok)
        self.assertEqual(event_type, "skip_risk_entry_interlock")
        self.assertIn("health state is not safe", reason)

    def test_ceiling_blocks_the_first_dollar_past_the_limit(self):
        positions = {"a|m|Yes": pos(16.0)}  # 16 + 5 = 21 > 20
        ok, event_type, _ = risk_manager.check_buy(positions, {"m": "other-ev"}, "ev", 5.0, None)
        self.assertFalse(ok)
        self.assertEqual(event_type, "skip_risk_exposure_ceiling")

    def test_landing_exactly_on_the_ceiling_is_allowed(self):
        positions = {"a|m|Yes": pos(15.0)}  # 15 + 5 == 20, not over
        ok, _, _ = risk_manager.check_buy(positions, {"m": "other-ev"}, "ev", 5.0, None)
        self.assertTrue(ok)

    def test_event_cap_blocks_same_event_concentration(self):
        positions = {"a|m1|Yes": pos(6.0)}  # 6 + 5 = 11 > 10 in same event
        ok, event_type, reason = risk_manager.check_buy(
            positions, {"m1": "ev"}, "ev", 5.0, None)
        self.assertFalse(ok)
        self.assertEqual(event_type, "skip_risk_event_cap")
        self.assertIn("ev", reason)

    def test_event_cap_ignores_other_events(self):
        positions = {"a|m1|Yes": pos(6.0)}
        ok, _, _ = risk_manager.check_buy(positions, {"m1": "other-ev"}, "ev", 5.0, None)
        self.assertTrue(ok)

    def test_wallet_cap_blocks_same_wallet_concentration_across_different_events(self):
        # 0xAAA already has $11 deployed in event "other-ev" (a different
        # event from this BUY's "ev") — the wallet cap must catch this even
        # though the per-event cap (scoped to "ev") wouldn't.
        positions = {"0xAAA|m1|Yes": pos(11.0)}  # 11 + 5 = 16 > 15
        ok, event_type, reason = risk_manager.check_buy(
            positions, {"m1": "other-ev"}, "ev", 5.0, None, wallet_address="0xAAA")
        self.assertFalse(ok)
        self.assertEqual(event_type, "skip_risk_wallet_cap")
        self.assertIn("0xAAA", reason)

    def test_wallet_cap_ignores_other_wallets(self):
        positions = {"0xBBB|m1|Yes": pos(11.0)}
        ok, _, _ = risk_manager.check_buy(
            positions, {"m1": "other-ev"}, "ev", 5.0, None, wallet_address="0xAAA")
        self.assertTrue(ok)

    def test_wallet_cap_is_skipped_when_no_wallet_address_given(self):
        # Backward compatibility: an existing call site that doesn't pass
        # wallet_address (the parameter defaults to None) must not suddenly
        # start failing just because MAX_WALLET_EXPOSURE_USD is configured.
        positions = {"0xAAA|m1|Yes": pos(11.0)}
        ok, _, _ = risk_manager.check_buy(positions, {"m1": "other-ev"}, "ev", 5.0, None)
        self.assertTrue(ok)

    def test_landing_exactly_on_the_wallet_cap_is_allowed(self):
        positions = {"0xAAA|m1|Yes": pos(10.0)}  # 10 + 5 == 15, not over
        ok, _, _ = risk_manager.check_buy(
            positions, {"m1": "other-ev"}, "ev", 5.0, None, wallet_address="0xAAA")
        self.assertTrue(ok)

    def test_none_disables_a_limit(self):
        config.MAX_TOTAL_EXPOSURE_USD = None
        config.MAX_EVENT_EXPOSURE_USD = None
        config.MAX_WALLET_EXPOSURE_USD = None
        positions = {"a|m1|Yes": pos(10_000.0)}
        ok, _, _ = risk_manager.check_buy(
            positions, {"m1": "ev"}, "ev", 5.0, None, wallet_address="a")
        self.assertTrue(ok)


class TestEquity(ConfigPatchingTestCase):
    def setUp(self):
        super().setUp()
        config.PAPER_BANKROLL_USD = 125.0
        config.EQUITY_FLOOR_USD = 100.0
        config.MAX_DRAWDOWN_FROM_PEAK_USD = 50.0

    def test_compute_equity_marks_priced_positions(self):
        # 10 shares @ entry cost $5, now priced 0.80 -> value 8.0, unrealized +3.0
        positions = {"a|m|Yes": {"cost_basis_usd": 5.0, "shares": 10.0}}
        equity = risk_manager.compute_equity(positions, {"a|m|Yes": 0.80}, realized_pnl=7.0)
        self.assertAlmostEqual(equity, 125.0 + 7.0 + 3.0)

    def test_compute_equity_carries_unpriced_positions_at_cost(self):
        positions = {"a|m|Yes": {"cost_basis_usd": 5.0, "shares": 10.0}}
        equity = risk_manager.compute_equity(positions, {}, realized_pnl=0.0)
        self.assertAlmostEqual(equity, 125.0)  # zero unrealized, not a guess

    def test_compute_equity_breakdown_matches_compute_equity_total(self):
        # The 2026-07-28 refactor split compute_equity's internals out for
        # the Grafana snapshot breakdown -- the combined total must still
        # agree exactly with the unchanged compute_equity.
        positions = {"a|m|Yes": {"cost_basis_usd": 5.0, "shares": 10.0}}
        prices = {"a|m|Yes": 0.80}
        equity = risk_manager.compute_equity(positions, prices, realized_pnl=7.0)
        breakdown = risk_manager.compute_equity_breakdown(positions, prices, realized_pnl=7.0)
        self.assertAlmostEqual(breakdown["total_equity"], equity)

    def test_compute_equity_breakdown_unrealized_and_cash_components(self):
        # 10 shares @ cost $5, priced 0.80 -> mark value 8.0, unrealized +3.0.
        # cash = bankroll(125) + realized(7) - deployed cost basis(5) = 127.
        positions = {"a|m|Yes": {"cost_basis_usd": 5.0, "shares": 10.0}}
        breakdown = risk_manager.compute_equity_breakdown(positions, {"a|m|Yes": 0.80}, realized_pnl=7.0)
        self.assertAlmostEqual(breakdown["total_unrealized_pnl"], 3.0)
        self.assertAlmostEqual(breakdown["total_cash"], 127.0)
        self.assertAlmostEqual(breakdown["total_equity"], 135.0)  # 125 + 7 + 3

    def test_extreme_tail_longshot_is_carried_at_cost_despite_a_priced_quote(self):
        """Regression for a live production incident (2026-07-31): a handful
        of ultra-longshot positions (avg_entry_price 0.001-0.008, $5-10 cost
        basis, thousands of implied shares) coincided with a single bad/stale
        CLOB read swinging the kill switch's equity by ~$4900-5000 in one
        sweep -- a tiny cost basis amplified into a huge phantom gain. Must
        be carried at cost (zero unrealized contribution) regardless of
        whatever price the sweep reports, not just when unpriced."""
        # 5000 shares @ $0.002 entry ($10 cost) -- if genuinely marked at a
        # bad/stale $1.00 this would otherwise contribute +$4990 unrealized.
        positions = {"a|m|Yes": {"cost_basis_usd": 10.0, "shares": 5000.0,
                                  "avg_entry_price": 0.002}}
        equity = risk_manager.compute_equity(positions, {"a|m|Yes": 1.0}, realized_pnl=0.0)
        self.assertAlmostEqual(equity, 125.0)  # carried at cost, not +$4990

    def test_extreme_tail_on_the_high_side_is_also_carried_at_cost(self):
        # avg_entry_price 0.995 -- the symmetric high-side tail.
        positions = {"a|m|Yes": {"cost_basis_usd": 10.0, "shares": 10.05,
                                  "avg_entry_price": 0.995}}
        equity = risk_manager.compute_equity(positions, {"a|m|Yes": 0.0}, realized_pnl=0.0)
        self.assertAlmostEqual(equity, 125.0)  # carried at cost, not -$10

    def test_position_just_inside_the_tail_threshold_is_still_marked_normally(self):
        # avg_entry_price 0.03 is outside the 0.02 exclusion band -- normal
        # mark-to-market must still apply, confirming this isn't a blanket
        # "small positions don't count" rule.
        positions = {"a|m|Yes": {"cost_basis_usd": 5.0, "shares": 10.0,
                                  "avg_entry_price": 0.03}}
        equity = risk_manager.compute_equity(positions, {"a|m|Yes": 0.80}, realized_pnl=0.0)
        self.assertAlmostEqual(equity, 125.0 + 3.0)

    def test_missing_avg_entry_price_falls_back_to_normal_marking(self):
        # Legacy positions with no avg_entry_price recorded must not be
        # silently excluded -- config.py's docstring only excludes KNOWN
        # extreme-tail entries, not positions missing the field entirely.
        positions = {"a|m|Yes": {"cost_basis_usd": 5.0, "shares": 10.0}}
        equity = risk_manager.compute_equity(positions, {"a|m|Yes": 0.80}, realized_pnl=0.0)
        self.assertAlmostEqual(equity, 125.0 + 3.0)

    def test_compute_equity_breakdown_with_no_positions(self):
        breakdown = risk_manager.compute_equity_breakdown({}, {}, realized_pnl=10.0)
        self.assertAlmostEqual(breakdown["total_unrealized_pnl"], 0.0)
        self.assertAlmostEqual(breakdown["total_cash"], 135.0)  # 125 + 10, nothing deployed
        self.assertAlmostEqual(breakdown["total_equity"], 135.0)

    def test_hwm_seeds_at_first_equity_and_only_ratchets_up(self):
        hwm, triggers = risk_manager.evaluate_equity(232.0, None)
        self.assertEqual(hwm, 232.0)
        self.assertEqual(triggers, [])
        hwm, _ = risk_manager.evaluate_equity(220.0, hwm)  # dip: HWM must not fall
        self.assertEqual(hwm, 232.0)
        hwm, _ = risk_manager.evaluate_equity(240.0, hwm)  # new peak
        self.assertEqual(hwm, 240.0)

    def test_floor_trigger_fires_below_floor(self):
        _, triggers = risk_manager.evaluate_equity(99.99, 99.99)
        self.assertEqual(len(triggers), 1)
        self.assertIn("below floor", triggers[0])

    def test_drawdown_trigger_fires_past_max_drawdown_from_peak(self):
        # Peak 232, equity 181 -> drawdown 51 > 50: fires. Floor (100) does not.
        _, triggers = risk_manager.evaluate_equity(181.0, 232.0)
        self.assertEqual(len(triggers), 1)
        self.assertIn("below peak", triggers[0])

    def test_drawdown_exactly_at_limit_does_not_fire(self):
        _, triggers = risk_manager.evaluate_equity(182.0, 232.0)  # exactly -50
        self.assertEqual(triggers, [])

    def test_both_triggers_can_fire_together(self):
        _, triggers = risk_manager.evaluate_equity(95.0, 232.0)
        self.assertEqual(len(triggers), 2)

    def test_none_disables_each_trigger_independently(self):
        config.EQUITY_FLOOR_USD = None
        _, triggers = risk_manager.evaluate_equity(50.0, 60.0)
        self.assertEqual(triggers, [])  # floor off; drawdown only -10, under 50
        config.MAX_DRAWDOWN_FROM_PEAK_USD = None
        _, triggers = risk_manager.evaluate_equity(1.0, 500.0)
        self.assertEqual(triggers, [])  # both off — nothing can fire


if __name__ == "__main__":
    unittest.main()
