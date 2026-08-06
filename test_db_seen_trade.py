#!/usr/bin/env python3
"""Unit tests for db.py's per-wallet bot_seen_trade dedup (2026-07-31),
replacing a single global SEEN_TRADE_ID_CAP=2000 cap across all wallets
combined. That old design let one busy wallet's trade volume evict a quiet
wallet's older trade_ids from the shared cap -- since bot.py's feed always
returns each wallet's most recent N trades regardless of how long ago they
happened (config.DIRECT_API_PER_WALLET_LIMIT), an evicted-but-still-fetched
trade_id from a quiet wallet got treated as brand new on the next bot.py
restart. Confirmed live: two restarts in one session each triggered a burst
of "new" copies of month-old, already-resolved-market trades.

Uses a TEMPORARY SQLite file, never the real data/app.db.

Run: python3 -m unittest test_db_seen_trade -v
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import config
import db
import polymarket_data_api


class TestSeenTradePerWalletCap(unittest.TestCase):
    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.tmp_path)
        # Same stub schema as test_db_pending_execution.py's _TempDbTestCase
        # (every table load_state()/save_state() touch) -- empty is fine for
        # all of these except bot_seen_trade, which is what these tests check.
        conn.execute("CREATE TABLE bot_seen_trade (trade_id TEXT PRIMARY KEY, seen_at INTEGER, wallet_address TEXT)")
        conn.execute(
            "CREATE TABLE bot_source_position (key TEXT PRIMARY KEY, shares REAL NOT NULL, "
            "cost_basis_usd REAL NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE paper_trade (id TEXT PRIMARY KEY, strategy TEXT NOT NULL DEFAULT "
            "'bot_filtered', wallet_address TEXT NOT NULL, market_slug TEXT NOT NULL, "
            "market_title TEXT, outcome TEXT NOT NULL, source_price REAL, source_size_usd REAL, "
            "our_size_usd REAL NOT NULL, cost_basis_usd REAL NOT NULL DEFAULT 0, "
            "our_shares REAL NOT NULL, avg_entry_price REAL NOT NULL, "
            "buy_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, opened_at INTEGER, "
            "closed_at INTEGER, close_reason TEXT, realized_pnl_usd REAL, "
            "peak_profit_pct REAL NOT NULL DEFAULT 0, last_priced_at INTEGER, "
            "decision_journal_id TEXT, is_demo_data INTEGER NOT NULL DEFAULT 0, "
            "is_phantom INTEGER NOT NULL DEFAULT 0)"
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
        self._saved_cap = config.SEEN_TRADE_IDS_PER_WALLET_CAP

    def tearDown(self):
        self._patcher.stop()
        config.SEEN_TRADE_IDS_PER_WALLET_CAP = self._saved_cap
        os.remove(self.tmp_path)

    def _base_state(self, seen_trade_ids):
        return {"seen_trade_ids": seen_trade_ids, "positions": {}, "source_positions": {},
                "source_cost_basis": {}, "trader_performance": {}, "muted_traders": {}}

    def test_a_busy_wallet_does_not_evict_a_quiet_wallets_trade_ids(self):
        config.SEEN_TRADE_IDS_PER_WALLET_CAP = 5
        # Quiet wallet: 2 old trades, seen first (so they'd be the first
        # evicted under the OLD global-cap-by-recency design).
        quiet = [{"trade_id": f"quiet-{i}", "wallet_address": "0xquiet"} for i in range(2)]
        db.save_state(self._base_state(quiet))
        # Busy wallet: 20 trades, all seen AFTER the quiet wallet's -- under
        # the old global cap, these would push quiet-0/quiet-1 out entirely.
        busy = [{"trade_id": f"busy-{i}", "wallet_address": "0xbusy"} for i in range(20)]
        db.save_state(self._base_state(busy))

        state = db.load_state()
        loaded_ids = {entry["trade_id"] for entry in state["seen_trade_ids"]}
        self.assertIn("quiet-0", loaded_ids)
        self.assertIn("quiet-1", loaded_ids)

    def test_each_wallet_is_capped_independently_at_the_configured_limit(self):
        config.SEEN_TRADE_IDS_PER_WALLET_CAP = 3
        trades = [{"trade_id": f"wallet-a-{i}", "wallet_address": "0xa"} for i in range(10)]
        db.save_state(self._base_state(trades))

        state = db.load_state()
        wallet_a_ids = [e["trade_id"] for e in state["seen_trade_ids"] if e["wallet_address"] == "0xa"]
        self.assertEqual(len(wallet_a_ids), 3)
        # Newest 3 survive (wallet-a-7/8/9), not the oldest.
        self.assertEqual(set(wallet_a_ids), {"wallet-a-7", "wallet-a-8", "wallet-a-9"})

    def test_null_wallet_address_rows_are_capped_in_their_own_bucket(self):
        config.SEEN_TRADE_IDS_PER_WALLET_CAP = 2
        legacy = [{"trade_id": f"legacy-{i}", "wallet_address": None} for i in range(5)]
        db.save_state(self._base_state(legacy))

        state = db.load_state()
        legacy_ids = [e["trade_id"] for e in state["seen_trade_ids"] if e["wallet_address"] is None]
        self.assertEqual(len(legacy_ids), 2)

    def test_round_trip_preserves_wallet_address(self):
        db.save_state(self._base_state([{"trade_id": "t1", "wallet_address": "0xabc"}]))
        state = db.load_state()
        entry = next(e for e in state["seen_trade_ids"] if e["trade_id"] == "t1")
        self.assertEqual(entry["wallet_address"], "0xabc")

    def test_production_cap_covers_the_largest_paginated_poll(self):
        config.SEEN_TRADE_IDS_PER_WALLET_CAP = self._saved_cap
        largest_poll = (config.DIRECT_API_PER_WALLET_LIMIT
                        * polymarket_data_api.MAX_PAGES_PER_FETCH)
        self.assertGreaterEqual(config.SEEN_TRADE_IDS_PER_WALLET_CAP, largest_poll)

    def test_integrity_schema_rebuilds_performance_without_all_phantom_wallets(self):
        conn = sqlite3.connect(self.tmp_path)
        conn.execute(
            "INSERT INTO wallet_profile VALUES "
            "('w1','0xphantom','p','[9.9]',0,0,NULL,NULL,1,1),"
            "('w2','0xclean','c','[8.8]',0,0,NULL,NULL,1,1)"
        )
        row = (
            "p", "bot_filtered", "0xphantom", "m1", "Yes", 10.0, 10.0,
            10.0, 1.0, "closed", 100, 100.0, 1,
        )
        conn.execute(
            "INSERT INTO paper_trade "
            "(id,strategy,wallet_address,market_slug,outcome,our_size_usd,cost_basis_usd,"
            "our_shares,avg_entry_price,status,closed_at,realized_pnl_usd,is_phantom) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row,
        )
        clean_row = list(row)
        clean_row[0] = "c"
        clean_row[2] = "0xclean"
        clean_row[-2] = -2.0
        clean_row[-1] = 0
        conn.execute(
            "INSERT INTO paper_trade "
            "(id,strategy,wallet_address,market_slug,outcome,our_size_usd,cost_basis_usd,"
            "our_shares,avg_entry_price,status,closed_at,realized_pnl_usd,is_phantom) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", clean_row,
        )
        conn.commit()
        conn.close()

        performance = db.load_state()["trader_performance"]
        self.assertNotIn("0xphantom", performance)
        self.assertEqual(performance["0xclean"]["recent_returns"], [-0.2])


if __name__ == "__main__":
    unittest.main()
