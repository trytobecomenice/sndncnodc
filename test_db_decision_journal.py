#!/usr/bin/env python3
"""Unit tests for the point 3.2 prerequisites (2026-07-23, see
docs/copy-trading/RISK_MANAGEMENT.md Rule 22):
  - db.append_log() now returns the new decision_journal row's id, and
    persists score_breakdown_json/rule_set_version when given.
  - db.get_active_rule_set_version() — a read-only lookup against the
    TS-owned rule_set table.
  - db.save_state()'s new decision_journal<->paper_trade linkage
    (paper_trade.decision_journal_id on open, decision_journal.
    linked_paper_trade_id on every buy that touches the position).

Uses a TEMPORARY SQLite file, never the real data/app.db — same precedent
as test_db_categories.py/test_db_prune.py.

Run: python3 -m unittest test_db_decision_journal -v
"""

import json
import os
import sqlite3
import tempfile
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
        conn.execute(
            "CREATE TABLE rule_set (id TEXT PRIMARY KEY, version INTEGER NOT NULL, "
            "is_active INTEGER NOT NULL DEFAULT 0, thresholds_json TEXT NOT NULL, "
            "description TEXT, created_at INTEGER)"
        )
        conn.execute("CREATE TABLE bot_seen_trade (trade_id TEXT PRIMARY KEY, seen_at INTEGER)")
        conn.execute("CREATE TABLE bot_source_position (key TEXT PRIMARY KEY, shares REAL)")
        # save_state() also upserts wallet_profile for every wallet in
        # config.TRACKED_TRADERS (real config, not test-controlled) — needed
        # here purely so that write doesn't fail, not exercised by these tests.
        conn.execute(
            "CREATE TABLE wallet_profile (id TEXT PRIMARY KEY, wallet_address TEXT NOT NULL UNIQUE, "
            "nickname TEXT, status TEXT NOT NULL DEFAULT 'watch', circuit_breaker_muted INTEGER "
            "NOT NULL DEFAULT 0, mute_reason TEXT, muted_at INTEGER, consecutive_losses INTEGER "
            "NOT NULL DEFAULT 0, recent_results_json TEXT, created_at INTEGER, updated_at INTEGER)"
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.remove(self.tmp_path)

    def _raw_conn(self):
        return sqlite3.connect(self.tmp_path)

    def _insert_rule_set(self, version, is_active):
        conn = self._raw_conn()
        conn.execute(
            "INSERT INTO rule_set (id, version, is_active, thresholds_json) VALUES (?, ?, ?, '{}')",
            (f"rs-{version}", version, 1 if is_active else 0),
        )
        conn.commit()
        conn.close()


class TestGetActiveRuleSetVersion(_TempDbTestCase):
    def test_returns_the_active_version(self):
        self._insert_rule_set(1, is_active=False)
        self._insert_rule_set(3, is_active=True)
        self.assertEqual(db.get_active_rule_set_version(), 3)

    def test_returns_none_when_no_row_is_active(self):
        self._insert_rule_set(1, is_active=False)
        self.assertIsNone(db.get_active_rule_set_version())

    def test_returns_none_on_an_empty_table(self):
        self.assertIsNone(db.get_active_rule_set_version())


class TestAppendLogDecisionJournal(_TempDbTestCase):
    def _copy_event(self, **overrides):
        event = {
            "timestamp": "2026-07-23T00:00:00Z", "event_type": "paper_buy",
            "trader_address": "0xAbC", "market_slug": "some-market", "outcome": "Yes",
            "side": "BUY",
        }
        event.update(overrides)
        return event

    def test_returns_the_new_decision_journal_id(self):
        returned_id = db.append_log(self._copy_event())
        self.assertIsNotNone(returned_id)
        conn = self._raw_conn()
        row = conn.execute("SELECT id FROM decision_journal").fetchone()
        conn.close()
        self.assertEqual(row[0], returned_id)

    def test_returns_none_for_an_event_that_is_not_a_decision(self):
        returned_id = db.append_log({
            "timestamp": "2026-07-23T00:00:00Z", "event_type": "bootstrap",
        })
        self.assertIsNone(returned_id)
        conn = self._raw_conn()
        count = conn.execute("SELECT COUNT(*) FROM decision_journal").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_persists_score_breakdown_json_when_given(self):
        breakdown = {"category": "crypto", "sizing_tier": "category", "composite_score": 0.4}
        db.append_log(self._copy_event(score_breakdown=breakdown))
        conn = self._raw_conn()
        row = conn.execute("SELECT score_breakdown_json FROM decision_journal").fetchone()
        conn.close()
        self.assertEqual(json.loads(row[0]), breakdown)

    def test_score_breakdown_json_is_null_when_not_given(self):
        db.append_log(self._copy_event())
        conn = self._raw_conn()
        row = conn.execute("SELECT score_breakdown_json FROM decision_journal").fetchone()
        conn.close()
        self.assertIsNone(row[0])

    def test_persists_rule_set_version_when_given(self):
        db.append_log(self._copy_event(rule_set_version=3))
        conn = self._raw_conn()
        row = conn.execute("SELECT rule_set_version FROM decision_journal").fetchone()
        conn.close()
        self.assertEqual(row[0], 3)


class TestSaveStateDecisionJournalLinkage(_TempDbTestCase):
    """save_state()'s new paper_trade.decision_journal_id (opening decision
    only) / decision_journal.linked_paper_trade_id (every decision that
    touches the position) linkage."""

    def _insert_decision(self, decision_id):
        conn = self._raw_conn()
        conn.execute(
            "INSERT INTO decision_journal (id, created_at, wallet_address, market_slug, "
            "outcome, decision_type, decision_reason, source) "
            "VALUES (?, 0, '0xtrader', 'some-market', 'Yes', 'copy', 'test', 'bot.py')",
            (decision_id,),
        )
        conn.commit()
        conn.close()

    def _state_with_position(self, last_decision_journal_id, shares=100.0, buy_count=1):
        return {
            "seen_trade_ids": [], "source_positions": {},
            "positions": {
                "0xtrader|some-market|Yes": {
                    "shares": shares, "cost_basis_usd": 40.0, "avg_entry_price": 0.4,
                    "buy_count": buy_count, "peak_profit_pct": 0.0,
                    "last_decision_journal_id": last_decision_journal_id,
                }
            },
            "trader_performance": {}, "muted_traders": {},
        }

    def test_new_position_gets_decision_journal_id_on_open(self):
        self._insert_decision("decision-1")
        db.save_state(self._state_with_position("decision-1"))

        conn = self._raw_conn()
        row = conn.execute(
            "SELECT decision_journal_id FROM paper_trade WHERE wallet_address = '0xtrader'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "decision-1")

    def test_opening_decision_gets_linked_paper_trade_id(self):
        self._insert_decision("decision-1")
        db.save_state(self._state_with_position("decision-1"))

        conn = self._raw_conn()
        paper_trade_id = conn.execute(
            "SELECT id FROM paper_trade WHERE wallet_address = '0xtrader'"
        ).fetchone()[0]
        linked = conn.execute(
            "SELECT linked_paper_trade_id FROM decision_journal WHERE id = 'decision-1'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(linked, paper_trade_id)

    def test_averaging_up_links_the_new_decision_without_overwriting_opening_decision_id(self):
        self._insert_decision("decision-1")
        self._insert_decision("decision-2")

        db.save_state(self._state_with_position("decision-1", shares=100.0, buy_count=1))
        db.save_state(self._state_with_position("decision-2", shares=200.0, buy_count=2))

        conn = self._raw_conn()
        row = conn.execute(
            "SELECT id, decision_journal_id, our_shares FROM paper_trade "
            "WHERE wallet_address = '0xtrader'"
        ).fetchone()
        paper_trade_id, decision_journal_id, our_shares = row
        self.assertEqual(decision_journal_id, "decision-1")  # opening decision, unchanged
        self.assertEqual(our_shares, 200.0)  # the average-up DID update the position

        linked_1 = conn.execute(
            "SELECT linked_paper_trade_id FROM decision_journal WHERE id = 'decision-1'"
        ).fetchone()[0]
        linked_2 = conn.execute(
            "SELECT linked_paper_trade_id FROM decision_journal WHERE id = 'decision-2'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(linked_1, paper_trade_id)
        self.assertEqual(linked_2, paper_trade_id)  # the average-up decision is ALSO linked

    def test_a_persist_with_no_fresh_decision_does_not_error(self):
        # A TTP/closeout-sweep-triggered persist() has no fresh decision for
        # a given position — last_decision_journal_id is None/absent.
        state = self._state_with_position(None)
        db.save_state(state)  # must not raise
        conn = self._raw_conn()
        row = conn.execute(
            "SELECT decision_journal_id FROM paper_trade WHERE wallet_address = '0xtrader'"
        ).fetchone()
        conn.close()
        self.assertIsNone(row[0])


if __name__ == "__main__":
    unittest.main()
