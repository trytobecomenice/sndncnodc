#!/usr/bin/env python3
"""Unit test for db.prune_event_log() — the actual fix for bot_event_log's
unbounded growth (NOT logging.handlers.RotatingFileHandler, which cannot
attach to a database table; see db.py's docstring on that function).

Uses a TEMPORARY SQLite file, never the real data/app.db — this function
deletes rows for real, so it gets an isolated test rather than the "verify
live" precedent used for read-only db.py functions elsewhere in this suite.

Run: python3 -m unittest test_db_prune -v
"""

import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import config
import db


class TestPruneEventLog(unittest.TestCase):
    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.tmp_path)
        conn.execute(
            "CREATE TABLE bot_event_log (id TEXT PRIMARY KEY, timestamp INTEGER, "
            "event_type TEXT, trader_address TEXT, market_slug TEXT, outcome TEXT, "
            "side TEXT, payload_json TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.remove(self.tmp_path)

    def _insert_row(self, row_id, age_days):
        conn = sqlite3.connect(self.tmp_path)
        ts = int(time.time() - age_days * 86400)
        conn.execute(
            "INSERT INTO bot_event_log (id, timestamp, event_type, payload_json) "
            "VALUES (?, ?, 'error', '{}')",
            (row_id, ts),
        )
        conn.commit()
        conn.close()

    def _row_count(self):
        conn = sqlite3.connect(self.tmp_path)
        count = conn.execute("SELECT COUNT(*) FROM bot_event_log").fetchone()[0]
        conn.close()
        return count

    def test_deletes_rows_older_than_retention_keeps_recent(self):
        self._insert_row("old-1", age_days=200)
        self._insert_row("old-2", age_days=181)
        self._insert_row("recent-1", age_days=179)
        self._insert_row("recent-2", age_days=1)

        deleted = db.prune_event_log(retention_days=180)

        self.assertEqual(deleted, 2)
        self.assertEqual(self._row_count(), 2)

    def test_returns_zero_when_nothing_is_old_enough(self):
        self._insert_row("recent-1", age_days=5)
        deleted = db.prune_event_log(retention_days=180)
        self.assertEqual(deleted, 0)
        self.assertEqual(self._row_count(), 1)

    def test_defaults_to_config_retention_when_not_specified(self):
        self._insert_row("old-1", age_days=config.EVENT_LOG_RETENTION_DAYS + 10)
        self._insert_row("recent-1", age_days=1)
        deleted = db.prune_event_log()  # no explicit retention_days
        self.assertEqual(deleted, 1)
        self.assertEqual(self._row_count(), 1)


if __name__ == "__main__":
    unittest.main()
