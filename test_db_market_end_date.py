#!/usr/bin/env python3
"""Unit tests for db.py's load_market_end_dates()/save_market_end_date()
(2026-07-26, "Priority 4" theta-decay TTP activation).

Uses a TEMPORARY SQLite file, never the real data/app.db — same precedent as
the other test_db_*.py files.

Run: python3 -m unittest test_db_market_end_date -v
"""

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
            "CREATE TABLE bot_market_event (market_slug TEXT PRIMARY KEY, event_slug TEXT NOT NULL, "
            "category TEXT, holding_rewards_enabled INTEGER, end_date_iso TEXT, resolved_at INTEGER)"
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.remove(self.tmp_path)


class TestMarketEndDatePersistence(_TempDbTestCase):
    def test_save_is_a_no_op_when_no_event_row_exists_yet(self):
        # No save_market_event() call happened first -- matches
        # save_market_category()'s own established "no-op, not an error"
        # precedent for this exact situation.
        db.save_market_end_date("some-market", "2026-08-01")
        self.assertEqual(db.load_market_end_dates(), {})

    def test_save_and_load_round_trip(self):
        db.save_market_event("some-market", "some-event")
        db.save_market_end_date("some-market", "2026-08-01")
        self.assertEqual(db.load_market_end_dates(), {"some-market": "2026-08-01"})

    def test_rows_with_no_end_date_are_absent_from_the_memo(self):
        db.save_market_event("market-a", "event-a")
        db.save_market_event("market-b", "event-b")
        db.save_market_end_date("market-a", "2026-08-01")
        result = db.load_market_end_dates()
        self.assertEqual(result, {"market-a": "2026-08-01"})
        self.assertNotIn("market-b", result)


if __name__ == "__main__":
    unittest.main()
