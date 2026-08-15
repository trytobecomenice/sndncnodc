#!/usr/bin/env python3
import unittest
from unittest.mock import patch

import bot


class LedgerInterlockTest(unittest.TestCase):
    @patch.object(bot, "append_log")
    @patch.object(bot, "set_risk_value")
    @patch.object(bot, "get_realized_ledger_integrity_status", return_value={
        "status": "FAIL", "failures": ["retained_event_missing_allocation"],
        "warnings": [],
    })
    def test_ledger_trip_declares_automatic_healthy_window_recovery(
            self, _status, set_risk, _append):
        risk_state = {"entry_interlock": None}
        bot.enforce_realized_ledger_integrity(risk_state)
        interlock = risk_state["entry_interlock"]
        self.assertTrue(interlock["active"])
        self.assertEqual(interlock["recovery_mode"], "automatic_healthy_window")
        self.assertNotIn("requires_manual_clear", interlock)
        set_risk.assert_called_once_with("entry_interlock", interlock)


if __name__ == "__main__":
    unittest.main()
