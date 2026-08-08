#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backfill_realized_event_allocations import apply_report, validate_report
import db
from reconcile_paper_trade_events import REPORT_VERSION


class TestAllocationBackfill(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.path)
        migration = "\n".join(
            Path(path).read_text() for path in (
                "packages/db/drizzle/0024_small_xavin.sql",
                "packages/db/drizzle/0025_smart_rocket_racer.sql",
            )
        ).replace("--> statement-breakpoint", "")
        conn.executescript("""
        CREATE TABLE bot_risk_state(key TEXT PRIMARY KEY,value_json TEXT NOT NULL,updated_at INTEGER);
        CREATE TABLE bot_event_log(id TEXT PRIMARY KEY,timestamp INTEGER,event_type TEXT,
          trader_address TEXT,market_slug TEXT,outcome TEXT,side TEXT,payload_json TEXT NOT NULL);
        CREATE TABLE paper_trade(id TEXT PRIMARY KEY,strategy TEXT,wallet_address TEXT,market_slug TEXT,
          outcome TEXT,status TEXT,realized_pnl_usd REAL,is_demo_data INTEGER DEFAULT 0,
          is_phantom INTEGER DEFAULT 0);
        """)
        conn.executescript(migration)
        conn.execute(
            "INSERT INTO paper_trade(id,strategy,wallet_address,market_slug,outcome,status,is_phantom) "
            "VALUES('clean','bot_filtered','w','m','Yes','closed',0),"
            "('phantom','bot_filtered','w','x','Yes','closed',1)"
        )
        for event_id, ts, market, pnl in (("e1", 10, "m", 2.0), ("e2", 11, "m", -0.5),
                                          ("e3", 12, "x", 9.0)):
            conn.execute(
                "INSERT INTO bot_event_log VALUES(?,?, 'paper_sell','w',?,'Yes','SELL',?)",
                (event_id, ts, market, json.dumps({"pnl_usd": pnl})),
            )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.remove(self.path)

    def report(self):
        allocations = [
            {"event_id": "e1", "paper_trade_id": "clean", "event_timestamp": 10,
             "event_type": "paper_sell", "strategy": "bot_filtered", "pnl_usd": 2.0,
             "cost_basis_usd": 4.0},
            {"event_id": "e2", "paper_trade_id": "clean", "event_timestamp": 11,
             "event_type": "paper_sell", "strategy": "bot_filtered", "pnl_usd": -0.5,
             "cost_basis_usd": 1.0},
            {"event_id": "e3", "paper_trade_id": "phantom", "event_timestamp": 12,
             "event_type": "paper_sell", "strategy": "bot_filtered", "pnl_usd": 9.0,
             "cost_basis_usd": 1.0},
        ]
        return {"report_version": REPORT_VERSION, "event_allocations": allocations,
                "event_count_in_trade_time_range": 3, "unmatched_event_count": 0,
                "ambiguous_event_count": 0}

    def test_apply_is_atomic_and_rebuilds_cumulative_pnl(self):
        summary = apply_report(self.path, self.report(), "abc")
        self.assertEqual(summary, {"event_count": 3, "unresolved": 0, "missing": 0})
        conn = sqlite3.connect(self.path)
        rows = conn.execute(
            "SELECT id,cumulative_realized_pnl_usd,realized_event_count FROM paper_trade ORDER BY id"
        ).fetchall()
        ready = json.loads(conn.execute(
            "SELECT value_json FROM bot_risk_state WHERE key=?",
            (db._REALIZED_LEDGER_READY_KEY,),
        ).fetchone()[0])
        conn.close()
        self.assertEqual(rows, [("clean", 1.5, 2), ("phantom", 9.0, 1)])
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["report_sha256"], "abc")

    def test_unresolved_report_is_rejected_before_write(self):
        report = self.report()
        report["unmatched_event_count"] = 1
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_unreported_shadow_realized_event_rolls_back_promotion(self):
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO bot_event_log VALUES('shadow-e',13,'shadow_rehab_sell',"
            "'w','m','Yes','SELL',?)",
            (json.dumps({"pnl_usd": 1.0}),),
        )
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(RuntimeError, "missing=1"):
            apply_report(self.path, self.report(), "abc")
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM paper_trade_realized_allocation"
        ).fetchone()[0], 0)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM bot_risk_state WHERE key=?", (db._REALIZED_LEDGER_READY_KEY,)
        ).fetchone())
        conn.close()


if __name__ == "__main__":
    unittest.main()
