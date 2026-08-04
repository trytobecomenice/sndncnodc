#!/usr/bin/env python3
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import config
import db


class TestQuantP0Persistence(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.path)
        conn.executescript("""
        CREATE TABLE bot_risk_state (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at INTEGER);
        CREATE TABLE bot_event_log (id TEXT PRIMARY KEY, timestamp INTEGER, event_type TEXT,
          trader_address TEXT, market_slug TEXT, outcome TEXT, side TEXT, payload_json TEXT NOT NULL);
        CREATE TABLE paper_trade (id TEXT PRIMARY KEY, strategy TEXT, status TEXT, closed_at INTEGER,
          realized_pnl_usd REAL, is_demo_data INTEGER DEFAULT 0);
        CREATE TABLE pnl_snapshot (id TEXT PRIMARY KEY, captured_at INTEGER, scope TEXT, strategy TEXT,
          wallet_address TEXT, realized_pnl_usd REAL, unrealized_pnl_usd REAL,
          open_positions_count INTEGER, closed_trades_count INTEGER, win_rate REAL);
        """)
        conn.commit()
        conn.close()
        self.patcher = patch.object(config, "SQLITE_PATH", self.path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        os.remove(self.path)

    def test_evaluation_epoch_is_created_once(self):
        first = db.get_or_create_evaluation_epoch(
            datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
        second = db.get_or_create_evaluation_epoch(
            datetime(2026, 8, 6, tzinfo=timezone.utc),
        )
        self.assertEqual(first, second)

    def test_realized_since_and_snapshot(self):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO bot_event_log VALUES ('a', 100, 'paper_sell', NULL,NULL,NULL,NULL,?)",
                     (json.dumps({"pnl_usd": 5.0}),))
        conn.execute("INSERT INTO bot_event_log VALUES ('b', 200, 'position_resolved', NULL,NULL,NULL,NULL,?)",
                     (json.dumps({"pnl_usd": -2.0}),))
        conn.commit()
        conn.close()
        self.assertEqual(db.realized_pnl_since(150), -2.0)
        db.record_pnl_snapshot("clean_epoch", -2.0, 1.0, 2, 1, 0.0)
        conn = sqlite3.connect(self.path)
        row = conn.execute("SELECT scope, realized_pnl_usd FROM pnl_snapshot").fetchone()
        conn.close()
        self.assertEqual(row, ("clean_epoch", -2.0))


if __name__ == "__main__":
    unittest.main()
