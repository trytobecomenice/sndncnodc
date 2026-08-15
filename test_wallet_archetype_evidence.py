#!/usr/bin/env python3

import copy
import json
from pathlib import Path
import tempfile
import unittest

from research.wallet_archetype_evidence import build_profiles


def _record(event_id, wallet, market, side, size, timestamp_ms, *, shares=None, **extra):
    record = {
        "event_type": "wallet_signal",
        "signal_event_id": event_id,
        "api_trade_id": event_id,
        "first_local_seen_timestamp_ms": timestamp_ms,
        "reported_visibility_lag_ms": 200,
        "signal": {
            "trade_id": event_id,
            "user_address": wallet,
            "market_slug": market,
            "outcome": "Yes",
            "side": side,
            "price": 0.5,
            "size_usd": size,
        },
    }
    if shares is not None:
        record["raw_signal_payload"] = {"size": shares, "usdcSize": size}
    record.update(extra)
    return record


class TestWalletArchetypeEvidence(unittest.TestCase):
    def _build(self, records):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "phase0.jsonl"
            journal.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            return build_profiles([journal])

    def test_incremental_trades_preserved_exact_duplicate_removed(self):
        first = _record("trade-1", "0xA", "market-a", "BUY", 20, 1_000)
        second = _record("trade-2", "0xA", "market-a", "BUY", 20, 1_100)
        artifact = self._build([first, copy.deepcopy(first), second])
        self.assertEqual(artifact["audit"]["duplicate_signals"], 1)
        self.assertEqual(artifact["audit"]["unique_valid_signals"], 2)
        self.assertEqual(artifact["profiles"][0]["observation_counts"]["buy_signals"], 2)

    def test_reports_continuous_flow_evidence_without_fake_archetype(self):
        artifact = self._build([
            _record("buy", "0xA", "market-a", "BUY", 30, 1_000),
            _record("sell", "0xA", "market-a", "SELL", 10, 1_500),
        ])
        profile = artifact["profiles"][0]
        self.assertAlmostEqual(profile["flow_evidence"]["absolute_notional_directionality"], 0.5)
        self.assertEqual(profile["flow_evidence"]["two_way_market_outcome_ratio"], 1.0)
        self.assertEqual(profile["maker_taker_evidence"]["status"], "UNKNOWN")
        self.assertEqual(profile["archetype"]["status"], "NOT_CLASSIFIED_OBSERVATION_ONLY")

    def test_cross_market_timing_is_candidate_not_arbitrage_claim(self):
        artifact = self._build([
            _record("a", "0xA", "market-a", "BUY", 10, 1_000),
            _record("b", "0xA", "market-b", "SELL", 10, 1_500),
        ])
        evidence = artifact["profiles"][0]["timing_evidence"]
        self.assertEqual(evidence["cross_market_burst_signal_count"], 2)
        self.assertEqual(evidence["cross_market_interpretation"], "multi_leg_candidate_not_arbitrage_proof")

    def test_large_share_fill_is_proxy_not_farmer_classification(self):
        artifact = self._build([
            _record("small", "0xA", "market-a", "BUY", 10, 1_000, shares=20),
            _record("large", "0xA", "market-b", "BUY", 50, 2_000, shares=5_000),
            _record("unknown", "0xA", "market-c", "SELL", 5, 3_000),
        ])
        evidence = artifact["profiles"][0]["share_size_evidence"]
        self.assertEqual(evidence["known_share_count"], 2)
        self.assertEqual(evidence["unknown_share_count"], 1)
        self.assertEqual(evidence["large_fill_count"], 1)
        self.assertEqual(evidence["large_fill_ratio_of_known"], 0.5)
        self.assertEqual(
            evidence["interpretation"],
            "large_fill_proxy_only_not_liquidity_reward_or_maker_proof",
        )

    def test_pnl_fields_cannot_change_profile(self):
        base = _record("a", "0xA", "market-a", "BUY", 10, 1_000)
        profitable = copy.deepcopy(base)
        profitable["realized_pnl_usd"] = 999999
        loss = copy.deepcopy(base)
        loss["realized_pnl_usd"] = -999999
        self.assertEqual(
            self._build([profitable])["profiles"],
            self._build([loss])["profiles"],
        )


if __name__ == "__main__":
    unittest.main()
