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
                "packages/db/drizzle/0028_ledger_seals.sql",
                "packages/db/drizzle/0029_fearless_roxanne_simpson.sql",
                "packages/db/drizzle/0030_redundant_nebula.sql",
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
        for sequence, event_id, ts, market, pnl, basis in (
                (1, "e1", 10, "m", 2.0, 4.0), (2, "e2", 11, "m", -0.5, 1.0),
                (3, "e3", 12, "x", 9.0, 1.0)):
            conn.execute(
                "INSERT INTO bot_event_log(id,event_sequence,timestamp,event_type,trader_address,"
                "market_slug,outcome,side,payload_json) "
                "VALUES(?,?,?,'paper_sell','w',?,'Yes','SELL',?)",
                (event_id, sequence, ts, market, json.dumps({
                    "pnl_usd": pnl, "cost_basis_usd": basis,
                })),
            )
        conn.execute("UPDATE bot_event_sequence_counter SET next_value=4 WHERE singleton=1")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.remove(self.path)

    def report(self):
        allocations = [
            {"event_id": "e1", "paper_trade_id": "clean", "event_timestamp": 10,
             "event_sequence": 1,
             "event_type": "paper_sell", "strategy": "bot_filtered", "pnl_usd": 2.0,
             "cost_basis_usd": 4.0},
            {"event_id": "e2", "paper_trade_id": "clean", "event_timestamp": 11,
             "event_sequence": 2,
             "event_type": "paper_sell", "strategy": "bot_filtered", "pnl_usd": -0.5,
             "cost_basis_usd": 1.0},
            {"event_id": "e3", "paper_trade_id": "phantom", "event_timestamp": 12,
             "event_sequence": 3,
             "event_type": "paper_sell", "strategy": "bot_filtered", "pnl_usd": 9.0,
             "cost_basis_usd": 1.0},
        ]
        return {
            "report_version": REPORT_VERSION,
            "event_allocations": allocations,
            "event_count_in_trade_time_range": 3,
            "unmatched_event_count": 0,
            "ambiguous_event_count": 0,
            "historical_unreconstructable_event_count": 0,
            "historical_unreconstructable_trade_count": 0,
            "quantity_conservation": {
                "mismatch_trade_count": 0, "unknown_trade_count": 0,
            },
            "source_snapshot": {
                "sequence_coherent": True,
                "realized_event_count": 3,
                "realized_event_max_sequence": 3,
                "realized_event_max_timestamp": 12,
                "trade_evidence_sha256": "a" * 64,
                "realized_event_evidence_sha256": "b" * 64,
            },
        }

    def test_apply_is_atomic_and_rebuilds_cumulative_pnl(self):
        summary = apply_report(self.path, self.report(), "abc")
        self.assertEqual(summary["event_count"], 3)
        self.assertEqual(summary["unresolved"], 0)
        self.assertEqual(summary["missing"], 0)
        self.assertEqual(summary["integrity"]["failures"], [])
        conn = sqlite3.connect(self.path)
        rows = conn.execute(
            "SELECT id,cumulative_realized_pnl_usd,cumulative_realized_cost_basis_usd,"
            "realized_event_count FROM paper_trade ORDER BY id"
        ).fetchall()
        ready = json.loads(conn.execute(
            "SELECT value_json FROM bot_risk_state WHERE key=?",
            (db._REALIZED_LEDGER_READY_KEY,),
        ).fetchone()[0])
        conn.close()
        self.assertEqual(rows, [("clean", 1.5, 5.0, 2), ("phantom", 9.0, 1.0, 1)])
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["report_sha256"], "abc")

    def test_apply_never_overwrites_runtime_acquisition_authority(self):
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE paper_trade SET total_acquired_shares=7.097458333766858 "
                     "WHERE id='clean'")
        conn.commit()
        conn.close()
        report = self.report()
        report["event_allocations"][0].update(
            shares_closed=6.972629815412295, shares_remaining=0.12482851835456277
        )
        report["event_allocations"][1].update(shares_closed=0.0, shares_remaining=0.0)
        # This report is deliberately invalid as a complete quantity history;
        # promotion must roll back, but it must never "repair" the acquired
        # authority by deriving and overwriting it from reductions.
        with self.assertRaisesRegex(RuntimeError, "ledger integrity failed"):
            apply_report(self.path, report, "abc")
        conn = sqlite3.connect(self.path)
        acquired = conn.execute(
            "SELECT total_acquired_shares FROM paper_trade WHERE id='clean'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(acquired, 7.097458333766858)

    def test_unresolved_report_is_rejected_before_write(self):
        report = self.report()
        report["unmatched_event_count"] = 1
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_unreported_shadow_realized_event_rolls_back_promotion(self):
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO bot_event_log(id,event_sequence,timestamp,event_type,trader_address,"
            "market_slug,outcome,side,payload_json) "
            "VALUES('shadow-e',4,13,'shadow_rehab_sell','w','m','Yes','SELL',?)",
            (json.dumps({"pnl_usd": 1.0}),),
        )
        conn.execute("UPDATE bot_event_sequence_counter SET next_value=5 WHERE singleton=1")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(RuntimeError, "source DB changed after report"):
            apply_report(self.path, self.report(), "abc")
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM paper_trade_realized_allocation"
        ).fetchone()[0], 0)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM bot_risk_state WHERE key=?", (db._REALIZED_LEDGER_READY_KEY,)
        ).fetchone())
        conn.close()

    def test_event_economics_mismatch_rolls_back_before_readiness(self):
        report = self.report()
        report["event_allocations"][0]["cost_basis_usd"] = 400.0
        with self.assertRaisesRegex(RuntimeError, "retained_event_economic_mismatch"):
            apply_report(self.path, report, "abc")
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM paper_trade_realized_allocation"
        ).fetchone()[0], 0)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM bot_risk_state WHERE key=?", (db._REALIZED_LEDGER_READY_KEY,)
        ).fetchone())
        conn.close()

    def test_historical_unreconstructable_is_preserved_but_excluded(self):
        report = self.report()
        report["event_allocations"][0]["allocation_status"] = (
            "historical_unreconstructable"
        )
        report["historical_unreconstructable_event_count"] = 1
        report["historical_unreconstructable_trade_count"] = 1
        report["quantity_conservation"]["unknown_trade_count"] = 1
        summary = apply_report(self.path, report, "abc")
        self.assertEqual(summary["integrity"]["historical_unreconstructable_allocations"], 1)
        conn = sqlite3.connect(self.path)
        status, source = conn.execute(
            "SELECT allocation_status,allocation_source FROM "
            "paper_trade_realized_allocation WHERE event_id='e1'"
        ).fetchone()
        conn.close()
        self.assertEqual(status, "historical_unreconstructable")
        self.assertEqual(source, "historical_unreconstructable")


if __name__ == "__main__":
    unittest.main()
