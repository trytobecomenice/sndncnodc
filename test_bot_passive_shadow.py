#!/usr/bin/env python3
"""Integration seam tests for passive signal-time shadow capture."""

import unittest
from unittest.mock import patch

import bot
import config
from entry_interlock import EntryInterlockState, InterlockStatus
from shadow_replay import JournalWriterHealth


class _FakeWriter:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.events = []
        self.dropped = 0

    def health(self):
        return JournalWriterHealth(
            queue_size=0,
            queue_capacity=4,
            accepted_events=len(self.events),
            dropped_events=self.dropped,
            write_errors=0,
            running=True,
        )

    def submit(self, event):
        if not self.accepted:
            self.dropped += 1
            return False
        self.events.append(event)
        return True


def _trade(enqueued_ns=9_900_000_000):
    return {
        "trade_id": "trade-1",
        "user_address": "0xabc",
        "market_slug": "market",
        "outcome": "Yes",
        "side": "BUY",
        "price": 0.40,
        "size_usd": 50.0,
        "_received_timestamp_ms": 9_900,
        "_enqueued_monotonic_ns": enqueued_ns,
        "_source_timestamp_ms": 9_800,
        "_raw_payload": '{"id":"trade-1"}',
        "_raw_payload_format": "canonicalized_api_record",
    }


def _book(book_timestamp_ms=9_950):
    return {
        "bids": [(0.39, 100)],
        "asks": [(0.40, 100)],
        "book_timestamp_ms": book_timestamp_ms,
        "book_hash": "hash-1",
        "received_timestamp_ms": 9_980,
        "received_monotonic_ns": 9_980_000_000,
    }


def _risk_state():
    healthy = EntryInterlockState(
        status=InterlockStatus.HEALTHY,
        changed_at_monotonic_ms=0,
        last_observed_monotonic_ms=0,
    )
    return {
        "entry_interlock": None,
        "_entry_interlock_state": healthy,
        "_shadow_interlock_state": healthy,
        "_shadow_writer_last_dropped": 0,
        "_shadow_writer_last_errors": 0,
    }


class TestPassiveShadowDecision(unittest.TestCase):
    def test_shadow_only_path_records_without_persistence_or_network(self):
        writer = _FakeWriter()
        state = _risk_state()
        with patch.object(config, "ENABLE_PASSIVE_ENTRY_INTERLOCK", False), \
             patch("bot.set_risk_value") as set_value, \
             patch("bot.clear_risk_value") as clear_value, \
             patch("bot.append_log") as append_log:
            accepted = bot.record_passive_shadow_decision(
                _trade(), _book(), 5.0, writer, state,
                decision_monotonic_ns=10_000_000_000,
                decision_timestamp_ms=10_000,
            )

        self.assertTrue(accepted)
        self.assertEqual(len(writer.events), 1)
        set_value.assert_not_called()
        clear_value.assert_not_called()
        append_log.assert_not_called()
        self.assertIsNone(state["entry_interlock"])

    def test_hot_path_enqueues_capsule_without_materializing_event(self):
        writer = _FakeWriter()
        state = _risk_state()
        with patch.object(config, "ENABLE_PASSIVE_ENTRY_INTERLOCK", False), \
             patch("shadow_capture.build_passive_shadow_event") as materialize:
            accepted = bot.record_passive_shadow_decision(
                _trade(), _book(), 5.0, writer, state,
                decision_monotonic_ns=10_000_000_000,
                decision_timestamp_ms=10_000,
            )

        self.assertTrue(accepted)
        materialize.assert_not_called()
        self.assertTrue(callable(writer.events[0].materialize_event))

    def test_enforced_stale_book_trips_before_buy_gate(self):
        writer = _FakeWriter()
        state = _risk_state()
        with patch.object(config, "ENABLE_PASSIVE_ENTRY_INTERLOCK", True), \
             patch.object(config, "ENTRY_INTERLOCK_MAX_BOOK_AGE_MS", 5.0), \
             patch("bot.set_risk_value") as set_value, \
             patch("bot.append_log") as append_log, \
             patch.object(bot.METRIC_ENTRY_INTERLOCK_ACTIVE, "set"):
            accepted = bot.record_passive_shadow_decision(
                _trade(), _book(book_timestamp_ms=9_000), 5.0, writer, state,
                decision_monotonic_ns=10_000_000_000,
                decision_timestamp_ms=10_000,
            )

        self.assertTrue(accepted)
        self.assertTrue(state["entry_interlock"]["active"])
        self.assertIn("market_data_age_exceeded", state["entry_interlock"]["reasons"])
        set_value.assert_called_once()
        self.assertEqual(
            append_log.call_args.args[0]["event_type"],
            "risk_entry_interlock_triggered",
        )

    def test_queue_overflow_is_a_fail_closed_audit_breach(self):
        writer = _FakeWriter(accepted=False)
        state = _risk_state()
        with patch.object(config, "ENABLE_PASSIVE_ENTRY_INTERLOCK", True), \
             patch("bot.set_risk_value"), \
             patch("bot.append_log"), \
             patch.object(bot.METRIC_ENTRY_INTERLOCK_ACTIVE, "set"):
            accepted = bot.record_passive_shadow_decision(
                _trade(), _book(), 5.0, writer, state,
                decision_monotonic_ns=10_000_000_000,
                decision_timestamp_ms=10_000,
            )

        self.assertFalse(accepted)
        self.assertIn("minimum_audit_unavailable", state["entry_interlock"]["reasons"])


if __name__ == "__main__":
    unittest.main()
