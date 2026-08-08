#!/usr/bin/env python3
import json
import sqlite3
import unittest

import db
from evaluate_protocol_v2 import evaluate_preconditions


class TestProtocolV2Preconditions(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE bot_risk_state(key TEXT PRIMARY KEY,value_json TEXT,updated_at INTEGER);
        CREATE TABLE bot_event_log(id TEXT PRIMARY KEY,timestamp INTEGER,event_type TEXT,payload_json TEXT);
        CREATE TABLE paper_trade(id TEXT PRIMARY KEY,status TEXT,closed_at INTEGER);
        CREATE TABLE paper_trade_realized_allocation(
          event_id TEXT PRIMARY KEY,paper_trade_id TEXT,event_timestamp INTEGER,event_type TEXT,
          strategy TEXT,pnl_usd REAL,allocation_status TEXT,allocation_source TEXT,
          termination_cause TEXT,termination_classifier_version TEXT);
        """)
        self.protocol = {
            "freeze_state": "FROZEN",
            "qualification": {
                "window_days": 0.01,
                "coverage_grace_seconds": 900,
                "ttp_observation_schema_version": "ttp-sweep-observation-v2",
                "ttp_price_read_success_rate_min": 0.99,
                "ttp_executable_bid_rate_min": 0.99,
                "termination_unknown_rate_max": 0.01,
            },
        }
        self.conn.execute(
            "INSERT INTO bot_risk_state VALUES(?,?,1)",
            (db._REALIZED_LEDGER_READY_KEY, json.dumps({
                "ready": True, "allocator_version": db._REALIZED_ALLOCATOR_VERSION,
            })),
        )
        payload = json.dumps({
            "attempted_positions": 1, "successful_price_reads": 1,
            "executable_bid_reads": 1,
            "qualification_schema_version": "ttp-sweep-observation-v2",
            "termination_classifier_version": db._TERMINATION_CLASSIFIER_VERSION,
        })
        for event_id, timestamp in (("t1", 9136), ("t2", 10000)):
            self.conn.execute(
                "INSERT INTO bot_event_log VALUES(?,?, 'ttp_sweep_observation',?)",
                (event_id, timestamp, payload),
            )
        self.conn.execute("INSERT INTO paper_trade VALUES('lot','closed',9500)")
        self.conn.execute(
            "INSERT INTO paper_trade_realized_allocation VALUES("
            "'live','lot',9500,'paper_sell','bot_filtered',1,'matched','live',"
            "'SOURCE_EXIT',?)",
            (db._TERMINATION_CLASSIFIER_VERSION,),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_historical_unknown_is_excluded_from_live_final_lot_rate(self):
        self.conn.execute(
            "INSERT INTO paper_trade_realized_allocation VALUES("
            "'historical','lot',9400,'paper_sell','bot_filtered',1,'matched',"
            "'historical_backfill','UNKNOWN',?)",
            (db._TERMINATION_CLASSIFIER_VERSION,),
        )
        checks, reasons = evaluate_preconditions(self.conn, self.protocol, 10000)
        self.assertEqual(checks["termination_capture"]["total"], 1)
        self.assertEqual(checks["termination_capture"]["unknown"], 0)
        self.assertEqual(reasons, [])

    def test_active_quarantine_blocks_qualification_without_faking_slo_success(self):
        state = {"version": db._TTP_PRICE_QUARANTINE_VERSION, "entries": {
            "x": {"market_slug": "dead", "outcome": "Yes",
                  "status": "QUARANTINED_UNPRICEABLE"},
        }}
        self.conn.execute(
            "INSERT INTO bot_risk_state VALUES(?,?,1)",
            (db._TTP_PRICE_QUARANTINE_KEY, json.dumps(state)),
        )
        self.conn.commit()
        checks, reasons = evaluate_preconditions(self.conn, self.protocol, 10000)
        self.assertEqual(checks["active_unpriceable_quarantines"], 1)
        self.assertIn("active_unpriceable_market_quarantine", reasons)


if __name__ == "__main__":
    unittest.main()
