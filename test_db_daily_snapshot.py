#!/usr/bin/env python3
"""Unit tests for db.py's Grafana daily-snapshot functions (2026-07-28):
has_snapshot_for_today(), record_daily_snapshot(), realized_pnl_today().

Uses a TEMPORARY SQLite file, never the real data/app.db — same precedent
as test_db_prune.py/test_db_categories.py.

Run: python3 -m unittest test_db_daily_snapshot -v
"""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
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
            "CREATE TABLE daily_portfolio_snapshots (date TEXT PRIMARY KEY, "
            "snapshot_at INTEGER NOT NULL, total_equity REAL NOT NULL, "
            "total_cash REAL NOT NULL, total_unrealized_pnl REAL NOT NULL, "
            "realized_pnl_today REAL NOT NULL, active_traders_followed INTEGER NOT NULL)"
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.remove(self.tmp_path)

    def _insert_event(self, event_type, pnl_usd, age_hours):
        conn = sqlite3.connect(self.tmp_path)
        ts = int(time.time() - age_hours * 3600)
        conn.execute(
            "INSERT INTO bot_event_log (id, timestamp, event_type, payload_json) VALUES (?, ?, ?, ?)",
            (f"evt-{ts}-{event_type}", ts, event_type, json.dumps({"pnl_usd": pnl_usd})),
        )
        conn.commit()
        conn.close()


class TestRealizedPnlToday(_TempDbTestCase):
    def test_sums_only_todays_closes_not_yesterdays(self):
        self._insert_event("paper_sell", 10.0, age_hours=1)  # today
        self._insert_event("paper_sell", 5.0, age_hours=30)  # yesterday
        self.assertAlmostEqual(db.realized_pnl_today(), 10.0)

    def test_zero_when_nothing_closed_today(self):
        self._insert_event("paper_sell", 10.0, age_hours=30)
        self.assertAlmostEqual(db.realized_pnl_today(), 0.0)

    def test_includes_zombie_dump_closes(self):
        # Real gap found and fixed 2026-07-28: paper_sell_zombie_dump/
        # live_sell_zombie_dump were missing from the realized-PnL event
        # type list entirely.
        self._insert_event("paper_sell_zombie_dump", 8.0, age_hours=1)
        self._insert_event("live_sell_zombie_dump", 4.0, age_hours=1)
        self.assertAlmostEqual(db.realized_pnl_today(), 12.0)

    def test_includes_position_resolved_and_trailing_tp_closes(self):
        self._insert_event("position_resolved", 3.0, age_hours=1)
        self._insert_event("paper_sell_trailing_tp", 2.0, age_hours=1)
        self._insert_event("live_sell_trailing_tp", 1.0, age_hours=1)
        self.assertAlmostEqual(db.realized_pnl_today(), 6.0)

    def test_ignores_unrelated_event_types(self):
        self._insert_event("skip_muted_trader", 999.0, age_hours=1)  # has a pnl_usd-shaped field, but wrong type
        self.assertAlmostEqual(db.realized_pnl_today(), 0.0)


class TestSnapshotIdempotency(_TempDbTestCase):
    def test_has_snapshot_for_today_is_false_before_any_write(self):
        self.assertFalse(db.has_snapshot_for_today())

    def test_has_snapshot_for_today_is_true_after_a_write(self):
        db.record_daily_snapshot(
            total_equity=100.0, total_cash=50.0, total_unrealized_pnl=10.0,
            realized_pnl_today=5.0, active_traders_followed=17,
        )
        self.assertTrue(db.has_snapshot_for_today())

    def test_a_second_write_same_day_overwrites_not_duplicates(self):
        now = datetime.now(timezone.utc)
        db.record_daily_snapshot(
            total_equity=100.0, total_cash=50.0, total_unrealized_pnl=10.0,
            realized_pnl_today=5.0, active_traders_followed=17, now=now,
        )
        db.record_daily_snapshot(
            total_equity=200.0, total_cash=60.0, total_unrealized_pnl=20.0,
            realized_pnl_today=15.0, active_traders_followed=16, now=now,
        )
        conn = sqlite3.connect(self.tmp_path)
        rows = conn.execute("SELECT * FROM daily_portfolio_snapshots").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)  # overwritten, not a second row

    def test_has_snapshot_for_today_is_false_for_a_row_from_a_different_day(self):
        from datetime import timedelta
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        db.record_daily_snapshot(
            total_equity=100.0, total_cash=50.0, total_unrealized_pnl=10.0,
            realized_pnl_today=5.0, active_traders_followed=17, now=yesterday,
        )
        self.assertFalse(db.has_snapshot_for_today())


if __name__ == "__main__":
    unittest.main()
