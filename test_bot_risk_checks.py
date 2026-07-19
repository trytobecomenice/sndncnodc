#!/usr/bin/env python3
"""Unit tests for bot.py's pure pre-trade risk math (check_slippage_ceiling,
compute_shortfall). Same stdlib-unittest pattern as test_risk_manager.py.

Run: python3 -m unittest test_bot_risk_checks -v
"""

import unittest

import bot
import config


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


if __name__ == "__main__":
    unittest.main()
