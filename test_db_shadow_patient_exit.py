#!/usr/bin/env python3
"""Unit tests for db.py's shadow_patient_exit CRUD + comparison stats
(2026-08-01, paper-only sibling of pending_exit_order).

Uses a TEMPORARY SQLite file, never the real data/app.db — same precedent as
test_db_pending_exit_order.py.

Run: python3 -m unittest test_db_shadow_patient_exit -v
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
            "CREATE TABLE shadow_patient_exit (id TEXT PRIMARY KEY, wallet_address TEXT NOT NULL, "
            "market_slug TEXT NOT NULL, outcome TEXT NOT NULL, position_key TEXT NOT NULL, "
            "shares REAL NOT NULL, init_price REAL NOT NULL, floor_price REAL NOT NULL, "
            "current_price REAL NOT NULL, immediate_exit_price REAL NOT NULL, "
            "close_reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
            "resolved_price REAL, created_at INTEGER NOT NULL, last_repriced_at INTEGER, "
            "resolved_at INTEGER)"
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.remove(self.tmp_path)


class TestShadowPatientExitCrud(_TempDbTestCase):
    def test_create_and_get_pending(self):
        row_id = db.create_shadow_patient_exit(
            wallet_address="0xTrader", market_slug="some-market", outcome="Yes",
            position_key="0xTrader|some-market|Yes", shares=10.0, init_price=0.50,
            floor_price=0.40, immediate_exit_price=0.48, close_reason="trailing_tp",
        )
        rows = db.get_shadow_patient_exits(status="pending")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], row_id)
        self.assertEqual(rows[0]["current_price"], 0.50)  # defaults to init_price
        self.assertEqual(rows[0]["immediate_exit_price"], 0.48)
        self.assertIsNone(rows[0]["resolved_price"])

    def test_update_price(self):
        row_id = db.create_shadow_patient_exit(
            wallet_address="0xTrader", market_slug="m", outcome="Yes", position_key="k",
            shares=10.0, init_price=0.50, floor_price=0.40, immediate_exit_price=0.48,
            close_reason="trailing_tp",
        )
        db.update_shadow_patient_exit_price(row_id, 0.45)
        rows = db.get_shadow_patient_exits(status="pending")
        self.assertEqual(rows[0]["current_price"], 0.45)
        self.assertIsNotNone(rows[0]["last_repriced_at"])

    def test_close_as_filled(self):
        row_id = db.create_shadow_patient_exit(
            wallet_address="0xTrader", market_slug="m", outcome="Yes", position_key="k",
            shares=10.0, init_price=0.50, floor_price=0.40, immediate_exit_price=0.48,
            close_reason="trailing_tp",
        )
        db.close_shadow_patient_exit(row_id, "filled", resolved_price=0.49)
        self.assertEqual(db.get_shadow_patient_exits(status="pending"), [])
        filled = db.get_shadow_patient_exits(status="filled")
        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0]["resolved_price"], 0.49)
        self.assertIsNotNone(filled[0]["resolved_at"])

    def test_close_as_abandoned_with_no_resolved_price(self):
        row_id = db.create_shadow_patient_exit(
            wallet_address="0xTrader", market_slug="m", outcome="Yes", position_key="k",
            shares=10.0, init_price=0.50, floor_price=0.40, immediate_exit_price=0.48,
            close_reason="trailing_tp",
        )
        db.close_shadow_patient_exit(row_id, "abandoned", resolved_price=None)
        abandoned = db.get_shadow_patient_exits(status="abandoned")
        self.assertEqual(len(abandoned), 1)
        self.assertIsNone(abandoned[0]["resolved_price"])


class TestShadowPatientExitComparisonStats(_TempDbTestCase):
    def _make_closed(self, status, immediate_exit_price, resolved_price):
        row_id = db.create_shadow_patient_exit(
            wallet_address="0xTrader", market_slug="m", outcome="Yes", position_key="k",
            shares=10.0, init_price=0.50, floor_price=0.40,
            immediate_exit_price=immediate_exit_price, close_reason="trailing_tp",
        )
        db.close_shadow_patient_exit(row_id, status, resolved_price=resolved_price)

    def test_returns_none_below_min_samples(self):
        for _ in range(5):
            self._make_closed("filled", 0.50, 0.52)
        self.assertIsNone(db.get_shadow_patient_exit_comparison_stats(min_samples=20))

    def test_excludes_abandoned_rows(self):
        for _ in range(20):
            self._make_closed("filled", 0.50, 0.55)  # +10% uplift each
        for _ in range(20):
            self._make_closed("abandoned", 0.50, None)
        stats = db.get_shadow_patient_exit_comparison_stats(min_samples=20)
        self.assertEqual(stats["sample_count"], 20)
        self.assertAlmostEqual(stats["avg_uplift_pct"], 0.10)
        self.assertEqual(stats["fill_rate"], 1.0)

    def test_mixed_filled_and_timeout_fill_rate(self):
        for _ in range(10):
            self._make_closed("filled", 0.50, 0.55)
        for _ in range(10):
            self._make_closed("fallback_timeout", 0.50, 0.45)
        stats = db.get_shadow_patient_exit_comparison_stats(min_samples=20)
        self.assertEqual(stats["sample_count"], 20)
        self.assertEqual(stats["fill_count"], 10)
        self.assertEqual(stats["fill_rate"], 0.5)
        self.assertAlmostEqual(stats["avg_uplift_pct"], 0.0)  # +10% and -10% average out


if __name__ == "__main__":
    unittest.main()
