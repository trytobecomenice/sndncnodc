#!/usr/bin/env python3
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import reset_kill_switch as reset


LATCH = {
    "triggered_at": "2026-08-10T20:01:56Z",
    "reasons": ["bad equity"],
    "equity": 800.0,
    "hwm": 1129.0,
}


class ResetKillSwitchTest(unittest.TestCase):
    def argv(self, equity="1100"):
        return [
            "reset_kill_switch.py", "--clear",
            "--expected-triggered-at", LATCH["triggered_at"],
            "--reviewed-equity", equity,
            "--reviewed-at", datetime.now(timezone.utc).isoformat(),
        ]

    @patch.object(reset, "clear_risk_value")
    @patch.object(reset, "get_risk_value", return_value=LATCH)
    def test_default_is_status_only(self, _get, clear):
        with patch("sys.argv", ["reset_kill_switch.py"]):
            reset.main()
        clear.assert_not_called()

    @patch.object(reset, "clear_risk_value")
    @patch.object(reset, "get_realized_ledger_integrity_status",
                  return_value={"status": "FAIL", "failures": ["missing"], "warnings": []})
    @patch.object(reset, "get_risk_value", return_value=LATCH)
    def test_non_pass_ledger_refuses_clear(self, _get, _audit, clear):
        with patch("sys.argv", self.argv()), self.assertRaisesRegex(SystemExit, "not PASS"):
            reset.main()
        clear.assert_not_called()

    @patch.object(reset, "clear_risk_value")
    @patch.object(reset.risk_manager, "evaluate_equity", return_value=(1129.0, ["still below floor"]))
    @patch.object(reset, "get_realized_ledger_integrity_status",
                  return_value={"status": "PASS", "failures": [], "warnings": []})
    @patch.object(reset, "get_risk_value", side_effect=[LATCH, 1129.0])
    def test_active_condition_refuses_clear(self, _get, _audit, _evaluate, clear):
        with patch("sys.argv", self.argv("800")), self.assertRaisesRegex(SystemExit, "still breaches"):
            reset.main()
        clear.assert_not_called()

    @patch.object(reset, "clear_risk_value")
    @patch.object(reset.risk_manager, "evaluate_equity", return_value=(1129.0, []))
    @patch.object(reset, "get_realized_ledger_integrity_status",
                  return_value={"status": "PASS", "failures": [], "warnings": []})
    @patch.object(reset, "get_risk_value", side_effect=[LATCH, 1129.0])
    def test_fresh_review_and_passed_conditions_can_clear(self, _get, _audit, _evaluate, clear):
        with patch("sys.argv", self.argv()):
            reset.main()
        clear.assert_called_once_with("kill_switch")


if __name__ == "__main__":
    unittest.main()
