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
               "VIP_WALLET_EXPOSURE_CAP_USD")

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
