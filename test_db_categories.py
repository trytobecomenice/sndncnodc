#!/usr/bin/env python3
"""Unit tests for db.py's category-scoring read/write functions, added
2026-07-23: load_market_categories()/save_market_category() (bot_market_event's
new `category` column) and get_wallet_composite_scores()'s extended return
shape (wallet_profile's new `category_scores_json` column).

Uses a TEMPORARY SQLite file, never the real data/app.db — same precedent as
test_db_prune.py.

Run: python3 -m unittest test_db_categories -v
"""

import json
import os
import sqlite3
import tempfile
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
            "CREATE TABLE bot_market_event (market_slug TEXT PRIMARY KEY, event_slug TEXT NOT NULL, "
            "category TEXT, holding_rewards_enabled INTEGER, resolved_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE wallet_profile (id TEXT PRIMARY KEY, wallet_address TEXT NOT NULL, "
            "composite_score REAL, win_rate REAL, trade_count_all_time INTEGER, "
            "capital_multiplier REAL, category_scores_json TEXT, derived_metrics_source TEXT, "
            "derived_metrics_version TEXT, derived_metrics_ready INTEGER NOT NULL DEFAULT 0)"
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.remove(self.tmp_path)

    def _raw_conn(self):
        return sqlite3.connect(self.tmp_path)


class TestMarketCategoryPersistence(_TempDbTestCase):
    def test_save_then_load_roundtrips(self):
        conn = self._raw_conn()
        conn.execute(
            "INSERT INTO bot_market_event (market_slug, event_slug, resolved_at) VALUES (?, ?, ?)",
            ("some-market", "some-event", 0),
        )
        conn.commit()
        conn.close()

        db.save_market_category("some-market", "crypto")
        self.assertEqual(db.load_market_categories(), {"some-market": "crypto"})

    def test_rows_with_no_category_yet_are_absent_not_none(self):
        conn = self._raw_conn()
        conn.execute(
            "INSERT INTO bot_market_event (market_slug, event_slug, resolved_at) VALUES (?, ?, ?)",
            ("uncategorized-market", "some-event", 0),
        )
        conn.commit()
        conn.close()

        self.assertEqual(db.load_market_categories(), {})

    def test_save_category_for_a_market_with_no_existing_row_is_a_harmless_noop(self):
        # save_market_event() must run first in practice (category is an
        # UPDATE, not an upsert) — confirms this doesn't raise if it hasn't.
        db.save_market_category("never-resolved-market", "sports")
        self.assertEqual(db.load_market_categories(), {})


class TestMarketEventHoldingRewardsPersistence(_TempDbTestCase):
    """save_market_event()'s new holding_rewards_enabled parameter (added
    2026-07-23 for point 3.1) — an audit/documentation field, not read by
    any scoring/sizing logic (see bot_market_event schema comment)."""

    def test_save_then_read_back_true(self):
        db.save_market_event("rewarded-market", "some-event", True)
        conn = self._raw_conn()
        row = conn.execute(
            "SELECT event_slug, holding_rewards_enabled FROM bot_market_event WHERE market_slug = ?",
            ("rewarded-market",),
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "some-event")
        self.assertEqual(bool(row[1]), True)

    def test_save_then_read_back_false(self):
        db.save_market_event("unrewarded-market", "some-event", False)
        conn = self._raw_conn()
        row = conn.execute(
            "SELECT holding_rewards_enabled FROM bot_market_event WHERE market_slug = ?",
            ("unrewarded-market",),
        ).fetchone()
        conn.close()
        self.assertEqual(bool(row[0]), False)

    def test_defaults_to_none_when_omitted(self):
        # Existing call sites/tests that only care about event_slug shouldn't
        # need updating — the parameter is optional.
        db.save_market_event("legacy-call-site-market", "some-event")
        conn = self._raw_conn()
        row = conn.execute(
            "SELECT holding_rewards_enabled FROM bot_market_event WHERE market_slug = ?",
            ("legacy-call-site-market",),
        ).fetchone()
        conn.close()
        self.assertIsNone(row[0])

    def test_upsert_updates_holding_rewards_enabled_on_conflict(self):
        db.save_market_event("re-resolved-market", "event-a", None)
        db.save_market_event("re-resolved-market", "event-a", True)
        conn = self._raw_conn()
        row = conn.execute(
            "SELECT holding_rewards_enabled FROM bot_market_event WHERE market_slug = ?",
            ("re-resolved-market",),
        ).fetchone()
        conn.close()
        self.assertEqual(bool(row[0]), True)


class TestGetWalletCompositeScoresWithCategories(_TempDbTestCase):
    def _insert_wallet(self, address, composite_score, category_scores_json=None,
                        win_rate=None, trade_count_all_time=None):
        conn = self._raw_conn()
        conn.execute(
            "INSERT INTO wallet_profile (id, wallet_address, composite_score, win_rate, "
            "trade_count_all_time, category_scores_json, derived_metrics_source, "
            "derived_metrics_version, derived_metrics_ready) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (address, address, composite_score, win_rate, trade_count_all_time, category_scores_json,
             "polymarket_official_raw_global", "global-score-v1", 1),
        )
        conn.commit()
        conn.close()

    def test_wallet_with_no_category_data_gets_empty_categories_dict(self):
        self._insert_wallet("0xAAA", 0.6)
        result = db.get_wallet_composite_scores()
        self.assertEqual(
            result["0xaaa"],
            {
                "composite": 0.6, "composite_win_rate": None, "composite_trade_count": None,
                "capital_multiplier": None, "categories": {},
            },
        )

    def test_composite_win_rate_and_trade_count_are_surfaced(self):
        # Added 2026-07-24 for half-Kelly sizing's composite-tier fallback
        # (Rule 25) — the wallet's LIFETIME rolling win rate, distinct from
        # any single category's win_rate.
        self._insert_wallet("0xAAA", 0.6, win_rate=0.58, trade_count_all_time=413)
        result = db.get_wallet_composite_scores()
        self.assertEqual(result["0xaaa"]["composite_win_rate"], 0.58)
        self.assertEqual(result["0xaaa"]["composite_trade_count"], 413)

    def test_parses_category_scores_extracting_score_pnl_t_stat_win_rate_trade_count(self):
        blob = json.dumps({
            "crypto": {"score": 0.81, "trade_count": 22, "win_rate": 0.68, "avg_pnl_usd": 14.2, "pnl_t_stat": 2.4},
            "sports": {"score": 0.15, "trade_count": 6, "win_rate": 0.33, "avg_pnl_usd": -3.1, "pnl_t_stat": -1.8},
        })
        self._insert_wallet("0xBBB", 0.5, blob)
        result = db.get_wallet_composite_scores()
        self.assertEqual(
            result["0xbbb"]["categories"],
            {
                "crypto": {"score": 0.81, "pnl_t_stat": 2.4, "win_rate": 0.68, "trade_count": 22},
                "sports": {"score": 0.15, "pnl_t_stat": -1.8, "win_rate": 0.33, "trade_count": 6},
            },
        )

    def test_missing_optional_fields_degrade_to_none_not_a_crash(self):
        # e.g. a category written before pnl_t_stat/win_rate/trade_count existed,
        # or the n<2 edge case for pnl_t_stat specifically.
        blob = json.dumps({"crypto": {"score": 0.81, "avg_pnl_usd": 14.2}})
        self._insert_wallet("0xBBB", 0.5, blob)
        result = db.get_wallet_composite_scores()
        self.assertEqual(
            result["0xbbb"]["categories"],
            {"crypto": {"score": 0.81, "pnl_t_stat": None, "win_rate": None, "trade_count": None}},
        )

    def test_malformed_json_degrades_to_empty_categories_not_a_crash(self):
        self._insert_wallet("0xCCC", 0.4, "{not valid json")
        result = db.get_wallet_composite_scores()
        self.assertEqual(
            result["0xccc"],
            {
                "composite": 0.4, "composite_win_rate": None, "composite_trade_count": None,
                "capital_multiplier": None, "categories": {},
            },
        )

    def test_keys_are_lowercased(self):
        self._insert_wallet("0xDDDeeeFFF", 0.3)
        result = db.get_wallet_composite_scores()
        self.assertIn("0xdddeeefff", result)

    def test_legacy_unprovenanced_metrics_fail_closed(self):
        self._insert_wallet("0xLEGACY", 0.99)
        conn = self._raw_conn()
        conn.execute("UPDATE wallet_profile SET derived_metrics_ready=0 WHERE wallet_address='0xLEGACY'")
        conn.commit()
        conn.close()
        result = db.get_wallet_composite_scores()["0xlegacy"]
        self.assertIsNone(result["composite"])
        self.assertEqual(result["categories"], {})

    def test_unknown_version_cannot_unlock_official_source(self):
        self._insert_wallet("0xWRONGVERSION", 0.99)
        conn = self._raw_conn()
        conn.execute(
            "UPDATE wallet_profile SET derived_metrics_version='future-unreviewed' "
            "WHERE wallet_address='0xWRONGVERSION'"
        )
        conn.commit()
        conn.close()
        result = db.get_wallet_composite_scores()["0xwrongversion"]
        self.assertIsNone(result["composite"])
        self.assertEqual(result["categories"], {})


if __name__ == "__main__":
    unittest.main()
