#!/usr/bin/env python3
import time
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
        self.assertEqual(result["quote_status_counts"], {"live_executable": 1})
        self.assertAlmostEqual(result["risk_equity"]["total_equity"], 1123.0)
        self.assertAlmostEqual(result["strict_liquidation_equity_usd"], 1121.0)
        self.assertAlmostEqual(result["stale_quote_equity_usd"], 1121.0)
        self.assertEqual(result["risk_equity_triggers"], [])
        self.assertEqual(result["strict_liquidation_triggers"], [])

    def test_stale_bid_is_attributed_but_never_used_as_strict_liquidation(self):
        state = {
            "positions": {
                "wallet|market|Yes": {
                    "shares": 20.0,
                    "cost_basis_usd": 10.0,
                    "avg_entry_price": 0.5,
                }
            }
        }
        stale_book = {
            "bids": [(0.4, 100.0)],
            "asks": [(0.6, 100.0)],
            "book_timestamp_ms": int(
                (time.time() - review.polymarket_simulator.MAX_BOOK_AGE_SECONDS - 5)
                * 1000
            ),
        }
        with (
            patch.object(review.db, "load_state", return_value=state),
            patch.object(review.bot, "get_market_prices", return_value=(None, 0.5, None)),
            patch.object(
                review.polymarket_simulator,
                "fetch_order_book_for_outcome",
                return_value=({}, stale_book),
            ),
            patch.object(review.db, "realized_pnl_total", return_value=0.0),
            patch.object(review.db, "get_risk_value", return_value=1125.0),
            patch.object(
                review.db,
                "get_realized_ledger_integrity_status",
                return_value={"status": "PASS", "failures": [], "warnings": []},
            ),
        ):
            result = review.review_equity(max_workers=1)

        self.assertEqual(result["quote_status_counts"], {"stale_book": 1})
        self.assertEqual(result["executable_bid_count"], 0)
        self.assertAlmostEqual(result["strict_liquidation_equity_usd"], 1115.0)
        self.assertAlmostEqual(result["stale_quote_equity_usd"], 1123.0)

    def test_resolved_payout_is_redeemable_liquidation_value(self):
        state = {
            "positions": {
                "wallet|resolved-market|Yes": {
                    "shares": 20.0,
                    "cost_basis_usd": 10.0,
                    "avg_entry_price": 0.5,
                }
            }
        }
        metadata = {
            "closed": True,
            "umaResolutionStatus": "resolved",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["1", "0"]',
        }
        with (
            patch.object(review.db, "load_state", return_value=state),
            patch.object(
                review.bot,
                "get_market_prices",
                return_value=(None, None, "no orderbook exists"),
            ),
            patch.object(
                review.polymarket_simulator,
                "fetch_market_metadata",
                return_value=metadata,
            ),
            patch.object(review.db, "realized_pnl_total", return_value=0.0),
            patch.object(review.db, "get_risk_value", return_value=1125.0),
            patch.object(
                review.db,
                "get_realized_ledger_integrity_status",
                return_value={"status": "PASS", "failures": [], "warnings": []},
            ),
        ):
            result = review.review_equity(max_workers=1)

        self.assertEqual(result["quote_status_counts"], {"resolved_redeemable": 1})
        self.assertEqual(result["resolved_redeemable_count"], 1)
        self.assertAlmostEqual(result["strict_liquidation_equity_usd"], 1135.0)


if __name__ == "__main__":
    unittest.main()
