#!/usr/bin/env python3

import unittest
from unittest.mock import patch

from passive_integrity import measure_passive_integrity
from shadow_capture import build_passive_shadow_capture, build_passive_shadow_event
from shadow_replay import decide_shadow_buy


class TestPassiveShadowCapture(unittest.TestCase):
    def _trade(self):
        return {
            "trade_id": "trade-1",
            "user_address": "0xabc",
            "market_slug": "market-one",
            "outcome": "Yes",
            "side": "BUY",
            "price": 0.50,
            "size_usd": 100,
            "detected_by": "polling",
            "_received_timestamp_ms": 10_000,
            "_enqueued_monotonic_ns": 1_000_000_000,
            "_source_timestamp_ms": 9_900,
            "_raw_payload": '{"side":"BUY"}',
            "_raw_payload_format": "canonicalized_api_record",
        }

    def _book(self):
        return {
            "bids": [(0.49, 100)],
            "asks": [(0.51, 100)],
            "book_timestamp_ms": 10_005,
            "book_hash": "book-1",
            "received_timestamp_ms": 10_010,
            "received_monotonic_ns": 1_010_000_000,
        }

    def test_builds_replayable_event_without_network_or_order_dependency(self):
        trade = self._trade()
        book = self._book()
        measurement = measure_passive_integrity(
            trade, book, decision_monotonic_ns=1_020_000_000, decision_timestamp_ms=10_020
        )
        event = build_passive_shadow_event(
            trade, book, 5, measurement, False, 10_020, 1_020_000_000
        )
        self.assertEqual(event.raw_payload, '{"side":"BUY"}')
        self.assertEqual(event.normalized_payload["passive_integrity"]["decision_queue_age_ms"], 20.0)
        attribution = event.normalized_payload["phase0_attribution"]
        self.assertEqual(attribution["version"], "phase0-attribution-v1")
        self.assertEqual(attribution["side_adjusted_chase_micros"], 10_000)
        self.assertIsNone(attribution["residual_alpha"]["lower_bound_micros"])
        self.assertEqual(
            attribution["residual_alpha"]["status"],
            "uncalibrated_lower_bound_model_unavailable",
        )
        self.assertEqual(decide_shadow_buy(event).action, "shadow_buy")

    def test_missing_original_raw_payload_is_honestly_flagged_as_reconstructed(self):
        trade = self._trade()
        trade.pop("_raw_payload")
        book = self._book()
        measurement = measure_passive_integrity(
            trade, book, decision_monotonic_ns=1_020_000_000, decision_timestamp_ms=10_020
        )
        event = build_passive_shadow_event(
            trade, book, 5, measurement, False, 10_020, 1_020_000_000
        )
        self.assertIn("raw_payload_reconstructed", event.quality_flags)
        self.assertEqual(
            event.normalized_payload["raw_payload_format"],
            "reconstructed_normalized_signal",
        )

    def test_interlocked_snapshot_replays_to_skip(self):
        trade = self._trade()
        book = self._book()
        measurement = measure_passive_integrity(
            trade, book, decision_monotonic_ns=1_020_000_000, decision_timestamp_ms=10_020
        )
        event = build_passive_shadow_event(
            trade, book, 5, measurement, True, 10_020, 1_020_000_000
        )
        self.assertEqual(decide_shadow_buy(event).reason, "entry_interlock_active")

    def test_continuous_kelly_size_gets_one_bounded_extra_vwap_feature(self):
        trade = self._trade()
        book = self._book()
        measurement = measure_passive_integrity(
            trade, book, decision_monotonic_ns=1_020_000_000, decision_timestamp_ms=10_020
        )
        event = build_passive_shadow_event(
            trade, book, 7.25, measurement, False, 10_020, 1_020_000_000
        )
        checkpoint = event.normalized_payload["checkpoints"]["decision_commit"]
        self.assertEqual(len(checkpoint["buy_vwap"]), 4)
        self.assertEqual(decide_shadow_buy(event).action, "shadow_buy")

    def test_producer_capsule_defers_event_construction_until_materialized(self):
        trade = self._trade()
        book = self._book()
        measurement = measure_passive_integrity(
            trade, book, decision_monotonic_ns=1_020_000_000,
            decision_timestamp_ms=10_020,
        )
        with patch("shadow_capture.build_passive_shadow_event") as build_event:
            capsule = build_passive_shadow_capture(
                trade, book, 5, measurement, False, 10_020, 1_020_000_000
            )
            build_event.assert_not_called()
            capsule.materialize_event()
            build_event.assert_called_once()

    def test_records_real_fee_liquidation_and_context_without_an_extra_model(self):
        trade = self._trade()
        trade["_source_position_action"] = "open_observed_position"
        book = self._book()
        book["fee_rate"] = 0.05
        book["event_slug"] = "event-one"
        measurement = measure_passive_integrity(
            trade, book, decision_monotonic_ns=1_020_000_000,
            decision_timestamp_ms=10_020,
        )
        event = build_passive_shadow_event(
            trade, book, 5, measurement, False, 10_020, 1_020_000_000,
            strategy_context={
                "event_slug": "event-one",
                "category": "politics",
                "wallet_model": {
                    "model_version": "rules-v1",
                    "sizing_tier": "category",
                    "sample_count": 25,
                    "shrunk_win_rate": 0.60,
                },
            },
        )
        attribution = event.normalized_payload["phase0_attribution"]
        self.assertEqual(attribution["fee_rate_ppm"], 50_000)
        self.assertGreater(attribution["buy_execution"]["taker_fee_usd_micros"], 0)
        self.assertEqual(
            attribution["projected_exit_liquidation"]["liquidation_ratio_ppm"],
            1_000_000,
        )
        self.assertLess(
            attribution["market_quality"]["immediate_round_trip_pnl_usd_micros"], 0
        )
        self.assertEqual(
            attribution["risk_context"]["factor_ids"],
            ["category:politics", "event:event-one"],
        )
        self.assertEqual(attribution["source_intent"], "unknown")


if __name__ == "__main__":
    unittest.main()
