#!/usr/bin/env python3
"""Unit tests for db.py's live_whale_event/token_registry consumer-sweep
functions (2026-07-24, Rule 29's consumer sweep):
get_unconsumed_whale_events() (the INNER JOIN + LIMIT behavior) and
mark_whale_event_consumed() (the idempotency write).

Uses a TEMPORARY SQLite file, never the real data/app.db — same precedent as
test_db_categories.py/test_db_pending_execution.py.

Run: python3 -m unittest test_db_whale_events -v
"""

import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import config
import db


class _TempDbTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.tmp_path)
        conn.execute(
            "CREATE TABLE live_whale_event (id TEXT PRIMARY KEY, wallet_address TEXT NOT NULL, "
            "contract_address TEXT NOT NULL, event_type TEXT NOT NULL, direction TEXT NOT NULL, "
            "token_id TEXT NOT NULL, share_amount TEXT NOT NULL, usdc_amount REAL, price REAL, "
            "tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL, block_number INTEGER NOT NULL, "
            "detected_at INTEGER NOT NULL, consumed_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE token_registry (token_id TEXT PRIMARY KEY, market_slug TEXT NOT NULL, "
            "outcome TEXT NOT NULL, updated_at INTEGER NOT NULL)"
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()
        self._batch_limit_patcher = patch.object(config, "WHALE_EVENT_SWEEP_BATCH_LIMIT", 50)
        self._batch_limit_patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._batch_limit_patcher.stop()
        os.remove(self.tmp_path)

    def _insert_event(self, event_id, token_id, wallet="0xTrader", direction="buy",
                       price=0.5, usdc_amount=5.0, block_number=100, log_index=0,
                       consumed_at=None):
        conn = sqlite3.connect(self.tmp_path)
        conn.execute(
            "INSERT INTO live_whale_event (id, wallet_address, contract_address, event_type, "
            "direction, token_id, share_amount, usdc_amount, price, tx_hash, log_index, "
            "block_number, detected_at, consumed_at) "
            "VALUES (?, ?, 'ctf', 'TransferSingle', ?, ?, '10', ?, ?, 'txhash', ?, ?, ?, ?)",
            (event_id, wallet, direction, token_id, usdc_amount, price, log_index,
             block_number, int(time.time()), consumed_at),
        )
        conn.commit()
        conn.close()

    def _insert_registry(self, token_id, market_slug="some-market", outcome="Yes"):
        conn = sqlite3.connect(self.tmp_path)
        conn.execute(
            "INSERT INTO token_registry (token_id, market_slug, outcome, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (token_id, market_slug, outcome, int(time.time())),
        )
        conn.commit()
        conn.close()


class TestGetUnconsumedWhaleEvents(_TempDbTestCase):
    def test_returns_row_with_joined_market_slug_and_outcome(self):
        self._insert_event("ev1", token_id="tok1")
        self._insert_registry("tok1", market_slug="will-x-happen", outcome="Yes")

        rows = db.get_unconsumed_whale_events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "ev1")
        self.assertEqual(rows[0]["market_slug"], "will-x-happen")
        self.assertEqual(rows[0]["outcome"], "Yes")

    def test_inner_join_excludes_unmatched_token_id(self):
        # No matching token_registry row for "tok-unknown" -- this event
        # must not appear at all (not force-processed with a missing
        # market_slug/outcome), so it naturally retries on a later sweep.
        self._insert_event("ev1", token_id="tok-unknown")
        rows = db.get_unconsumed_whale_events()
        self.assertEqual(rows, [])

    def test_already_consumed_rows_are_excluded(self):
        self._insert_event("ev1", token_id="tok1", consumed_at=int(time.time()))
        self._insert_registry("tok1")
        rows = db.get_unconsumed_whale_events()
        self.assertEqual(rows, [])

    def test_ordered_by_block_number_then_log_index(self):
        self._insert_registry("tok1")
        self._insert_event("ev-late", token_id="tok1", block_number=200, log_index=0)
        self._insert_event("ev-early", token_id="tok1", block_number=100, log_index=5)
        self._insert_event("ev-mid", token_id="tok1", block_number=100, log_index=6)

        rows = db.get_unconsumed_whale_events()
        self.assertEqual([r["id"] for r in rows], ["ev-early", "ev-mid", "ev-late"])

    def test_respects_batch_limit(self):
        self._insert_registry("tok1")
        for i in range(5):
            self._insert_event(f"ev{i}", token_id="tok1", block_number=100 + i)
        with patch.object(config, "WHALE_EVENT_SWEEP_BATCH_LIMIT", 2):
            rows = db.get_unconsumed_whale_events()
        self.assertEqual(len(rows), 2)


class TestMarkWhaleEventConsumed(_TempDbTestCase):
    def test_stamps_a_unix_epoch_integer_not_a_string(self):
        self._insert_event("ev1", token_id="tok1")
        self._insert_registry("tok1")
        before = int(time.time())

        db.mark_whale_event_consumed("ev1")

        conn = sqlite3.connect(self.tmp_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT consumed_at FROM live_whale_event WHERE id = 'ev1'").fetchone()
        conn.close()
        self.assertIsInstance(row["consumed_at"], int)
        self.assertGreaterEqual(row["consumed_at"], before)

        # Once consumed, get_unconsumed_whale_events() must no longer return it.
        self.assertEqual(db.get_unconsumed_whale_events(), [])


class TestGetUnconsumedWhaleEventsWithoutRegistryMatch(_TempDbTestCase):
    """The 'unknown token' on-demand fallback's input query (2026-07-25,
    Rule 30 addendum) — the LEFT JOIN counterpart to
    get_unconsumed_whale_events()'s INNER JOIN."""

    def test_returns_event_with_no_registry_row_at_all(self):
        self._insert_event("ev1", token_id="tok-unknown")
        rows = db.get_unconsumed_whale_events_without_registry_match()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "ev1")
        self.assertEqual(rows[0]["token_id"], "tok-unknown")

    def test_excludes_event_that_already_has_a_registry_match(self):
        self._insert_event("ev1", token_id="tok1")
        self._insert_registry("tok1")
        rows = db.get_unconsumed_whale_events_without_registry_match()
        self.assertEqual(rows, [])

    def test_excludes_already_consumed_rows(self):
        self._insert_event("ev1", token_id="tok-unknown", consumed_at=int(time.time()))
        rows = db.get_unconsumed_whale_events_without_registry_match()
        self.assertEqual(rows, [])

    def test_includes_detected_at_for_the_ttl_check(self):
        self._insert_event("ev1", token_id="tok-unknown")
        rows = db.get_unconsumed_whale_events_without_registry_match()
        self.assertIn("detected_at", rows[0])
        self.assertIsInstance(rows[0]["detected_at"], int)


class TestUpsertTokenRegistryRow(_TempDbTestCase):
    def test_inserts_a_new_row(self):
        db.upsert_token_registry_row("tok-new", "some-market", "Yes")
        conn = sqlite3.connect(self.tmp_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM token_registry WHERE token_id = 'tok-new'").fetchone()
        conn.close()
        self.assertEqual(row["market_slug"], "some-market")
        self.assertEqual(row["outcome"], "Yes")

    def test_upserts_over_an_existing_row_rather_than_erroring(self):
        self._insert_registry("tok1", market_slug="old-slug", outcome="No")
        db.upsert_token_registry_row("tok1", "new-slug", "Yes")

        conn = sqlite3.connect(self.tmp_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM token_registry WHERE token_id = 'tok1'").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_slug"], "new-slug")
        self.assertEqual(rows[0]["outcome"], "Yes")

    def test_resolving_the_fallback_makes_the_event_visible_to_the_fast_path(self):
        # End-to-end within this test module: an event with no match, then
        # upserted, must show up via get_unconsumed_whale_events() (the
        # INNER-JOIN fast path) afterward, not just disappear from both.
        self._insert_event("ev1", token_id="tok-new")
        self.assertEqual(db.get_unconsumed_whale_events(), [])  # not matched yet

        db.upsert_token_registry_row("tok-new", "resolved-market", "Yes")

        rows = db.get_unconsumed_whale_events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_slug"], "resolved-market")


if __name__ == "__main__":
    unittest.main()
