#!/usr/bin/env python3
import unittest
from unittest.mock import patch

from scripts import recompute_kill_switch_equity as review


class RecomputeKillSwitchEquityTest(unittest.TestCase):
    @patch.object(review.db, "get_realized_ledger_integrity_status",
                  return_value={"status": "PASS", "failures": [], "warnings": []})
    @patch.object(review.db, "get_risk_value", return_value=1129.0)
    @patch.object(review.db, "realized_pnl_total", return_value=-2.0)
    @patch.object(review.bot, "get_market_prices", return_value=(0.4, 0.5, None))
    @patch.object(review.db, "load_state", return_value={
        "positions": {
            "wallet|market|Yes": {
                "shares": 20.0, "cost_basis_usd": 10.0, "avg_entry_price": 0.5,
            }
        }
    })
    def test_reports_runtime_and_strict_liquidation_marks(
            self, _state, _price, _realized, _hwm, _integrity):
        result = review.review_equity(max_workers=1)
        self.assertEqual(result["ledger_integrity"]["status"], "PASS")
        self.assertEqual(result["indicative_price_count"], 1)
        self.assertEqual(result["executable_bid_count"], 1)
        self.assertAlmostEqual(result["risk_equity"]["total_equity"], 1123.0)
        self.assertAlmostEqual(result["strict_liquidation_equity_usd"], 1121.0)
        self.assertEqual(result["risk_equity_triggers"], [])
        self.assertEqual(result["strict_liquidation_triggers"], [])


if __name__ == "__main__":
    unittest.main()
