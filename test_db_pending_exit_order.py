#!/usr/bin/env python3
"""Unit tests for db.py's pending_exit_order CRUD and compute_live_edge_pct()
(2026-07-26, "Priority 3" bifurcated dynamic order pegging).

Uses a TEMPORARY SQLite file, never the real data/app.db — same precedent as
test_db_pending_execution.py/test_db_whale_events.py.

Run: python3 -m unittest test_db_pending_exit_order -v
"""

import json
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
            "CREATE TABLE pending_exit_order (id TEXT PRIMARY KEY, wallet_address TEXT NOT NULL, "
            "market_slug TEXT NOT NULL, outcome TEXT NOT NULL, position_key TEXT NOT NULL, "
            "shares REAL NOT NULL, init_price REAL NOT NULL, floor_price REAL NOT NULL, "
            "current_price REAL NOT NULL, bullpen_order_id TEXT, close_reason TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL, "
            "last_repriced_at INTEGER, filled_at INTEGER)"
        )
        conn.execute("CREATE TABLE bot_event_log (id TEXT PRIMARY KEY, timestamp INTEGER NOT NULL, "
                     "event_type TEXT NOT NULL, trader_address TEXT, market_slug TEXT, outcome TEXT, "
                     "side TEXT, payload_json TEXT NOT NULL)")
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.remove(self.tmp_path)

    def _insert_close_event(self, event_type, pnl_usd, cost_basis_usd):
        conn = sqlite3.connect(self.tmp_path)
        payload = json.dumps({"pnl_usd": pnl_usd, "cost_basis_usd": cost_basis_usd})
        conn.execute(
            "INSERT INTO bot_event_log (id, timestamp, event_type, payload_json) VALUES (?, ?, ?, ?)",
            (str(time.time()) + str(pnl_usd), int(time.time()), event_type, payload),
        )
        conn.commit()
        conn.close()


class TestComputeLiveEdgePct(_TempDbTestCase):
    def test_returns_none_below_min_samples(self):
        for i in range(5):
            self._insert_close_event("position_resolved", pnl_usd=1.0, cost_basis_usd=5.0)
        self.assertIsNone(db.compute_live_edge_pct(min_samples=20))

    def test_computes_mean_return_per_dollar_staked(self):
        # 3 trades: +50%, +50%, -100% -> mean = 0.0
        self._insert_close_event("position_resolved", pnl_usd=2.5, cost_basis_usd=5.0)
        self._insert_close_event("paper_sell", pnl_usd=2.5, cost_basis_usd=5.0)
        self._insert_close_event("paper_sell_trailing_tp", pnl_usd=-5.0, cost_basis_usd=5.0)
        result = db.compute_live_edge_pct(min_samples=3)
        self.assertAlmostEqual(result, 0.0)

    def test_ignores_rows_with_no_cost_basis(self):
        self._insert_close_event("position_resolved", pnl_usd=2.5, cost_basis_usd=5.0)
        self._insert_close_event("position_resolved", pnl_usd=1.0, cost_basis_usd=0)  # excluded
        result = db.compute_live_edge_pct(min_samples=1)
        self.assertAlmostEqual(result, 0.5)  # only the first row counts


class TestPendingExitOrderCrud(_TempDbTestCase):
    def test_create_and_get_pending(self):
        row_id = db.create_pending_exit_order(
            wallet_address="0xTrader", market_slug="some-market", outcome="Yes",
            position_key="0xTrader|some-market|Yes", shares=10.0, init_price=0.50,
            floor_price=0.40, close_reason="trailing_tp", bullpen_order_id="order-1",
        )
        rows = db.get_pending_exit_orders(status="pending")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], row_id)
        self.assertEqual(rows[0]["current_price"], 0.50)  # defaults to init_price
        self.assertEqual(rows[0]["bullpen_order_id"], "order-1")

    def test_update_price_and_order_id(self):
        row_id = db.create_pending_exit_order(
            wallet_address="0xTrader", market_slug="m", outcome="Yes", position_key="k",
            shares=10.0, init_price=0.50, floor_price=0.40, close_reason="trailing_tp",
        )
        db.update_pending_exit_order_price(row_id, 0.49, bullpen_order_id="order-2")
        rows = db.get_pending_exit_orders(status="pending")
        self.assertEqual(rows[0]["current_price"], 0.49)
        self.assertEqual(rows[0]["bullpen_order_id"], "order-2")
        self.assertIsNotNone(rows[0]["last_repriced_at"])

    def test_close_as_filled(self):
        row_id = db.create_pending_exit_order(
            wallet_address="0xTrader", market_slug="m", outcome="Yes", position_key="k",
            shares=10.0, init_price=0.50, floor_price=0.40, close_reason="trailing_tp",
        )
        db.close_pending_exit_order(row_id, "filled", filled_at=int(time.time()))
        self.assertEqual(db.get_pending_exit_orders(status="pending"), [])
        filled = db.get_pending_exit_orders(status="filled")
        self.assertEqual(len(filled), 1)
        self.assertIsNotNone(filled[0]["filled_at"])


if __name__ == "__main__":
    unittest.main()
