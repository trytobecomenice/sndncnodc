#!/usr/bin/env python3

import unittest

from phase0_attribution import build_phase0_attribution


class TestPhase0Attribution(unittest.TestCase):
    def test_actual_size_liquidation_exposes_thin_bid_side(self):
        result = build_phase0_attribution(
            {
                "price": 0.50,
                "_source_timestamp_ms": 9_000,
                "_received_timestamp_ms": 10_000,
            },
            {
                "bids": [(0.10, 2)],
                "asks": [(0.50, 100)],
                "fee_rate": 0.0,
            },
            10,
        )
        self.assertEqual(result["buy_execution"]["fill_ratio_ppm"], 1_000_000)
        self.assertEqual(
            result["projected_exit_liquidation"]["liquidation_ratio_ppm"], 100_000
        )
        self.assertIn("insufficient_exit_liquidity", result["quality_flags"])
        self.assertEqual(result["signal_timing"]["reported_source_to_receive_ms"], 1_000)
        self.assertIsNone(result["signal_timing"]["age_upper_bound_ms"])

    def test_missing_fee_and_model_are_unknown_not_zero_edge(self):
        result = build_phase0_attribution(
            {"price": 0.40},
            {"bids": [(0.39, 100)], "asks": [(0.41, 100)]},
            5,
        )
        self.assertIsNone(result["fee_rate_ppm"])
        self.assertIsNone(result["buy_execution"]["taker_fee_usd_micros"])
        self.assertIsNone(result["residual_alpha"]["point_estimate_micros"])
        self.assertIsNone(result["residual_alpha"]["lower_bound_micros"])
        self.assertIn("fee_rate_unknown", result["quality_flags"])

    def test_point_estimate_is_labeled_separately_from_missing_lower_bound(self):
        result = build_phase0_attribution(
            {"price": 0.40},
            {
                "bids": [(0.39, 100)],
                "asks": [(0.40, 100)],
                "fee_rate": 0.0,
            },
            5,
            strategy_context={"wallet_model": {"shrunk_win_rate": 0.55}},
        )
        self.assertEqual(result["residual_alpha"]["point_estimate_micros"], 150_000)
        self.assertIsNone(result["residual_alpha"]["lower_bound_micros"])


if __name__ == "__main__":
    unittest.main()
