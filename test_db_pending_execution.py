#!/usr/bin/env python3
"""Unit tests for db.py's pending_execution CRUD (2026-07-24, Rule 29 —
"Dip & Rebound" resting paper orders) and the source_cost_basis round-trip
through load_state()/save_state() (bot_source_position's new
cost_basis_usd column).

Uses a TEMPORARY SQLite file, never the real data/app.db — same precedent as
test_db_categories.py/test_db_prune.py.

Run: python3 -m unittest test_db_pending_execution -v
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
            "CREATE TABLE pending_execution (id TEXT PRIMARY KEY, wallet_address TEXT NOT NULL, "
            "market_slug TEXT NOT NULL, outcome TEXT NOT NULL, source_trade_id TEXT, category TEXT, "
            "anchor_price REAL NOT NULL, lowest_seen_price REAL, whale_shares_at_creation REAL, "
            "target_usd REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
            "created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, filled_at INTEGER, "
            "invalidated_reason TEXT)"
        )
        conn.execute(
            "CREATE TABLE bot_source_position (key TEXT PRIMARY KEY, shares REAL NOT NULL, "
            "cost_basis_usd REAL NOT NULL DEFAULT 0)"
        )
        conn.execute("CREATE TABLE bot_seen_trade (trade_id TEXT PRIMARY KEY, seen_at INTEGER, wallet_address TEXT)")
        conn.execute(
            "CREATE TABLE paper_trade (id TEXT PRIMARY KEY, strategy TEXT NOT NULL DEFAULT "
            "'bot_filtered', wallet_address TEXT NOT NULL, market_slug TEXT NOT NULL, "
            "market_title TEXT, outcome TEXT NOT NULL, source_price REAL, source_size_usd REAL, "
            "our_size_usd REAL NOT NULL, cost_basis_usd REAL NOT NULL DEFAULT 0, "
            "our_shares REAL NOT NULL, avg_entry_price REAL NOT NULL, "
            "buy_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, opened_at INTEGER, "
            "closed_at INTEGER, close_reason TEXT, realized_pnl_usd REAL, "
            "peak_profit_pct REAL NOT NULL DEFAULT 0, last_priced_at INTEGER, "
            "decision_journal_id TEXT, "
            "is_demo_data INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE wallet_profile (id TEXT PRIMARY KEY, wallet_address TEXT NOT NULL UNIQUE, "
            "nickname TEXT, recent_results_json TEXT, consecutive_losses INTEGER, "
            "circuit_breaker_muted INTEGER, mute_reason TEXT, muted_at INTEGER, "
            "created_at INTEGER, updated_at INTEGER)"
        )
        conn.execute("CREATE TABLE decision_journal (id TEXT PRIMARY KEY, linked_paper_trade_id TEXT)")
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.remove(self.tmp_path)

    def _raw_conn(self):
        conn = sqlite3.connect(self.tmp_path)
        conn.row_factory = sqlite3.Row
        return conn


class TestPendingExecutionCrud(_TempDbTestCase):
    def test_create_and_get_by_key(self):
        row_id = db.create_pending_execution(
            wallet_address="0xTrader", market_slug="some-market", outcome="Yes",
            source_trade_id="tid1", category="crypto", anchor_price=0.45,
            whale_shares_at_creation=100.0, target_usd=5.0,
            expires_at=int(time.time()) + 3600,
        )
        self.assertIsNotNone(row_id)

        row = db.get_pending_execution("0xTrader", "some-market", "Yes")
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], row_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["anchor_price"], 0.45)
        self.assertIsNone(row["lowest_seen_price"])

    def test_get_by_key_returns_none_when_no_pending_row(self):
        self.assertIsNone(db.get_pending_execution("0xTrader", "some-market", "Yes"))

    def test_get_by_key_ignores_non_matching_status(self):
        row_id = db.create_pending_execution(
            wallet_address="0xTrader", market_slug="some-market", outcome="Yes",
            source_trade_id="tid1", category="crypto", anchor_price=0.45,
            whale_shares_at_creation=100.0, target_usd=5.0,
            expires_at=int(time.time()) + 3600,
        )
        db.close_pending_execution(row_id, "expired")
        self.assertIsNone(db.get_pending_execution("0xTrader", "some-market", "Yes", status="pending"))
        expired = db.get_pending_execution("0xTrader", "some-market", "Yes", status="expired")
        self.assertEqual(expired["id"], row_id)

    def test_get_pending_executions_lists_only_pending_oldest_first(self):
        id1 = db.create_pending_execution(
            wallet_address="0xA", market_slug="m1", outcome="Yes", source_trade_id="t1",
            category="crypto", anchor_price=0.4, whale_shares_at_creation=10.0,
            target_usd=5.0, expires_at=int(time.time()) + 3600,
        )
        id2 = db.create_pending_execution(
            wallet_address="0xB", market_slug="m2", outcome="No", source_trade_id="t2",
            category="sports", anchor_price=0.6, whale_shares_at_creation=20.0,
            target_usd=7.0, expires_at=int(time.time()) + 3600,
        )
        db.close_pending_execution(id2, "expired")

        rows = db.get_pending_executions(status="pending")
        self.assertEqual([r["id"] for r in rows], [id1])

    def test_update_anchor(self):
        row_id = db.create_pending_execution(
            wallet_address="0xTrader", market_slug="some-market", outcome="Yes",
            source_trade_id="tid1", category="crypto", anchor_price=0.45,
            whale_shares_at_creation=100.0, target_usd=5.0,
            expires_at=int(time.time()) + 3600,
        )
        db.update_pending_execution_anchor(row_id, 0.40)
        row = db.get_pending_execution("0xTrader", "some-market", "Yes")
        self.assertEqual(row["anchor_price"], 0.40)

    def test_update_lowest_seen(self):
        row_id = db.create_pending_execution(
            wallet_address="0xTrader", market_slug="some-market", outcome="Yes",
            source_trade_id="tid1", category="crypto", anchor_price=0.45,
            whale_shares_at_creation=100.0, target_usd=5.0,
            expires_at=int(time.time()) + 3600,
        )
        db.update_pending_execution_lowest_seen(row_id, 0.41)
        row = db.get_pending_execution("0xTrader", "some-market", "Yes")
        self.assertEqual(row["lowest_seen_price"], 0.41)

    def test_close_as_filled_sets_status_and_filled_at(self):
        row_id = db.create_pending_execution(
            wallet_address="0xTrader", market_slug="some-market", outcome="Yes",
            source_trade_id="tid1", category="crypto", anchor_price=0.45,
            whale_shares_at_creation=100.0, target_usd=5.0,
            expires_at=int(time.time()) + 3600,
        )
        filled_ts = int(time.time())
        db.close_pending_execution(row_id, "filled", filled_at=filled_ts)
        row = db.get_pending_execution("0xTrader", "some-market", "Yes", status="filled")
        self.assertEqual(row["status"], "filled")
        self.assertEqual(row["filled_at"], filled_ts)

    def test_close_as_invalidated_records_reason(self):
        row_id = db.create_pending_execution(
            wallet_address="0xTrader", market_slug="some-market", outcome="Yes",
            source_trade_id="tid1", category="crypto", anchor_price=0.45,
            whale_shares_at_creation=100.0, target_usd=5.0,
            expires_at=int(time.time()) + 3600,
        )
        db.close_pending_execution(row_id, "invalidated", invalidated_reason="whale_sold")
        row = db.get_pending_execution("0xTrader", "some-market", "Yes", status="invalidated")
        self.assertEqual(row["invalidated_reason"], "whale_sold")

    def test_panic_bulk_invalidates_only_pending_entry_intents(self):
        first = db.create_pending_execution(
            wallet_address="0xA", market_slug="m1", outcome="Yes",
            source_trade_id="t1", category="crypto", anchor_price=0.4,
            whale_shares_at_creation=10.0, target_usd=5.0,
            expires_at=int(time.time()) + 3600,
        )
        second = db.create_pending_execution(
            wallet_address="0xB", market_slug="m2", outcome="No",
            source_trade_id="t2", category="sports", anchor_price=0.6,
            whale_shares_at_creation=20.0, target_usd=7.0,
            expires_at=int(time.time()) + 3600,
        )
        expired = db.create_pending_execution(
            wallet_address="0xC", market_slug="m3", outcome="Yes",
            source_trade_id="t3", category="other", anchor_price=0.5,
            whale_shares_at_creation=5.0, target_usd=3.0,
            expires_at=int(time.time()) + 3600,
        )
        db.close_pending_execution(expired, "expired")

        changed = db.invalidate_all_pending_executions("panic test")

        self.assertEqual(changed, 2)
        self.assertEqual(db.get_pending_executions(status="pending"), [])
        rows = db.get_pending_executions(status="invalidated")
        self.assertEqual({row["id"] for row in rows}, {first, second})
        self.assertTrue(all(row["invalidated_reason"] == "panic test" for row in rows))
        self.assertIsNotNone(
            db.get_pending_execution("0xC", "m3", "Yes", status="expired")
        )


class TestSourceCostBasisRoundTrip(_TempDbTestCase):
    def test_save_state_writes_cost_basis_and_load_state_reads_it_back(self):
        # 2026-07-27 bug fix: load_state() now lowercases the trader
        # portion of the key on read (see its own comment) -- a saved
        # mixed-case key round-trips back lowercased, not verbatim.
        db.save_state({
            "seen_trade_ids": [], "positions": {},
            "source_positions": {"0xTrader|m1|Yes": 100.0},
            "source_cost_basis": {"0xTrader|m1|Yes": 45.0},
            "trader_performance": {}, "muted_traders": {},
        })
        state = db.load_state()
        self.assertEqual(state["source_positions"]["0xtrader|m1|Yes"], 100.0)
        self.assertEqual(state["source_cost_basis"]["0xtrader|m1|Yes"], 45.0)

    def test_load_state_positions_carry_opened_at(self):
        # Time-Decay Loss Cut (2026-08-01) needs a stable entry timestamp,
        # unaffected by averaging up -- paper_trade.opened_at is only ever
        # set on INSERT (see save_state()), so a position round-tripped
        # through save/load must expose that exact value, not last_priced_at.
        db.save_state({
            "seen_trade_ids": [], "positions": {},
            "source_positions": {}, "source_cost_basis": {},
            "trader_performance": {}, "muted_traders": {},
        })
        conn = self._raw_conn()
        conn.execute(
            "INSERT INTO paper_trade (id, wallet_address, market_slug, outcome, our_size_usd, "
            "cost_basis_usd, our_shares, avg_entry_price, buy_count, status, opened_at, "
            "peak_profit_pct, last_priced_at) VALUES "
            "('row1', '0xTrader', 'm1', 'Yes', 5.0, 5.0, 10.0, 0.5, 1, 'open', 1000, 0.0, 2000)"
        )
        conn.commit()
        conn.close()
        state = db.load_state()
        self.assertEqual(state["positions"]["0xTrader|m1|Yes"]["opened_at"], 1000)

    def test_missing_source_cost_basis_defaults_to_zero(self):
        # A key present in source_positions but absent from source_cost_basis
        # (shouldn't happen via bot.py's own bookkeeping, but load_state()
        # must not crash on a row written before this column existed).
        db.save_state({
            "seen_trade_ids": [], "positions": {},
            "source_positions": {"0xTrader|m1|Yes": 100.0},
            "source_cost_basis": {},
            "trader_performance": {}, "muted_traders": {},
        })
        state = db.load_state()
        self.assertEqual(state["source_cost_basis"]["0xtrader|m1|Yes"], 0.0)

    def test_duplicate_casing_rows_are_merged_on_load_not_overwritten(self):
        # 2026-07-27 bug fix: found live in data/app.db -- a stale
        # mixed-case row (pre-fix) and a fresh lowercase row (post-fix)
        # for the SAME real wallet/market/outcome coexisted, and
        # load_state() previously read whichever happened to sort last,
        # silently discarding the other's real trade history. Both must
        # now be SUMMED, not one overwriting the other.
        conn = sqlite3.connect(self.tmp_path)
        conn.execute(
            "INSERT INTO bot_source_position (key, shares, cost_basis_usd) VALUES (?, ?, ?)",
            ("0xTrader|m1|Yes", 100.0, 45.0),
        )
        conn.execute(
            "INSERT INTO bot_source_position (key, shares, cost_basis_usd) VALUES (?, ?, ?)",
            ("0xtrader|m1|Yes", 50.0, 20.0),
        )
        conn.commit()
        conn.close()
        state = db.load_state()
        self.assertEqual(state["source_positions"]["0xtrader|m1|Yes"], 150.0)
        self.assertEqual(state["source_cost_basis"]["0xtrader|m1|Yes"], 65.0)
        # No lingering second entry under the old casing.
        self.assertNotIn("0xTrader|m1|Yes", state["source_positions"])


if __name__ == "__main__":
    unittest.main()
