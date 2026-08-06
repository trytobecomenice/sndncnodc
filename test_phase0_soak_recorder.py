#!/usr/bin/env python3

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import phase0_soak_recorder
from phase0_soak_recorder import (
    JsonlSoakJournal, MarketBookStream, SoakRecorder, _soak_event_id,
    apply_memory_limit, parse_args,
)


class _Journal:
    def __init__(self):
        self.records = []
        self.path = Path(tempfile.gettempdir()) / "phase0_unit_in_memory_never_created.jsonl"

    def append(self, record):
        self.records.append(record)


class TestMarketBookStream(unittest.IsolatedAsyncioTestCase):
    async def test_research_identity_preserves_economically_distinct_same_api_id(self):
        first = {"trade_id": "same", "price": 0.4, "size_usd": 10}
        second = {"trade_id": "same", "price": 0.5, "size_usd": 10}
        self.assertNotEqual(_soak_event_id(first), _soak_event_id(second))

    async def test_book_then_delta_has_monotonic_local_generation(self):
        stream = MarketBookStream(_Journal(), max_assets=2)
        stream.connected = True
        stream.reconnect_epoch = 7
        stream._apply({
            "event_type": "book",
            "asset_id": "asset",
            "bids": [{"price": "0.49", "size": "10"}],
            "asks": [{"price": "0.51", "size": "20"}],
            "timestamp": "1000",
            "hash": "one",
        }, 1_100, 1_100_000_000)
        first = stream.snapshot("asset")
        stream._apply({
            "event_type": "price_change",
            "price_changes": [{
                "asset_id": "asset", "price": "0.49", "size": "0", "side": "BUY",
                "hash": "two",
            }],
            "timestamp": "1200",
        }, 1_210, 1_210_000_000)
        second = stream.snapshot("asset")
        self.assertEqual(first["local_generation"], 1)
        self.assertEqual(second["local_generation"], 2)
        self.assertEqual(second["bids"], [])
        self.assertEqual(second["book_hash"], "two")

    async def test_old_reconnect_epoch_is_never_exposed_as_current_book(self):
        stream = MarketBookStream(_Journal())
        stream.connected = True
        stream.reconnect_epoch = 1
        stream._apply({
            "event_type": "book", "asset_id": "asset",
            "bids": [{"price": "0.4", "size": "1"}],
            "asks": [{"price": "0.6", "size": "1"}], "timestamp": "1",
        }, 1, 1)
        self.assertIsNotNone(stream.snapshot("asset"))
        stream.reconnect_epoch = 2
        self.assertIsNone(stream.snapshot("asset"))

    async def test_asset_lru_is_bounded(self):
        stream = MarketBookStream(_Journal(), max_assets=2)
        await stream.ensure_subscription("one")
        await stream.ensure_subscription("two")
        await stream.ensure_subscription("three")
        self.assertEqual(list(stream.assets), ["two", "three"])

    async def test_legacy_bootstrap_ids_restore_only_as_api_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "soak.jsonl"
            path.write_text(json.dumps({
                "event_type": "dedup_bootstrap",
                "dedup_identity": "exact_trade_id",
                "event_ids": ["legacy-api-id"],
            }) + "\n")
            journal = JsonlSoakJournal(path)
            recorder = SoakRecorder(journal, max_workers=1)
            try:
                self.assertIn("legacy-api-id", recorder.api_seen_ids)
                self.assertNotIn("legacy-api-id", recorder.seen_ids)
                self.assertFalse(recorder.bootstrap_required)
            finally:
                recorder.executor.shutdown(wait=False, cancel_futures=True)
                journal.close()

    async def test_signal_ingress_is_persisted_before_attribution(self):
        journal = _Journal()
        recorder = SoakRecorder(journal, max_workers=1)

        async def resolve(_trade):
            return (
                {"event_slug": "event"},
                {
                    "bids": [(0.49, 100)], "asks": [(0.50, 100)],
                    "book_timestamp_ms": phase0_soak_recorder._wall_ms(),
                    "received_monotonic_ns": phase0_soak_recorder.time.monotonic_ns(),
                    "fee_rate": 0.0, "event_slug": "event",
                },
                "asset",
            )

        recorder._resolve_signal_book = resolve
        recorder.stream.connected = True
        recorder.stream.reconnect_epoch = 1
        recorder.stream.assets["asset"] = phase0_soak_recorder.time.monotonic_ns()
        recorder.stream._apply({
            "event_type": "book", "asset_id": "asset",
            "bids": [{"price": "0.49", "size": "100"}],
            "asks": [{"price": "0.50", "size": "100"}],
            "timestamp": str(phase0_soak_recorder._wall_ms()),
        }, phase0_soak_recorder._wall_ms(), phase0_soak_recorder.time.monotonic_ns())
        trade = {
            "trade_id": "trade", "user_address": "0xabc", "market_slug": "market",
            "market_title": "Market", "outcome": "Yes", "side": "BUY",
            "price": 0.5, "size_usd": 5, "timestamp": "now",
            "_source_timestamp_ms": phase0_soak_recorder._wall_ms(),
            "_raw_record": {"asset": "asset", "size": "10"},
        }
        try:
            with mock.patch.object(phase0_soak_recorder, "DELAY_TARGETS_MS", ()):
                await recorder._process_signal(trade, 1, 2)
            self.assertEqual(
                [record["event_type"] for record in journal.records],
                ["wallet_signal_ingress", "wallet_signal"],
            )
            self.assertEqual(
                journal.records[1]["correlation_id"], journal.records[0]["event_id"]
            )
            self.assertNotEqual(
                journal.records[1]["event_id"], journal.records[0]["event_id"]
            )
            self.assertEqual(
                journal.records[1]["decision_book_source"],
                "public_ws_state_known_at_signal",
            )
            self.assertEqual(
                journal.records[1]["shadow_lifecycle"]["source_share_basis"],
                "source_reported_share_size",
            )
        finally:
            recorder.executor.shutdown(wait=False, cancel_futures=True)

    async def test_delayed_book_after_target_is_not_claimed_known_by_deadline(self):
        journal = _Journal()
        recorder = SoakRecorder(journal, max_workers=1)
        now_ns = phase0_soak_recorder.time.monotonic_ns()
        recorder.stream.snapshot = lambda _asset: {
            "bids": [(0.49, 100)], "asks": [(0.50, 100)],
            "received_monotonic_ns": now_ns,
            "received_timestamp_ms": phase0_soak_recorder._wall_ms(),
            "local_generation": 1, "reconnect_epoch": 1,
        }
        trade = {
            "trade_id": "trade", "user_address": "0xabc", "market_slug": "market",
            "outcome": "Yes", "side": "BUY", "price": 0.5, "size_usd": 5,
        }
        try:
            await recorder._capture_delay(
                "event", trade, "asset", {"fee_rate": 0.0, "lifecycle": {}},
                0, now_ns - 100_000_000,
            )
            observation = journal.records[-1]
            self.assertFalse(observation["book_known_by_capture_deadline"])
            self.assertEqual(observation["target_snapshot_status"], "not_proven_known_by_target")
        finally:
            recorder.executor.shutdown(wait=False, cancel_futures=True)


class TestArguments(unittest.TestCase):
    def test_bootstrap_sample_warmup_is_explicit_sanity_only_option(self):
        args = parse_args([
            "--bootstrap-sample-count", "1",
            "--bootstrap-sample-warmup-seconds", "2.5",
        ])
        self.assertEqual(args.bootstrap_sample_count, 1)
        self.assertEqual(args.bootstrap_sample_warmup_seconds, 2.5)

    def test_darwin_skips_linux_address_space_limit(self):
        with mock.patch.object(phase0_soak_recorder.sys, "platform", "darwin"):
            self.assertFalse(apply_memory_limit(768))


if __name__ == "__main__":
    unittest.main()
