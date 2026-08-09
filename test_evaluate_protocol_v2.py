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
        CREATE TABLE paper_trade(id TEXT PRIMARY KEY,status TEXT,closed_at INTEGER,
          market_slug TEXT,outcome TEXT,cost_basis_usd REAL,
          cumulative_realized_pnl_usd REAL DEFAULT 0,realized_event_count INTEGER DEFAULT 0,
          total_acquired_shares REAL,our_shares REAL);
        CREATE TABLE paper_trade_realized_allocation(
          event_id TEXT PRIMARY KEY,paper_trade_id TEXT,event_timestamp INTEGER,event_type TEXT,
          strategy TEXT,pnl_usd REAL,allocation_status TEXT,allocation_source TEXT,
          termination_cause TEXT,termination_classifier_version TEXT,cost_basis_usd REAL,
          shares_closed REAL,shares_remaining REAL);
        CREATE TABLE paper_trade_event_seal(id TEXT PRIMARY KEY,range_start INTEGER,range_end INTEGER,
          event_count INTEGER,pnl_micros INTEGER,shares_micros INTEGER,canonical_sha256 TEXT,
          previous_chain_sha256 TEXT,chain_sha256 TEXT,sealer_version TEXT,sealed_at INTEGER);
        CREATE TABLE pnl_snapshot(captured_at INTEGER,scope TEXT,realized_pnl_usd REAL,
          unrealized_pnl_usd REAL);
        """)
        self.protocol = {
            "freeze_state": "FROZEN",
            "qualification": {
                "window_days": 0.01,
                "coverage_grace_seconds": 900,
                "ttp_observation_schema_version": "ttp-sweep-observation-v3",
                "ttp_price_read_success_rate_min": 0.99,
                "ttp_executable_bid_rate_min": 0.99,
                "ttp_rate_minimum_fetch_attempts": 2,
                "ttp_minimum_sweeps": 2,
                "structural_suspect_sla_seconds": 86400,
                "legacy_quarantine_keys": [],
                "legacy_quarantine_count_max": 0,
                "quarantined_cost_basis_to_equity_max": 0.10,
                "quarantined_ratio_minimum_equity_usd": 900,
                "termination_unknown_rate_max": 0.01,
                "termination_minimum_final_lots": 1,
            },
        }
        self.conn.execute(
            "INSERT INTO bot_risk_state VALUES(?,?,1)",
            (db._REALIZED_LEDGER_READY_KEY, json.dumps({
                "ready": True, "allocator_version": db._REALIZED_ALLOCATOR_VERSION,
            })),
        )
        payload = json.dumps({
            "attempted_positions": 1, "fetch_attempted_positions": 1,
            "pipeline_successful_price_reads": 1,
            "pipeline_executable_bid_reads": 1,
            "qualification_schema_version": "ttp-sweep-observation-v3",
            "termination_classifier_version": db._TERMINATION_CLASSIFIER_VERSION,
        })
        for event_id, timestamp in (("t1", 9136), ("t2", 10000)):
            self.conn.execute(
                "INSERT INTO bot_event_log VALUES(?,?, 'ttp_sweep_observation',?)",
                (event_id, timestamp, payload),
            )
        self.conn.execute(
            "INSERT INTO paper_trade(id,status,closed_at,market_slug,outcome,cost_basis_usd,"
            "cumulative_realized_pnl_usd,realized_event_count,total_acquired_shares,our_shares) "
            "VALUES('lot','closed',9500,'m','Yes',1,1,1,1,0)"
        )
        self.conn.execute("INSERT INTO pnl_snapshot VALUES(10000,'portfolio',0,0)")
        self.conn.execute(
            "INSERT INTO bot_event_log VALUES('live',9500,'paper_sell',?)",
            (json.dumps({"pnl_usd": 1, "our_shares_closed": 1,
                         "our_shares_remaining": 0}),),
        )
        self.conn.execute(
            "INSERT INTO paper_trade_realized_allocation(event_id,paper_trade_id,event_timestamp,"
            "event_type,strategy,pnl_usd,allocation_status,allocation_source,termination_cause,"
            "termination_classifier_version,cost_basis_usd,shares_closed,shares_remaining) VALUES("
            "'live','lot',9500,'paper_sell','bot_filtered',1,'matched','live',"
            "'SOURCE_EXIT',?,1,1,0)",
            (db._TERMINATION_CLASSIFIER_VERSION,),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_historical_unknown_is_excluded_from_live_final_lot_rate(self):
        self.conn.execute(
            "INSERT INTO bot_event_log VALUES('historical',9400,'paper_sell',?)",
            (json.dumps({"pnl_usd": 0, "our_shares_closed": 0,
                         "our_shares_remaining": 1}),),
        )
        self.conn.execute(
            "INSERT INTO paper_trade_realized_allocation(event_id,paper_trade_id,event_timestamp,"
            "event_type,strategy,pnl_usd,allocation_status,allocation_source,termination_cause,"
            "termination_classifier_version,cost_basis_usd,shares_closed,shares_remaining) VALUES("
            "'historical','lot',9400,'paper_sell','bot_filtered',0,'matched',"
            "'historical_backfill','UNKNOWN',?,0,0,1)",
            (db._TERMINATION_CLASSIFIER_VERSION,),
        )
        self.conn.execute("UPDATE paper_trade SET realized_event_count=2 WHERE id='lot'")
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
        self.assertIn("structural_unpriceable_count", reasons)

    def test_preconditions_refuse_perfect_but_underpowered_rate_denominator(self):
        self.protocol["qualification"]["ttp_rate_minimum_fetch_attempts"] = 3
        checks, reasons = evaluate_preconditions(self.conn, self.protocol, 10000)
        by_name = {gate["name"]: gate for gate in checks["qualification_gates"]}
        self.assertEqual(by_name["ttp_pipeline_price_read"]["status"], "UNKNOWN")
        self.assertIn("ttp_pipeline_price_read", reasons)


if __name__ == "__main__":
    unittest.main()
