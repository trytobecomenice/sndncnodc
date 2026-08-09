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
import json
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import config
import db
from ledger_integrity import audit_ledger
from seal_chain_manifest import record_manifest


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
        conn.executescript("""
        CREATE TABLE paper_trade(id TEXT PRIMARY KEY,cumulative_realized_pnl_usd REAL DEFAULT 0,
          realized_event_count INTEGER DEFAULT 0,total_acquired_shares REAL,our_shares REAL,
          status TEXT);
        CREATE TABLE paper_trade_realized_allocation(
          event_id TEXT PRIMARY KEY,paper_trade_id TEXT,event_timestamp INTEGER,event_type TEXT,
          strategy TEXT,pnl_usd REAL,cost_basis_usd REAL,allocation_status TEXT,
          termination_cause TEXT,termination_classifier_version TEXT,
          shares_closed REAL,shares_remaining REAL);
        CREATE TABLE paper_trade_event_seal(
          id TEXT PRIMARY KEY,range_start INTEGER,range_end INTEGER,event_count INTEGER,
          pnl_micros INTEGER,shares_micros INTEGER,canonical_sha256 TEXT,sealer_version TEXT,
          previous_chain_sha256 TEXT,chain_sha256 TEXT,sealed_at INTEGER);
        CREATE UNIQUE INDEX paper_trade_event_seal_range_unique
          ON paper_trade_event_seal(range_start,range_end);
        CREATE TRIGGER paper_trade_event_seal_no_update BEFORE UPDATE ON paper_trade_event_seal
          BEGIN SELECT RAISE(ABORT,'paper_trade_event_seal is append-only'); END;
        CREATE TRIGGER paper_trade_event_seal_no_delete BEFORE DELETE ON paper_trade_event_seal
          BEGIN SELECT RAISE(ABORT,'paper_trade_event_seal is append-only'); END;
        """)
        conn.commit()
        conn.close()
        self._manifest_temp = tempfile.TemporaryDirectory()
        self.manifest_dir = self._manifest_temp.name
        record_manifest(
            self.tmp_path,
            os.path.join(self.manifest_dir, "seal-000000000000-genesis.json"),
            now=1,
        )
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()
        self._manifest_patcher = patch.object(
            config, "LEDGER_SEAL_MANIFEST_DIR", self.manifest_dir
        )
        self._manifest_patcher.start()

    def tearDown(self):
        self._manifest_patcher.stop()
        self._patcher.stop()
        self._manifest_temp.cleanup()
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

    def _insert_realized_evidence(self, allocation_pnl=1.25):
        ts = int(time.time() - 200 * 86400)
        conn = sqlite3.connect(self.tmp_path)
        conn.execute("INSERT INTO paper_trade VALUES('lot',?,1,2,0,'closed')", (allocation_pnl,))
        conn.execute(
            "INSERT INTO bot_event_log(id,timestamp,event_type,payload_json) VALUES(?,?,?,?)",
            ("sell-1", ts, "paper_sell", json.dumps({
                "pnl_usd": 1.25, "our_shares_closed": 2, "our_shares_remaining": 0,
            })),
        )
        conn.execute(
            "INSERT INTO paper_trade_realized_allocation VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sell-1", "lot", ts, "paper_sell", "bot_filtered", allocation_pnl, 1,
             "matched", "SOURCE_EXIT", db._TERMINATION_CLASSIFIER_VERSION, 2, 0),
        )
        conn.commit()
        conn.close()

    def test_realized_evidence_is_sealed_atomically_before_prune(self):
        self._insert_realized_evidence()
        self.assertEqual(db.prune_event_log(retention_days=180), 1)
        conn = sqlite3.connect(self.tmp_path)
        conn.row_factory = sqlite3.Row
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_trade_event_seal").fetchone()[0], 1)
        self.assertEqual(audit_ledger(conn, db._REALIZED_PNL_EVENT_TYPES)["status"], "PASS")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            conn.execute("UPDATE paper_trade_event_seal SET event_count=2")
        conn.close()
        manifests = [name for name in os.listdir(self.manifest_dir)
                     if name.startswith("seal-") and name.endswith(".json")]
        self.assertEqual(len(manifests), 2)  # genesis + committed seal head

    def test_mismatched_economic_evidence_aborts_prune_and_keeps_event(self):
        self._insert_realized_evidence(allocation_pnl=9.0)
        with self.assertRaisesRegex(RuntimeError, "PnL mismatch"):
            db.prune_event_log(retention_days=180)
        self.assertEqual(self._row_count(), 1)

    def test_missing_seal_schema_fails_closed_instead_of_legacy_prune(self):
        self._insert_realized_evidence()
        conn = sqlite3.connect(self.tmp_path)
        conn.execute("DROP TRIGGER paper_trade_event_seal_no_update")
        conn.execute("DROP TRIGGER paper_trade_event_seal_no_delete")
        conn.execute("DROP TABLE paper_trade_event_seal")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(RuntimeError, "without migration-0028 retention seal"):
            db.prune_event_log(retention_days=180)
        self.assertEqual(self._row_count(), 1)

    def test_external_anchor_mismatch_aborts_prune_and_preserves_event(self):
        self._insert_realized_evidence()
        manifest_path = os.path.join(
            self.manifest_dir, "seal-000000000000-genesis.json"
        )
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest["seal_count"] = 9
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        with self.assertRaisesRegex(RuntimeError, "external-manifest mismatch"):
            db.prune_event_log(retention_days=180)
        self.assertEqual(self._row_count(), 1)

    def test_never_reduced_open_lot_still_obeys_share_conservation(self):
        conn = sqlite3.connect(self.tmp_path)
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT INTO paper_trade VALUES('open-lot',0,0,3,2,'open')")
        conn.commit()
        result = audit_ledger(conn, db._REALIZED_PNL_EVENT_TYPES)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["quantity_mismatch_lots"], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
