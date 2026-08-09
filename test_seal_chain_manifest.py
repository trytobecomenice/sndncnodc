#!/usr/bin/env python3
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest

from seal_chain_manifest import (
    record_manifest, recover_pending_manifest, stage_manifest, verify_manifest,
)


class TestSealChainManifest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "app.db"
        self.manifest_path = Path(self.temp.name) / "external" / "seal.json"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE paper_trade_event_seal("
                     "id TEXT PRIMARY KEY,range_start INTEGER,range_end INTEGER,"
                     "chain_sha256 TEXT)")
        conn.execute("INSERT INTO paper_trade_event_seal VALUES('a',1,10,'head-a')")
        conn.execute("INSERT INTO paper_trade_event_seal VALUES('b',11,20,'head-b')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_external_anchor_passes_and_cannot_be_overwritten(self):
        manifest = record_manifest(self.db_path, self.manifest_path, now=123)
        self.assertEqual(manifest["seal_count"], 2)
        self.assertEqual(manifest["chain_head_sha256"], "head-b")
        self.assertEqual(verify_manifest(self.db_path, self.manifest_path)["status"], "PASS")
        with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
            record_manifest(self.db_path, self.manifest_path)

    def test_valid_prefix_truncation_is_detected(self):
        record_manifest(self.db_path, self.manifest_path, now=123)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM paper_trade_event_seal WHERE id='b'")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(RuntimeError, "external-manifest mismatch"):
            verify_manifest(self.db_path, self.manifest_path)

    def test_pending_anchor_only_recovers_against_exact_committed_state(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        pending, final = stage_manifest(conn, self.manifest_path.parent, "crash", now=123)
        conn.close()
        result = recover_pending_manifest(self.db_path, self.manifest_path.parent)
        self.assertEqual(result["status"], "RECOVERED")
        self.assertFalse(pending.exists())
        self.assertTrue(final.exists())


if __name__ == "__main__":
    unittest.main()
