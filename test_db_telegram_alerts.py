#!/usr/bin/env python3
"""Unit tests for db.append_log()'s Phase 1 observability wiring (2026-07-31):
the copybot_events_total Prometheus counter, and the Telegram alerts fired
on risk_kill_switch_triggered (always, immediate) and error (throttled)
events. Built the same night as the kill-switch/equity-corruption incident
these exist to surface faster next time.

Uses a TEMPORARY SQLite file, never the real data/app.db — same
_TempDbTestCase shape as test_db_decision_journal.py.

Run: python3 -m unittest test_db_telegram_alerts -v
"""

import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import config
import db


class _TempDbTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.tmp_path)
        conn.execute(
            "CREATE TABLE bot_event_log (id TEXT PRIMARY KEY, timestamp INTEGER, "
            "event_type TEXT, trader_address TEXT, market_slug TEXT, outcome TEXT, "
            "side TEXT, payload_json TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE bot_risk_state (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE decision_journal (id TEXT PRIMARY KEY, created_at INTEGER, "
            "wallet_address TEXT NOT NULL, observed_trade_id TEXT, market_slug TEXT NOT NULL, "
            "outcome TEXT NOT NULL, side TEXT, decision_type TEXT NOT NULL, "
            "decision_reason TEXT NOT NULL, score_breakdown_json TEXT, rule_set_version INTEGER, "
            "resulting_action TEXT, linked_paper_trade_id TEXT, source TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE paper_trade (id TEXT PRIMARY KEY, strategy TEXT NOT NULL "
            "DEFAULT 'bot_filtered', wallet_address TEXT NOT NULL, market_slug TEXT NOT NULL, "
            "market_title TEXT, outcome TEXT NOT NULL, source_price REAL, source_size_usd REAL, "
            "our_size_usd REAL NOT NULL, cost_basis_usd REAL NOT NULL DEFAULT 0, "
            "our_shares REAL NOT NULL, avg_entry_price REAL NOT NULL, buy_count INTEGER NOT NULL "
            "DEFAULT 0, status TEXT NOT NULL, opened_at INTEGER, closed_at INTEGER, "
            "close_reason TEXT, realized_pnl_usd REAL, peak_profit_pct REAL NOT NULL DEFAULT 0, "
            "last_priced_at INTEGER, "
            "decision_journal_id TEXT, is_demo_data INTEGER NOT NULL DEFAULT 0)"
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.remove(self.tmp_path)


class TestEventsCounter(_TempDbTestCase):
    def test_append_log_increments_the_events_total_counter(self):
        before = db._EVENTS_TOTAL.labels(event_type="test_event_xyz")._value.get()
        db.append_log({"timestamp": "t", "event_type": "test_event_xyz"})
        after = db._EVENTS_TOTAL.labels(event_type="test_event_xyz")._value.get()
        self.assertEqual(after, before + 1)


class TestKillSwitchTelegramAlert(_TempDbTestCase):
    def test_kill_switch_trigger_sends_an_immediate_alert(self):
        with patch("db.telegram_alerts.send_telegram_alert") as mock_send:
            db.append_log({
                "timestamp": "t", "event_type": "risk_kill_switch_triggered",
                "reasons": ["equity $100 below floor $200"], "equity": 100, "hwm": 300,
            })
        mock_send.assert_called_once()
        self.assertIn("equity $100 below floor $200", mock_send.call_args.args[0])
        self.assertIn("Kill switch", mock_send.call_args.args[0])

    def test_ttp_quarantine_sends_one_specific_operator_alert(self):
        with patch("db.telegram_alerts.send_telegram_alert") as mock_send:
            db.append_log({
                "timestamp": "t", "event_type": "ttp_market_quarantined",
                "market_slug": "dead-market", "outcome": "Yes", "consecutive_failures": 3,
            })
        mock_send.assert_called_once()
        self.assertIn("quarantined", mock_send.call_args.args[0])
        self.assertIn("position remains unpriceable", mock_send.call_args.args[0])


class TestThrottledErrorAlert(_TempDbTestCase):
    def setUp(self):
        super().setUp()
        self._saved_throttle = config.TELEGRAM_ERROR_ALERT_THROTTLE_SECONDS
        config.TELEGRAM_ERROR_ALERT_THROTTLE_SECONDS = 300

    def tearDown(self):
        config.TELEGRAM_ERROR_ALERT_THROTTLE_SECONDS = self._saved_throttle
        super().tearDown()

    def test_first_error_sends_immediately(self):
        with patch("db.telegram_alerts.send_telegram_alert") as mock_send:
            db.append_log({"timestamp": "t", "event_type": "error", "error": "boom"})
        mock_send.assert_called_once()
        self.assertIn("boom", mock_send.call_args.args[0])

    def test_second_error_within_the_window_is_suppressed(self):
        with patch("db.telegram_alerts.send_telegram_alert") as mock_send:
            db.append_log({"timestamp": "t", "event_type": "error", "error": "boom1"})
            db.append_log({"timestamp": "t", "event_type": "error", "error": "boom2"})
        mock_send.assert_called_once()  # only the first went out

    def test_suppressed_count_is_folded_into_the_next_alert_after_the_window(self):
        with patch("db.telegram_alerts.send_telegram_alert") as mock_send, \
             patch("db.time.time") as mock_time:
            mock_time.return_value = 1000.0
            db.append_log({"timestamp": "t", "event_type": "error", "error": "boom1"})
            db.append_log({"timestamp": "t", "event_type": "error", "error": "boom2"})
            mock_time.return_value = 1000.0 + 901
            db.append_log({"timestamp": "t", "event_type": "error", "error": "boom3"})
        self.assertEqual(mock_send.call_count, 2)
        second_alert_text = mock_send.call_args_list[1].args[0]
        self.assertIn("boom3", second_alert_text)
        self.assertIn("1 same-fingerprint occurrence", second_alert_text)

    def test_distinct_error_fingerprints_do_not_mask_each_other(self):
        with patch("db.telegram_alerts.send_telegram_alert") as mock_send:
            db.append_log({"timestamp": "t", "event_type": "error",
                           "error": "trailing_tp price check: timeout", "market_slug": "m"})
            db.append_log({"timestamp": "t", "event_type": "error",
                           "error": "auth failure: token expired"})
        self.assertEqual(mock_send.call_count, 2)

    def test_recovered_fingerprint_reopens_as_a_fresh_incident(self):
        error = {"timestamp": "t", "event_type": "error",
                 "error": "trailing_tp price check: timeout",
                 "market_slug": "m", "outcome": "Yes"}
        with patch("db.telegram_alerts.send_telegram_alert") as mock_send:
            db.append_log(error)
            db.record_ttp_price_failure("m", "Yes", "timeout", now=10)
            self.assertTrue(db.clear_ttp_price_failure_state("m", "Yes", now=20))
            db.append_log(error)
        self.assertEqual(mock_send.call_count, 3)
        self.assertIn("recovered", mock_send.call_args_list[1].args[0].lower())


if __name__ == "__main__":
    unittest.main()
