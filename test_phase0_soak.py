#!/usr/bin/env python3

import unittest

from phase0_soak import ConvictionTracker, Phase0ShadowLedger, signal_key


def _trade(trade_id, side, price=0.5, size=10, received_ms=1_000):
    return {
        "trade_id": trade_id,
        "user_address": "0xabc",
        "market_slug": "market",
        "outcome": "Yes",
        "side": side,
        "price": price,
        "size_usd": size,
        "_received_timestamp_ms": received_ms,
    }


def _book():
    return {
        "bids": [(0.49, 1_000)],
        "asks": [(0.50, 1_000)],
        "fee_rate": 0.0,
    }


class TestConvictionTracker(unittest.TestCase):
    def test_distinct_same_direction_fills_increase_conviction(self):
        tracker = ConvictionTracker(started_ms=0)
        first = tracker.observe(_trade("one", "BUY"), 1_000)
        second = tracker.observe(_trade("two", "BUY"), 1_100)
        self.assertEqual(first["wallet_recent_trade_count_1h"], 1)
        self.assertEqual(second["wallet_recent_trade_count_1h"], 2)
        self.assertEqual(second["dedup_identity"], "caller_supplied_distinct_fill_event")

    def test_old_fills_expire_from_window(self):
        tracker = ConvictionTracker(window_ms=100, started_ms=0)
        tracker.observe(_trade("one", "BUY"), 1_000)
        observation = tracker.observe(_trade("two", "BUY"), 1_101)
        self.assertEqual(observation["wallet_recent_trade_count_1h"], 1)


class TestPhase0ShadowLedger(unittest.TestCase):
    def test_incremental_buys_are_not_deduplicated_by_position_key(self):
        ledger = Phase0ShadowLedger(tiers_usd=(5,))
        first = ledger.apply_signal(_trade("one", "BUY"), _book())
        second = ledger.apply_signal(_trade("two", "BUY"), _book())
        key = signal_key(_trade("one", "BUY"))
        self.assertEqual(first["source_position_action"], "open_observed_position")
        self.assertEqual(second["source_position_action"], "increase_observed_position")
        self.assertEqual(
            sum(lot["shares_micros"] for lot in ledger.shadow_lots[5][key]),
            20_000_000,
        )

    def test_source_reduce_realizes_only_executable_shadow_exit(self):
        ledger = Phase0ShadowLedger(tiers_usd=(5,))
        ledger.apply_signal(_trade("buy", "BUY", size=10), _book())
        result = ledger.apply_signal(_trade("sell", "SELL", size=5), _book())
        tier = result["tiers"]["5"]
        self.assertEqual(result["source_reduce_fraction_ppm"], 500_000)
        self.assertEqual(tier["action"], "hypothetical_sell")
        self.assertEqual(tier["realized_pnl_usd_micros"], -50_000)
        self.assertEqual(
            result["ledger_after"]["shadow_lots"]["5"][0]["shares_micros"],
            5_000_000,
        )

    def test_partial_sell_uses_raw_source_share_size_not_fee_affected_cash(self):
        ledger = Phase0ShadowLedger(tiers_usd=(5,))
        first = _trade("buy-one", "BUY", price=0.3, size=29.9)
        first["_raw_record"] = {"size": "100"}
        second = _trade("buy-two", "BUY", price=0.5, size=49.8)
        second["_raw_record"] = {"size": "100"}
        sell = _trade("sell", "SELL", price=0.6, size=59.7)
        sell["_raw_record"] = {"size": "100"}
        ledger.apply_signal(first, {
            "bids": [(0.6, 1_000)], "asks": [(0.3, 1_000)], "fee_rate": 0.0,
        })
        ledger.apply_signal(second, {
            "bids": [(0.6, 1_000)], "asks": [(0.5, 1_000)], "fee_rate": 0.0,
        })
        result = ledger.apply_signal(sell, {
            "bids": [(0.6, 1_000)], "asks": [(0.61, 1_000)], "fee_rate": 0.0,
        })
        self.assertEqual(result["source_share_basis"], "source_reported_share_size")
        self.assertEqual(result["source_reduce_fraction_ppm"], 500_000)

    def test_worst_execution_first_closes_highest_cost_lot(self):
        ledger = Phase0ShadowLedger(tiers_usd=(5,))
        ledger.apply_signal(_trade("cheap", "BUY", price=0.3, size=10), {
            "bids": [(0.6, 1_000)], "asks": [(0.3, 1_000)], "fee_rate": 0.0,
        })
        ledger.apply_signal(_trade("expensive", "BUY", price=0.5, size=10), {
            "bids": [(0.6, 1_000)], "asks": [(0.5, 1_000)], "fee_rate": 0.0,
        })
        # Source inventory is 53.33 shares; selling the later 20 source
        # shares reduces 37.5% of our 26.67 shadow shares = 10 shares.
        result = ledger.apply_signal(_trade("reduce", "SELL", price=0.6, size=12), {
            "bids": [(0.6, 1_000)], "asks": [(0.61, 1_000)], "fee_rate": 0.0,
        })
        tier = result["tiers"]["5"]
        self.assertEqual(tier["lot_allocation_policy"], "worst_execution_first")
        self.assertEqual(tier["lot_closures"][0]["lot_id"], "expensive")
        self.assertEqual(tier["realized_pnl_usd_micros"], 1_000_000)

    def test_sell_without_prior_source_inventory_is_not_fake_realized_pnl(self):
        ledger = Phase0ShadowLedger(tiers_usd=(5,))
        result = ledger.apply_signal(_trade("sell", "SELL"), _book())
        self.assertEqual(result["source_position_action"], "sell_without_observed_source_inventory")
        self.assertIsNone(result["tiers"]["5"]["realized_pnl_usd_micros"])


if __name__ == "__main__":
    unittest.main()
