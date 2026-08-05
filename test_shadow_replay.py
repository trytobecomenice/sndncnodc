#!/usr/bin/env python3

from dataclasses import asdict
import os
import tempfile
import threading
import unittest

from shadow_replay import (
    BoundedJournalWriter,
    EventEnvelope,
    JsonlEventJournal,
    build_book_checkpoint,
    build_rest_book_checkpoint,
    build_source_signal_envelope,
    decision_digest,
    decide_shadow_buy,
    replay_shadow_journal,
)


def checkpoint(**overrides):
    values = {
        "checkpoint": "decision_commit",
        "book": {
            "bids": [("0.49", "20"), ("0.48", "30")],
            "asks": [("0.51", "4"), ("0.52", "20"), ("0.53", "30"), ("0.54", "40")],
        },
        "received_timestamp_ms": 1_700_000_000_010,
        "monotonic_ns": 2_000_000,
        "book_timestamp_ms": 1_700_000_000_000,
        "source_sequence": "42",
        "source_hash": "book-42",
        "resync_generation": 3,
    }
    values.update(overrides)
    return build_book_checkpoint(**values)


def envelope(book_checkpoint=None, copy_size_usd=5):
    book_checkpoint = book_checkpoint or checkpoint()
    return EventEnvelope(
        event_id="signal-1",
        event_type="source_trade_signal",
        source="polymarket_data_api",
        source_timestamp_ms=1_700_000_000_000,
        received_timestamp_ms=1_700_000_000_010,
        monotonic_ns=1_000_000,
        raw_payload='{"price":"0.50","side":"BUY"}',
        normalized_payload={
            "wallet_address": "0xabc",
            "market_slug": "market-one",
            "outcome": "Yes",
            "side": "BUY",
            "copy_size_usd": copy_size_usd,
            "decision_commit_checkpoint": asdict(book_checkpoint),
        },
        correlation_id="copy-1",
        quality_flags=(),
    )


class TestBookCheckpoint(unittest.TestCase):
    def test_sorts_book_and_keeps_only_top_three_levels(self):
        cp = checkpoint(book={
            "bids": [("0.47", "1"), ("0.49", "2"), ("0.48", "3"), ("0.46", "4")],
            "asks": [("0.54", "1"), ("0.51", "2"), ("0.53", "3"), ("0.52", "4")],
        })
        self.assertEqual(cp.best_bid_price_micros, 490_000)
        self.assertEqual(cp.best_ask_price_micros, 510_000)
        self.assertEqual(len(cp.top_bids), 3)
        self.assertEqual(len(cp.top_asks), 3)

    def test_computes_executable_vwap_for_all_three_size_tiers(self):
        cp = checkpoint()
        self.assertEqual([v.requested_usd_micros for v in cp.buy_vwap],
                         [3_000_000, 5_000_000, 10_000_000])
        self.assertFalse(cp.vwap_for_usd(5).insufficient_liquidity)
        self.assertGreater(cp.vwap_for_usd(5).price_micros, 510_000)

    def test_missing_side_is_explicitly_quality_flagged(self):
        cp = checkpoint(book={"bids": [], "asks": []})
        self.assertIn("missing_book", cp.quality_flags)

    def test_invalid_non_finite_level_is_rejected(self):
        with self.assertRaises(ValueError):
            checkpoint(book={"bids": [("nan", "2")], "asks": [("0.5", "2")]})

    def test_direct_rest_adapter_preserves_timestamp_hash_and_missing_metadata_flags(self):
        cp = build_rest_book_checkpoint(
            "signal_visible",
            {"bids": [(0.49, 2)], "asks": [(0.51, 2)],
             "book_timestamp_ms": 1234, "book_hash": "hash-1"},
            received_timestamp_ms=1240,
            monotonic_ns=10,
        )
        self.assertEqual(cp.book_timestamp_ms, 1234)
        self.assertEqual(cp.source_hash, "hash-1")
        self.assertNotIn("missing_book_timestamp", cp.quality_flags)


class TestSourceSignalEnvelope(unittest.TestCase):
    def test_named_checkpoint_schema_carries_all_four_causal_observations(self):
        checkpoints = [
            checkpoint(checkpoint=name, monotonic_ns=index * 100)
            for index, name in enumerate(
                ("source_pre_trade", "signal_visible", "decision_commit", "execution"), 1
            )
        ]
        event = build_source_signal_envelope(
            event_id="signal-four",
            source="fixture",
            raw_payload='{"side":"BUY"}',
            signal={"side": "BUY", "copy_size_usd": 5},
            checkpoints=checkpoints,
            received_timestamp_ms=1000,
            monotonic_ns=200,
        )
        self.assertEqual(
            set(event.normalized_payload["checkpoints"]),
            {"source_pre_trade", "signal_visible", "decision_commit", "execution"},
        )
        self.assertEqual(decide_shadow_buy(event).action, "shadow_buy")

    def test_duplicate_checkpoint_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate checkpoint"):
            build_source_signal_envelope(
                event_id="signal-duplicate",
                source="fixture",
                raw_payload="{}",
                signal={"side": "BUY", "copy_size_usd": 5},
                checkpoints=[checkpoint(), checkpoint()],
                received_timestamp_ms=1000,
                monotonic_ns=100,
            )


class TestShadowDecision(unittest.TestCase):
    def test_quotes_but_never_submits_supported_buy(self):
        decision = decide_shadow_buy(envelope())
        self.assertEqual(decision.action, "shadow_buy")
        self.assertEqual(decision.reason, "quoted_not_submitted")
        self.assertIsNotNone(decision.executable_price_micros)

    def test_interlock_blocks_shadow_entry(self):
        decision = decide_shadow_buy(envelope(), entry_interlock_active=True)
        self.assertEqual(decision.action, "skip")
        self.assertEqual(decision.reason, "entry_interlock_active")

    def test_stale_book_fails_closed(self):
        decision = decide_shadow_buy(envelope(checkpoint(quality_flags=("stale_book",))))
        self.assertEqual(decision.reason, "untrusted_decision_book")

    def test_unsupported_size_is_not_silently_interpolated(self):
        decision = decide_shadow_buy(envelope(copy_size_usd=7))
        self.assertEqual(decision.reason, "unsupported_size_tier")


class TestJournalReplay(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)

    def tearDown(self):
        os.remove(self.path)

    def test_raw_payload_and_integer_features_round_trip(self):
        journal = JsonlEventJournal(self.path)
        event = envelope()
        journal.append(event)
        loaded = journal.read_all()
        self.assertEqual(loaded[0].raw_payload, event.raw_payload)
        self.assertEqual(loaded[0].normalized_payload, event.normalized_payload)

    def test_same_journal_replays_to_same_decision_digest(self):
        journal = JsonlEventJournal(self.path)
        journal.append(envelope())
        events = journal.read_all()
        first = replay_shadow_journal(events)
        second = replay_shadow_journal(events)
        self.assertEqual(decision_digest(first), decision_digest(second))
        self.assertEqual(first, second)

    def test_replay_rejects_monotonic_time_regression(self):
        first = envelope()
        second_record = first.to_record()
        second_record.update({"event_id": "signal-2", "monotonic_ns": 999_999})
        second = EventEnvelope.from_record(second_record)
        with self.assertRaises(ValueError):
            replay_shadow_journal([first, second])

    def test_corrupt_line_reports_exact_record(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{}\nnot-json\n")
        with self.assertRaisesRegex(ValueError, "line 1"):
            JsonlEventJournal(self.path).read_all()

    def test_bounded_writer_drains_without_blocking_submitter(self):
        journal = JsonlEventJournal(self.path)
        writer = BoundedJournalWriter(journal, queue_capacity=2)
        self.assertTrue(writer.submit(envelope()))
        writer.close(timeout=2)
        self.assertEqual(len(journal.read_all()), 1)
        health = writer.health()
        self.assertEqual(health.accepted_events, 1)
        self.assertEqual(health.dropped_events, 0)
        self.assertFalse(health.running)

    def test_bounded_writer_materializes_deferred_capture_off_producer_thread(self):
        journal = JsonlEventJournal(self.path)
        producer_thread = threading.get_ident()
        materialized_thread = []

        class DeferredCapture:
            def materialize_event(self):
                materialized_thread.append(threading.get_ident())
                return envelope()

        writer = BoundedJournalWriter(journal, queue_capacity=2)
        self.assertTrue(writer.submit(DeferredCapture()))
        writer.close(timeout=2)
        self.assertEqual(len(journal.read_all()), 1)
        self.assertNotEqual(materialized_thread, [])
        self.assertNotEqual(materialized_thread[0], producer_thread)

    def test_queue_overflow_is_observable_and_marks_audit_unavailable(self):
        release = threading.Event()
        entered = threading.Event()

        class BlockingJournal:
            def append(self, unused_envelope):
                entered.set()
                release.wait(2)

        writer = BoundedJournalWriter(BlockingJournal(), queue_capacity=1)
        try:
            self.assertTrue(writer.submit(envelope()))
            self.assertTrue(entered.wait(1))
            self.assertTrue(writer.submit(envelope()))
            self.assertFalse(writer.submit(envelope()))
            health = writer.health()
            self.assertEqual(health.dropped_events, 1)
            self.assertFalse(health.minimum_audit_available)
        finally:
            release.set()
            writer.close(timeout=2)


if __name__ == "__main__":
    unittest.main()
