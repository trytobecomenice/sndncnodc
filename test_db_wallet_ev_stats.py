#!/usr/bin/env python3
"""Unit tests for db.get_wallet_realized_ev_stats() (2026-07-31) — feeds
risk_manager's automatic EV-scaled per-wallet exposure cap.

Uses a TEMPORARY SQLite file, never the real data/app.db.

Run: python3 -m unittest test_db_wallet_ev_stats -v
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import config
import db


class TestGetWalletRealizedEvStats(unittest.TestCase):
    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.tmp_path)
        conn.execute(
            "CREATE TABLE paper_trade (id TEXT PRIMARY KEY, strategy TEXT NOT NULL "
            "DEFAULT 'bot_filtered', wallet_address TEXT NOT NULL, market_slug TEXT NOT NULL, "
            "outcome TEXT NOT NULL, cost_basis_usd REAL NOT NULL DEFAULT 0, "
            "status TEXT NOT NULL, realized_pnl_usd REAL)"
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.remove(self.tmp_path)

    def _insert(self, wallet, cost_basis, pnl, strategy="bot_filtered", status="closed"):
        conn = sqlite3.connect(self.tmp_path)
        conn.execute(
            "INSERT INTO paper_trade (id, strategy, wallet_address, market_slug, outcome, "
            "cost_basis_usd, status, realized_pnl_usd) VALUES (?, ?, ?, 'm', 'Yes', ?, ?, ?)",
            (os.urandom(8).hex(), strategy, wallet, cost_basis, status, pnl),
        )
        conn.commit()
        conn.close()

    def test_computes_mean_ev_pct_and_trade_count_per_wallet(self):
        self._insert("0xAbC", 10.0, 5.0)   # +50%
        self._insert("0xAbC", 10.0, -2.0)  # -20%
        stats = db.get_wallet_realized_ev_stats()
        self.assertEqual(stats["0xabc"]["trade_count"], 2)
        self.assertAlmostEqual(stats["0xabc"]["ev_pct"], 0.15)  # mean(0.5, -0.2)

    def test_wallet_address_is_lowercased(self):
        self._insert("0xABCDEF", 10.0, 1.0)
        stats = db.get_wallet_realized_ev_stats()
        self.assertIn("0xabcdef", stats)
        self.assertNotIn("0xABCDEF", stats)

    def test_open_positions_are_excluded(self):
        self._insert("0xabc", 10.0, 5.0, status="open")
        stats = db.get_wallet_realized_ev_stats()
        self.assertNotIn("0xabc", stats)

    def test_shadow_rehab_trades_are_excluded(self):
        self._insert("0xabc", 10.0, 5.0, strategy="shadow_rehab")
        stats = db.get_wallet_realized_ev_stats()
        self.assertNotIn("0xabc", stats)

    def test_zero_cost_basis_does_not_crash_on_division(self):
        self._insert("0xabc", 0.0, 5.0)
        stats = db.get_wallet_realized_ev_stats()
        self.assertIsNone(stats["0xabc"]["ev_pct"])  # nullif -> NULL, not a ZeroDivisionError

    def test_no_closed_trades_returns_empty_dict(self):
        self.assertEqual(db.get_wallet_realized_ev_stats(), {})


if __name__ == "__main__":
    unittest.main()
