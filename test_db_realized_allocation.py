#!/usr/bin/env python3
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import config
import db


SCHEMA = """
CREATE TABLE bot_risk_state (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at INTEGER);
CREATE TABLE bot_event_log (id TEXT PRIMARY KEY, timestamp INTEGER, event_type TEXT,
  trader_address TEXT, market_slug TEXT, outcome TEXT, side TEXT, payload_json TEXT NOT NULL);
CREATE TABLE decision_journal (id TEXT PRIMARY KEY,created_at INTEGER,wallet_address TEXT,
  market_slug TEXT,outcome TEXT,side TEXT,decision_type TEXT,decision_reason TEXT,
  score_breakdown_json TEXT,rule_set_version INTEGER,source TEXT);
CREATE TABLE paper_trade (id TEXT PRIMARY KEY, strategy TEXT NOT NULL, wallet_address TEXT NOT NULL,
  market_slug TEXT NOT NULL, outcome TEXT NOT NULL, our_size_usd REAL NOT NULL DEFAULT 0,
  cost_basis_usd REAL NOT NULL DEFAULT 0, our_shares REAL NOT NULL DEFAULT 0,
  avg_entry_price REAL NOT NULL DEFAULT 0, buy_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL, opened_at INTEGER, closed_at INTEGER, close_reason TEXT,
  realized_pnl_usd REAL, cumulative_realized_pnl_usd REAL NOT NULL DEFAULT 0,
  cumulative_realized_cost_basis_usd REAL NOT NULL DEFAULT 0,
  realized_event_count INTEGER NOT NULL DEFAULT 0, peak_profit_pct REAL NOT NULL DEFAULT 0,
  last_priced_at INTEGER, is_demo_data INTEGER NOT NULL DEFAULT 0,
  is_phantom INTEGER NOT NULL DEFAULT 0);
CREATE TABLE paper_trade_realized_allocation (
  event_id TEXT PRIMARY KEY, paper_trade_id TEXT, event_timestamp INTEGER NOT NULL,
  event_type TEXT NOT NULL, strategy TEXT NOT NULL, pnl_usd REAL NOT NULL,
  cost_basis_usd REAL, allocation_status TEXT NOT NULL, candidate_count INTEGER NOT NULL,
  allocator_version TEXT NOT NULL, allocation_source TEXT NOT NULL,
  termination_cause TEXT NOT NULL, source_shares_at_termination REAL,
  termination_classifier_version TEXT NOT NULL, allocated_at INTEGER NOT NULL);
CREATE TABLE early_rejection_capture(id TEXT PRIMARY KEY,bot_event_id TEXT UNIQUE,captured_at INTEGER,
  rejection_code TEXT,wallet_address TEXT,market_slug TEXT,outcome TEXT,source_trade_id TEXT,
  source_price REAL,source_size_usd REAL,raw_evidence_table TEXT,analysis_state TEXT,capture_version TEXT);
"""


class TestDurableRealizedAllocation(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        self.patcher = patch.object(config, "SQLITE_PATH", self.path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        os.remove(self.path)

    def insert_trade(self, trade_id="lot", phantom=0):
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO paper_trade(id,strategy,wallet_address,market_slug,outcome,status,"
            "opened_at,is_phantom) VALUES(?,?,?,?,?,'open',?,?)",
            (trade_id, "bot_filtered", "0xAbC", "market", "Yes", 1, phantom),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def sell_event(pnl, remaining):
        return {"timestamp": "t", "event_type": "paper_sell", "trader_address": "0xabc",
                "market_slug": "market", "outcome": "Yes", "side": "SELL",
                "our_shares_remaining": remaining, "pnl_usd": pnl, "cost_basis_usd": 2.0}

    def test_partial_and_final_events_accumulate_on_one_tax_lot(self):
        self.insert_trade()
        db.append_log(self.sell_event(1.25, 3.0))
        db.append_log(self.sell_event(-0.5, 0.0))
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT status,realized_pnl_usd,cumulative_realized_pnl_usd,"
            "cumulative_realized_cost_basis_usd,realized_event_count "
            "FROM paper_trade WHERE id='lot'"
        ).fetchone()
        allocations = conn.execute(
            "SELECT allocation_status,paper_trade_id,pnl_usd FROM "
            "paper_trade_realized_allocation ORDER BY allocated_at,event_id"
        ).fetchall()
        conn.close()
        self.assertEqual(row, ("closed", -0.5, 0.75, 4.0, 2))
        self.assertEqual(len(allocations), 2)
        self.assertTrue(all(item[0] == "matched" and item[1] == "lot" for item in allocations))

    def test_ambiguous_event_is_persisted_and_never_cross_closes(self):
        self.insert_trade("a")
        self.insert_trade("b")
        db.append_log(self.sell_event(2.0, 0.0))
        conn = sqlite3.connect(self.path)
        allocation = conn.execute(
            "SELECT allocation_status,candidate_count,paper_trade_id FROM "
            "paper_trade_realized_allocation"
        ).fetchone()
        statuses = conn.execute("SELECT status FROM paper_trade ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(allocation, ("ambiguous", 2, None))
        self.assertEqual(statuses, [("open",), ("open",)])

    def test_ready_ledger_is_authoritative_and_excludes_phantom_lot(self):
        self.insert_trade("clean", phantom=0)
        db.append_log(self.sell_event(3.0, 0.0))
        self.insert_trade("phantom", phantom=1)
        db.append_log(self.sell_event(9.0, 0.0))
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO bot_risk_state VALUES(?,?,1)",
            (db._REALIZED_LEDGER_READY_KEY, json.dumps({
                "ready": True, "allocator_version": db._REALIZED_ALLOCATOR_VERSION,
            })),
        )
        conn.commit()
        conn.close()
        self.assertEqual(db.realized_pnl_total(), 3.0)

    def test_readiness_key_switches_wallet_and_shadow_decision_readers(self):
        conn = sqlite3.connect(self.path)
        for trade_id, strategy, legacy, cumulative, cumulative_basis in (
            ("real", "bot_filtered", -1.0, 4.0, 20.0),
            ("rehab", "shadow_rehab", -2.0, 3.0, 15.0),
            ("challenger", "shadow_challenger", -3.0, 2.0, 10.0),
        ):
            conn.execute(
                "INSERT INTO paper_trade(id,strategy,wallet_address,market_slug,outcome,status,"
                "cost_basis_usd,realized_pnl_usd,cumulative_realized_pnl_usd,"
                "cumulative_realized_cost_basis_usd,is_phantom) "
                "VALUES(?,?,?,'m','Yes','closed',10,?,?,?,0)",
                (trade_id, strategy, "0xabc", legacy, cumulative, cumulative_basis),
            )
        conn.commit()
        conn.close()

        # Migration without promotion must retain the legacy readers.
        self.assertAlmostEqual(db.get_wallet_realized_ev_stats()["0xabc"]["ev_pct"], -0.1)
        self.assertEqual(db.get_shadow_rehab_returns("0xabc"), [-0.2])
        self.assertEqual(db.get_shadow_returns("0xabc", "shadow_challenger"), [-0.3])

        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO bot_risk_state VALUES(?,?,1)",
            (db._REALIZED_LEDGER_READY_KEY, json.dumps({
                "ready": True, "allocator_version": db._REALIZED_ALLOCATOR_VERSION,
            })),
        )
        conn.commit()
        conn.close()
        self.assertAlmostEqual(db.get_wallet_realized_ev_stats()["0xabc"]["ev_pct"], 0.2)
        self.assertEqual(db.get_shadow_rehab_returns("0xabc"), [0.2])
        self.assertEqual(db.get_shadow_returns("0xabc", "shadow_challenger"), [0.2])
        status = db.get_realized_ledger_reader_status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["reader_column"], "cumulative_realized_pnl_usd")
        self.assertEqual(
            status["reader_cost_basis_column"], "cumulative_realized_cost_basis_usd"
        )

    def test_rejection_capture_is_raw_pointer_and_analysis_stays_blocked(self):
        db.append_log({"timestamp": "t", "event_type": "skip_risk_entry_interlock",
                       "trader_address": "0xabc", "market_slug": "market", "outcome": "Yes",
                       "side": "BUY", "source_trade_id": "trade-1", "source_price": 0.4,
                       "source_size_usd": 20.0, "reason": "interlocked"})
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT c.rejection_code,c.source_trade_id,c.analysis_state,c.raw_evidence_table,"
            "e.event_type FROM early_rejection_capture c JOIN bot_event_log e ON e.id=c.bot_event_id"
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("skip_risk_entry_interlock", "trade-1",
                               "BLOCKED_UNTIL_LEDGER_V2", "bot_event_log",
                               "skip_risk_entry_interlock"))

    def test_ttp_quarantine_is_persistent_idempotent_and_factually_cleared(self):
        first = db.record_ttp_permanent_price_failure("dead-market", "Yes", "404", threshold=3, now=10)
        second = db.record_ttp_permanent_price_failure("dead-market", "Yes", "404", threshold=3, now=20)
        third = db.record_ttp_permanent_price_failure("dead-market", "Yes", "404", threshold=3, now=30)
        fourth = db.record_ttp_permanent_price_failure("dead-market", "Yes", "404", threshold=3, now=40)
        self.assertFalse(first["newly_quarantined"])
        self.assertFalse(second["newly_quarantined"])
        self.assertTrue(third["newly_quarantined"])
        self.assertFalse(fourth["newly_quarantined"])
        self.assertEqual(
            db.load_ttp_price_failure_states()[("dead-market", "Yes")]["status"],
            "QUARANTINED_UNPRICEABLE",
        )
        self.assertTrue(db.clear_ttp_price_failure_state("dead-market", "Yes", now=50))
        self.assertEqual(db.load_ttp_price_failure_states(), {})

    def test_unknown_wording_becomes_suspected_but_never_auto_quarantined(self):
        first = db.record_ttp_price_failure(
            "changed-api", "Yes", "410 Gone", permanent=False,
            suspected_after_seconds=3600, now=100,
        )
        later = db.record_ttp_price_failure(
            "changed-api", "Yes", "different permanent-looking wording",
            permanent=False, suspected_after_seconds=3600, now=3701,
        )
        self.assertEqual(first["status"], "OBSERVING")
        self.assertEqual(later["status"], "SUSPECTED_STRUCTURAL")
        self.assertTrue(later["newly_suspected"])
        self.assertFalse(later["newly_quarantined"])

    def test_explicit_resolution_cause_and_source_snapshot_are_preserved(self):
        self.insert_trade()
        event = {"timestamp": "t", "event_type": "position_resolved",
                 "trader_address": "0xabc", "market_slug": "market", "outcome": "Yes",
                 "pnl_usd": 1.0, "cost_basis_usd": 2.0,
                 "termination_cause": "INTENDED_SOURCE_RESOLUTION",
                 "source_shares_at_termination": 42.0}
        db.append_log(event)
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT termination_cause,source_shares_at_termination,termination_classifier_version "
            "FROM paper_trade_realized_allocation"
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("INTENDED_SOURCE_RESOLUTION", 42.0, "termination-cause-v1"))


if __name__ == "__main__":
    unittest.main()
