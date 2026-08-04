#!/usr/bin/env python3
import unittest
from unittest.mock import patch

import bot
import config
import manage_challengers
import risk_manager


class TestDrawdownWarning(unittest.TestCase):
    def test_trigger_hysteresis_and_clear(self):
        with patch.object(config, "MAX_DRAWDOWN_FROM_PEAK_USD", 100.0), \
             patch.object(config, "DRAWDOWN_WARNING_FRACTION", 0.75), \
             patch.object(config, "DRAWDOWN_WARNING_RECOVERY_FRACTION", 0.60):
            warning, transition = risk_manager.evaluate_drawdown_warning(
                920.0, 1000.0, now_iso="now",
            )
            self.assertEqual(transition, "triggered")
            self.assertEqual(warning["drawdown_usd"], 80.0)
            warning, transition = risk_manager.evaluate_drawdown_warning(935.0, 1000.0, warning)
            self.assertIsNone(transition)  # still above the 60% recovery line
            warning, transition = risk_manager.evaluate_drawdown_warning(945.0, 1000.0, warning)
            self.assertEqual(transition, "cleared")
            self.assertIsNone(warning)

    def test_warning_blocks_buy_but_not_via_hard_kill_switch(self):
        warning = {"triggered_at": "now", "equity": 920.0, "drawdown_usd": 80.0}
        with patch.object(config, "DRAWDOWN_WARNING_PAUSE_BUYS", True), \
             patch.object(config, "DRAWDOWN_WARNING_RECOVERY_FRACTION", 0.60):
            ok, event_type, reason = risk_manager.check_buy(
                {}, {}, "event", 5.0, None, drawdown_warning=warning,
            )
        self.assertFalse(ok)
        self.assertEqual(event_type, "skip_risk_drawdown_warning")
        self.assertIn("resume automatically", reason)


class TestStartupCircuitBreakerAudit(unittest.TestCase):
    def test_identical_losses_are_muted_on_startup(self):
        performance = {"0xabc": {"recent_returns": [-1.0] * 10}}
        muted = {}
        tracked = {"0xabc": ("0xabc", "strict-5")}
        with patch.object(config, "REEVALUATE_CIRCUIT_BREAKERS_ON_STARTUP", True), \
             patch.object(config, "MUTE_EV_MIN_SAMPLES", 10), \
             patch("bot.append_log") as log:
            result = bot.reevaluate_circuit_breakers_on_startup(performance, muted, tracked)
        self.assertEqual(result, ["0xabc"])
        self.assertIn("0xabc", muted)
        self.assertEqual(log.call_args.args[0]["event_type"], "trader_muted_startup_audit")


class TestChallengerEvidence(unittest.TestCase):
    def test_positive_constant_returns_have_positive_lcb(self):
        evidence = manage_challengers.compute_shadow_evidence([0.10] * 20)
        self.assertAlmostEqual(evidence["lowerConfidenceBound"], 0.10)
        self.assertEqual(evidence["tradeCount"], 20)

    def test_negative_evidence_is_not_ready(self):
        evidence = manage_challengers.compute_shadow_evidence([-0.10] * 20)
        profile = {"status_changed_at": 0}
        with patch.object(config, "CHALLENGER_MIN_AGE_DAYS", 7), \
             patch.object(config, "CHALLENGER_MIN_CLOSED_TRADES", 20):
            self.assertFalse(manage_challengers.challenger_is_ready(
                profile, evidence, now_ts=8 * 86400,
            ))


if __name__ == "__main__":
    unittest.main()
