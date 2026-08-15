#!/usr/bin/env python3
import json
import os
import sqlite3
import tempfile
import unittest

from reconcile_paper_trade_events import allocate_events, reconcile


class TestEventAllocation(unittest.TestCase):
    def test_unique_interval_allocates_partial_events_to_one_lot(self):
        trades = [{"id": "a", "strategy": "bot_filtered", "wallet_address": "w", "market_slug": "m", "outcome": "Yes",
                   "opened_at": 10, "closed_at": 20, "allocation_end_at": 20}]
        events = [
            {"id": "e1", "strategy": "bot_filtered", "trader_address": "w", "market_slug": "m", "outcome": "Yes",
             "timestamp": 15, "pnl_usd": 1},
            {"id": "e2", "strategy": "bot_filtered", "trader_address": "w", "market_slug": "m", "outcome": "Yes",
             "timestamp": 20, "pnl_usd": 2},
        ]
        allocated, unmatched, ambiguous = allocate_events(trades, events)
        self.assertEqual([row["id"] for row in allocated["a"]], ["e1", "e2"])
        self.assertFalse(unmatched)
        self.assertFalse(ambiguous)

    def test_no_match_and_overlapping_lots_are_never_forced(self):
        trades = [
            {"id": "a", "strategy": "bot_filtered", "wallet_address": "w", "market_slug": "m", "outcome": "Yes",
             "opened_at": 10, "closed_at": 20, "allocation_end_at": 20},
            {"id": "b", "strategy": "bot_filtered", "wallet_address": "w", "market_slug": "m", "outcome": "Yes",
             "opened_at": 15, "closed_at": 25, "allocation_end_at": 25},
        ]
        events = [
            {"id": "amb", "strategy": "bot_filtered", "trader_address": "w", "market_slug": "m", "outcome": "Yes",
             "timestamp": 17, "pnl_usd": 1},
            {"id": "none", "strategy": "bot_filtered", "trader_address": "x", "market_slug": "m", "outcome": "Yes",
             "timestamp": 17, "pnl_usd": 1},
        ]
        allocated, unmatched, ambiguous = allocate_events(trades, events)
        self.assertFalse(allocated)
        self.assertEqual([row["id"] for row in unmatched], ["none"])
        self.assertEqual(ambiguous[0]["candidate_trade_ids"], ["a", "b"])

    def test_partial_event_payload_fields_survive_report_allocation(self):
        trades = [{"id": "a", "strategy": "bot_filtered", "wallet_address": "w", "market_slug": "m", "outcome": "Yes",
                   "opened_at": 10, "closed_at": 20, "allocation_end_at": 20}]
        events = [{"id": "e", "strategy": "bot_filtered", "trader_address": "w", "market_slug": "m", "outcome": "Yes",
                   "timestamp": 15, "event_type": "paper_sell", "pnl_usd": 1.5,
                   "cost_basis_usd": 2.0}]
        allocated, unmatched, ambiguous = allocate_events(trades, events)
        self.assertEqual(allocated["a"][0]["cost_basis_usd"], 2.0)
        self.assertFalse(unmatched)
        self.assertFalse(ambiguous)

    def test_identical_real_and_shadow_lots_are_separated_by_strategy(self):
        base = {"wallet_address": "w", "market_slug": "m", "outcome": "Yes",
                "opened_at": 10, "closed_at": 20, "allocation_end_at": 20}
        trades = [
            {**base, "id": "real", "strategy": "bot_filtered"},
            {**base, "id": "rehab", "strategy": "shadow_rehab"},
        ]
        events = [{"id": "e", "strategy": "shadow_rehab", "trader_address": "w",
                   "market_slug": "m", "outcome": "Yes", "timestamp": 15, "pnl_usd": 1}]
        allocated, unmatched, ambiguous = allocate_events(trades, events)
        self.assertEqual([row["id"] for row in allocated["rehab"]], ["e"])
        self.assertNotIn("real", allocated)
        self.assertFalse(unmatched)
        self.assertFalse(ambiguous)

    def test_same_second_events_follow_durable_sequence_not_uuid_sort(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE paper_trade(
                  id TEXT PRIMARY KEY,strategy TEXT,wallet_address TEXT,market_slug TEXT,
                  outcome TEXT,status TEXT,opened_at INTEGER,closed_at INTEGER,close_reason TEXT,
                  cost_basis_usd REAL,realized_pnl_usd REAL,is_phantom INTEGER DEFAULT 0,
                  phantom_classifier_version TEXT,is_demo_data INTEGER DEFAULT 0,
                  our_shares REAL,total_acquired_shares REAL,phantom_reason TEXT,
                  phantom_classified_at INTEGER
                );
                CREATE TABLE bot_event_log(
                  id TEXT PRIMARY KEY,event_sequence INTEGER NOT NULL UNIQUE,timestamp INTEGER,
                  event_type TEXT,trader_address TEXT,market_slug TEXT,outcome TEXT,side TEXT,
                  payload_json TEXT NOT NULL
                );
                CREATE TABLE bot_event_sequence_counter(
                  singleton INTEGER PRIMARY KEY,next_value INTEGER NOT NULL
                );
                INSERT INTO bot_event_sequence_counter VALUES(1,103);
                INSERT INTO paper_trade(
                  id,strategy,wallet_address,market_slug,outcome,status,opened_at,closed_at,
                  close_reason,cost_basis_usd,realized_pnl_usd,is_phantom,
                  phantom_classifier_version,is_demo_data
                ) VALUES('lot','bot_filtered','w','m','Yes','closed',10,20,
                  'source_sell',10,2,0,NULL,0);
            """)
            # UUID lexical order is deliberately opposite causal insertion
            # order.  Both events share the same second.
            conn.execute(
                "INSERT INTO bot_event_log VALUES(?,?,?,?,?,?,?,?,?)",
                ("z-first", 101, 20, "paper_sell", "w", "m", "Yes", "SELL",
                 json.dumps({"pnl_usd": 1, "cost_basis_usd": 4,
                             "our_shares_closed": 4, "our_shares_remaining": 6})),
            )
            conn.execute(
                "INSERT INTO bot_event_log VALUES(?,?,?,?,?,?,?,?,?)",
                ("a-second", 102, 20, "paper_sell", "w", "m", "Yes", "SELL",
                 json.dumps({"pnl_usd": 1, "cost_basis_usd": 6,
                             "our_shares_closed": 6, "our_shares_remaining": 0})),
            )
            conn.commit()
            conn.close()

            report = reconcile(path)
            self.assertEqual(
                [(row["event_id"], row["event_sequence"])
                 for row in report["event_allocations"]],
                [("z-first", 101), ("a-second", 102)],
            )
            self.assertEqual(report["event_count_by_strategy"], {"bot_filtered": 2})
            self.assertEqual(report["event_count_by_type"], {"paper_sell": 2})
            self.assertTrue(report["source_snapshot"]["sequence_coherent"])
            self.assertEqual(report["source_snapshot"]["realized_event_count"], 2)
            self.assertEqual(report["historical_unreconstructable_event_count"], 0)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
