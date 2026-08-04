#!/usr/bin/env python3
"""Unit tests for db.py's wallet_approval_request CRUD (2026-08-01, Telegram
wallet-approval workflow) — the single gate every promotion path queues
through instead of writing wallet_profile.status='track'/'bench' directly.

Uses a TEMPORARY SQLite file, never the real data/app.db — same precedent as
test_db_pending_execution.py.

Run: python3 -m unittest test_db_wallet_approval -v
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
            "CREATE TABLE wallet_approval_request (id TEXT PRIMARY KEY, "
            "wallet_address TEXT NOT NULL, requested_tier TEXT NOT NULL, "
            "source TEXT NOT NULL, category TEXT, score_snapshot_json TEXT NOT NULL, "
            "reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
            "telegram_message_id INTEGER, telegram_chat_id TEXT, "
            "created_at INTEGER NOT NULL, resolved_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE wallet_profile (id TEXT PRIMARY KEY, "
            "wallet_address TEXT NOT NULL UNIQUE, nickname TEXT, status TEXT NOT NULL DEFAULT 'watch', "
            "status_reason TEXT, status_changed_at INTEGER, circuit_breaker_muted INTEGER DEFAULT 0, "
            "mute_reason TEXT, muted_at INTEGER, created_at INTEGER, updated_at INTEGER)"
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.remove(self.tmp_path)

    def _raw_conn(self):
        conn = sqlite3.connect(self.tmp_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert_request(self, request_id="req-1", wallet_address="0xtrader",
                         requested_tier="track", status="pending", telegram_message_id=None):
        conn = self._raw_conn()
        try:
            conn.execute(
                "INSERT INTO wallet_approval_request (id, wallet_address, requested_tier, source, "
                "category, score_snapshot_json, reason, status, telegram_message_id, telegram_chat_id, "
                "created_at) VALUES (?, ?, ?, 'category_quota', 'crypto', '{}', 'test reason', ?, ?, "
                "NULL, ?)",
                (request_id, wallet_address, requested_tier, status, telegram_message_id, int(time.time())),
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_wallet_profile(self, wallet_address="0xtrader", status="watch"):
        conn = self._raw_conn()
        try:
            conn.execute(
                "INSERT INTO wallet_profile (id, wallet_address, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"wp-{wallet_address}", wallet_address, status, int(time.time()), int(time.time())),
            )
            conn.commit()
        finally:
            conn.close()


class TestGetPendingWalletApprovalRequests(_TempDbTestCase):
    def test_lists_only_pending_oldest_first(self):
        self._insert_request("req-old", wallet_address="0xa")
        self._insert_request("req-new", wallet_address="0xb")
        self._insert_request("req-resolved", wallet_address="0xc", status="approved")

        # Force a distinguishable created_at ordering (both inserted "now").
        conn = self._raw_conn()
        conn.execute("UPDATE wallet_approval_request SET created_at = 100 WHERE id = 'req-old'")
        conn.execute("UPDATE wallet_approval_request SET created_at = 200 WHERE id = 'req-new'")
        conn.commit()
        conn.close()

        rows = db.get_pending_wallet_approval_requests()
        self.assertEqual([r["id"] for r in rows], ["req-old", "req-new"])

    def test_unsent_only_filters_to_null_telegram_message_id(self):
        self._insert_request("req-sent", wallet_address="0xa", telegram_message_id=555)
        self._insert_request("req-unsent", wallet_address="0xb")

        rows = db.get_pending_wallet_approval_requests(unsent_only=True)
        self.assertEqual([r["id"] for r in rows], ["req-unsent"])

    def test_returns_empty_list_when_nothing_pending(self):
        self.assertEqual(db.get_pending_wallet_approval_requests(), [])


class TestGetWalletApprovalRequest(_TempDbTestCase):
    def test_returns_row_by_id(self):
        self._insert_request("req-1", wallet_address="0xa", requested_tier="bench")
        row = db.get_wallet_approval_request("req-1")
        self.assertIsNotNone(row)
        self.assertEqual(row["wallet_address"], "0xa")
        self.assertEqual(row["requested_tier"], "bench")

    def test_includes_wallet_profile_nickname_via_left_join(self):
        self._insert_request("req-1", wallet_address="0xa")
        self._insert_wallet_profile("0xa", status="watch")
        conn = self._raw_conn()
        conn.execute("UPDATE wallet_profile SET nickname = ? WHERE wallet_address = ?", ("Big Whale", "0xa"))
        conn.commit()
        conn.close()

        row = db.get_wallet_approval_request("req-1")
        self.assertEqual(row["nickname"], "Big Whale")

    def test_nickname_is_none_when_wallet_never_scored(self):
        self._insert_request("req-1", wallet_address="0xnever-scored")
        row = db.get_wallet_approval_request("req-1")
        self.assertIsNone(row["nickname"])

    def test_returns_none_for_missing_id(self):
        self.assertIsNone(db.get_wallet_approval_request("does-not-exist"))


class TestMarkWalletApprovalRequestSent(_TempDbTestCase):
    def test_records_telegram_message_and_chat_id(self):
        self._insert_request("req-1")
        db.mark_wallet_approval_request_sent("req-1", 12345, "chat-abc")
        row = db.get_wallet_approval_request("req-1")
        self.assertEqual(row["telegram_message_id"], 12345)
        self.assertEqual(row["telegram_chat_id"], "chat-abc")


class TestResolveWalletApprovalRequest(_TempDbTestCase):
    def test_challenger_approval_is_transactional_one_in_one_out(self):
        self._insert_wallet_profile("0xnew", status="challenger")
        self._insert_wallet_profile("0xold", status="track")
        conn = self._raw_conn()
        conn.execute("UPDATE wallet_profile SET circuit_breaker_muted=1 WHERE wallet_address='0xold'")
        conn.execute(
            "INSERT INTO wallet_approval_request (id,wallet_address,requested_tier,source,category,"
            "score_snapshot_json,reason,status,created_at) VALUES "
            "('req-swap','0xnew','track','challenger_shadow',NULL,?,'passed','pending',?)",
            ('{"replacementWalletAddress":"0xold"}', int(time.time())),
        )
        conn.commit()
        conn.close()

        self.assertTrue(db.resolve_wallet_approval_request("req-swap", "approved"))
        conn = self._raw_conn()
        statuses = dict(conn.execute(
            "SELECT wallet_address,status FROM wallet_profile WHERE wallet_address IN ('0xnew','0xold')"
        ).fetchall())
        conn.close()
        self.assertEqual(statuses, {"0xnew": "track", "0xold": "retiring"})

    def test_approve_flips_wallet_profile_status_to_requested_tier(self):
        self._insert_request("req-1", wallet_address="0xa", requested_tier="track")
        self._insert_wallet_profile("0xa", status="watch")

        result = db.resolve_wallet_approval_request("req-1", "approved")
        self.assertTrue(result)

        request = db.get_wallet_approval_request("req-1")
        self.assertEqual(request["status"], "approved")
        self.assertIsNotNone(request["resolved_at"])

        conn = self._raw_conn()
        profile = conn.execute("SELECT status, status_reason FROM wallet_profile WHERE wallet_address = ?",
                                ("0xa",)).fetchone()
        conn.close()
        self.assertEqual(profile["status"], "track")
        self.assertIn("Telegram", profile["status_reason"])

    def test_approve_bench_tier_flips_status_to_bench_not_track(self):
        self._insert_request("req-1", wallet_address="0xa", requested_tier="bench")
        self._insert_wallet_profile("0xa", status="watch")

        db.resolve_wallet_approval_request("req-1", "approved")

        conn = self._raw_conn()
        profile = conn.execute("SELECT status FROM wallet_profile WHERE wallet_address = ?", ("0xa",)).fetchone()
        conn.close()
        self.assertEqual(profile["status"], "bench")

    def test_reject_does_not_touch_wallet_profile(self):
        self._insert_request("req-1", wallet_address="0xa", requested_tier="track")
        self._insert_wallet_profile("0xa", status="watch")

        db.resolve_wallet_approval_request("req-1", "rejected")

        request = db.get_wallet_approval_request("req-1")
        self.assertEqual(request["status"], "rejected")

        conn = self._raw_conn()
        profile = conn.execute("SELECT status FROM wallet_profile WHERE wallet_address = ?", ("0xa",)).fetchone()
        conn.close()
        self.assertEqual(profile["status"], "watch")

    def test_returns_false_and_no_ops_for_missing_request(self):
        self.assertFalse(db.resolve_wallet_approval_request("does-not-exist", "approved"))

    def test_returns_false_and_no_ops_for_already_resolved_request(self):
        # Guards the double-tap / replayed-getUpdates-offset case: a request
        # that's already 'approved' or 'rejected' must not be resolved again.
        self._insert_request("req-1", wallet_address="0xa", requested_tier="track", status="approved")
        self._insert_wallet_profile("0xa", status="ignore")

        result = db.resolve_wallet_approval_request("req-1", "rejected")
        self.assertFalse(result)

        # Original resolution and wallet_profile status are both untouched.
        request = db.get_wallet_approval_request("req-1")
        self.assertEqual(request["status"], "approved")
        conn = self._raw_conn()
        profile = conn.execute("SELECT status FROM wallet_profile WHERE wallet_address = ?", ("0xa",)).fetchone()
        conn.close()
        self.assertEqual(profile["status"], "ignore")


if __name__ == "__main__":
    unittest.main()
