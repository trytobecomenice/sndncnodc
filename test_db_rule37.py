#!/usr/bin/env python3
"""Unit tests for db.py's Rule 37 additions (2026-07-27): Shadow Rehab
persistence (load_shadow_positions/save_shadow_positions/
get_shadow_rehab_returns, plus the strategy-scoped _maybe_close_paper_trade)
and pool-refill queries (get_muted_wallets/get_ever_tracked_wallets/
get_pool_refill_candidates).

Uses a TEMPORARY SQLite file, never the real data/app.db — same precedent
as the other test_db_*.py files.

Run: python3 -m unittest test_db_rule37 -v
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
            "CREATE TABLE paper_trade (id TEXT PRIMARY KEY, strategy TEXT NOT NULL "
            "DEFAULT 'bot_filtered', wallet_address TEXT NOT NULL, market_slug TEXT NOT NULL, "
            "market_title TEXT, outcome TEXT NOT NULL, source_price REAL, source_size_usd REAL, "
            "our_size_usd REAL NOT NULL, cost_basis_usd REAL NOT NULL DEFAULT 0, "
            "our_shares REAL NOT NULL, avg_entry_price REAL NOT NULL, buy_count INTEGER NOT NULL "
            "DEFAULT 0, status TEXT NOT NULL, opened_at INTEGER, closed_at INTEGER, "
            "close_reason TEXT, realized_pnl_usd REAL, peak_profit_pct REAL NOT NULL DEFAULT 0, "
            "last_priced_at INTEGER, "
            "decision_journal_id TEXT, is_demo_data INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE wallet_profile (id TEXT PRIMARY KEY, wallet_address TEXT NOT NULL UNIQUE, "
            "nickname TEXT, status TEXT NOT NULL DEFAULT 'watch', circuit_breaker_muted INTEGER "
            "NOT NULL DEFAULT 0, mute_reason TEXT, muted_at INTEGER, consecutive_losses INTEGER "
            "NOT NULL DEFAULT 0, recent_results_json TEXT, composite_score REAL, win_rate REAL, "
            "trade_count_all_time INTEGER, category TEXT, created_at INTEGER, updated_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE bot_event_log (id TEXT PRIMARY KEY, timestamp INTEGER, event_type TEXT, "
            "trader_address TEXT, market_slug TEXT, outcome TEXT, side TEXT, payload_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE decision_journal (id TEXT PRIMARY KEY, created_at INTEGER, "
            "wallet_address TEXT NOT NULL, observed_trade_id TEXT, market_slug TEXT NOT NULL, "
            "outcome TEXT NOT NULL, side TEXT, decision_type TEXT NOT NULL, "
            "decision_reason TEXT NOT NULL, score_breakdown_json TEXT, rule_set_version INTEGER, "
            "resulting_action TEXT, linked_paper_trade_id TEXT, source TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        self._patcher = patch.object(config, "SQLITE_PATH", self.tmp_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.remove(self.tmp_path)

    def _insert_paper_trade(self, wallet_address, market_slug, outcome, strategy="bot_filtered",
                             status="open", our_shares=10.0, cost_basis_usd=5.0,
                             realized_pnl_usd=None, closed_at=None, avg_entry_price=0.5,
                             buy_count=1):
        conn = sqlite3.connect(self.tmp_path)
        conn.execute(
            "INSERT INTO paper_trade (id, strategy, wallet_address, market_slug, outcome, "
            "our_size_usd, cost_basis_usd, our_shares, avg_entry_price, buy_count, status, "
            "opened_at, closed_at, realized_pnl_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"pt-{wallet_address}-{market_slug}-{outcome}-{status}-{time.time_ns()}", strategy,
             wallet_address, market_slug, outcome, cost_basis_usd, cost_basis_usd, our_shares,
             avg_entry_price, buy_count, status, int(time.time()), closed_at, realized_pnl_usd),
        )
        conn.commit()
        conn.close()

    def _insert_wallet_profile(self, wallet_address, composite_score=None, win_rate=None,
                                trade_count_all_time=None, category=None, circuit_breaker_muted=0):
        conn = sqlite3.connect(self.tmp_path)
        conn.execute(
            "INSERT INTO wallet_profile (id, wallet_address, composite_score, win_rate, "
            "trade_count_all_time, category, circuit_breaker_muted) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"wp-{wallet_address}", wallet_address, composite_score, win_rate,
             trade_count_all_time, category, circuit_breaker_muted),
        )
        conn.commit()
        conn.close()


class TestShadowPositionPersistence(_TempDbTestCase):
    def test_load_returns_empty_when_no_shadow_rows_exist(self):
        self.assertEqual(db.load_shadow_positions(), {})

    def test_save_then_load_round_trip(self):
        shadow_positions = {"0xabc|some-market|Yes": {
            "shares": 10.0, "cost_basis_usd": 5.0, "avg_entry_price": 0.5, "buy_count": 1,
        }}
        db.save_shadow_positions(shadow_positions)
        loaded = db.load_shadow_positions()
        self.assertEqual(loaded, shadow_positions)

    def test_challenger_ledger_is_separate_from_rehab(self):
        challenger = {"0xabc|some-market|Yes": {
            "shares": 10.0, "cost_basis_usd": 5.0, "avg_entry_price": 0.5, "buy_count": 1,
        }}
        db.save_shadow_positions(challenger, "shadow_challenger")
        self.assertEqual(db.load_shadow_positions("shadow_challenger"), challenger)
        self.assertEqual(db.load_shadow_positions("shadow_rehab"), {})

    def test_real_bot_filtered_positions_never_appear_in_shadow_load(self):
        self._insert_paper_trade("0xabc", "some-market", "Yes", strategy="bot_filtered")
        self.assertEqual(db.load_shadow_positions(), {})

    def test_save_updates_an_existing_open_shadow_row_not_a_duplicate_insert(self):
        key = "0xabc|some-market|Yes"
        db.save_shadow_positions({key: {"shares": 10.0, "cost_basis_usd": 5.0,
                                          "avg_entry_price": 0.5, "buy_count": 1}})
        db.save_shadow_positions({key: {"shares": 20.0, "cost_basis_usd": 10.0,
                                          "avg_entry_price": 0.5, "buy_count": 2}})
        conn = sqlite3.connect(self.tmp_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM paper_trade WHERE strategy='shadow_rehab'").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["our_shares"], 20.0)

    def test_key_missing_from_dict_gets_closed_as_reconciled_missing_from_state(self):
        key = "0xabc|some-market|Yes"
        db.save_shadow_positions({key: {"shares": 10.0, "cost_basis_usd": 5.0,
                                          "avg_entry_price": 0.5, "buy_count": 1}})
        db.save_shadow_positions({})  # key no longer present
        conn = sqlite3.connect(self.tmp_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM paper_trade WHERE strategy='shadow_rehab'").fetchone()
        conn.close()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["close_reason"], "reconciled_missing_from_state")


class TestGetShadowRehabReturns(_TempDbTestCase):
    def test_returns_computed_correctly_from_closed_shadow_rows(self):
        self._insert_paper_trade("0xabc", "m1", "Yes", strategy="shadow_rehab", status="closed",
                                  cost_basis_usd=5.0, realized_pnl_usd=2.5, closed_at=100)
        self._insert_paper_trade("0xabc", "m2", "Yes", strategy="shadow_rehab", status="closed",
                                  cost_basis_usd=10.0, realized_pnl_usd=-5.0, closed_at=200)
        returns = db.get_shadow_rehab_returns("0xabc")
        self.assertEqual(sorted(returns), sorted([0.5, -0.5]))

    def test_open_shadow_rows_are_excluded(self):
        self._insert_paper_trade("0xabc", "m1", "Yes", strategy="shadow_rehab", status="open")
        self.assertEqual(db.get_shadow_rehab_returns("0xabc"), [])

    def test_bot_filtered_rows_never_leak_into_shadow_returns(self):
        self._insert_paper_trade("0xabc", "m1", "Yes", strategy="bot_filtered", status="closed",
                                  cost_basis_usd=5.0, realized_pnl_usd=2.5, closed_at=100)
        self.assertEqual(db.get_shadow_rehab_returns("0xabc"), [])

    def test_limit_caps_to_most_recent(self):
        for i in range(5):
            self._insert_paper_trade("0xabc", f"m{i}", "Yes", strategy="shadow_rehab",
                                      status="closed", cost_basis_usd=10.0,
                                      realized_pnl_usd=float(i), closed_at=100 + i)
        returns = db.get_shadow_rehab_returns("0xabc", limit=2)
        self.assertEqual(len(returns), 2)
        # Most recent first: closed_at=104 (pnl=4) then 103 (pnl=3).
        self.assertEqual(returns, [0.4, 0.3])

    def test_wallet_address_matched_case_insensitively(self):
        self._insert_paper_trade("0xabc", "m1", "Yes", strategy="shadow_rehab", status="closed",
                                  cost_basis_usd=5.0, realized_pnl_usd=2.5, closed_at=100)
        self.assertEqual(db.get_shadow_rehab_returns("0XABC"), [0.5])


class TestMaybeClosePaperTradeStrategyScoping(_TempDbTestCase):
    """The strategy-scoped close (2026-07-27) must never cross-close a
    real position when a shadow_rehab_sell event fires, and vice versa."""

    def test_shadow_rehab_sell_only_closes_the_shadow_row(self):
        self._insert_paper_trade("0xabc", "m1", "Yes", strategy="bot_filtered", status="open")
        self._insert_paper_trade("0xabc", "m1", "Yes", strategy="shadow_rehab", status="open")
        db.append_log({
            "event_type": "shadow_rehab_sell", "trader_address": "0xabc", "timestamp": "t",
            "market_slug": "m1", "outcome": "Yes", "our_shares_remaining": 0.0,
            "pnl_usd": 1.0,
        })
        conn = sqlite3.connect(self.tmp_path)
        conn.row_factory = sqlite3.Row
        rows = {r["strategy"]: r["status"] for r in conn.execute("SELECT * FROM paper_trade").fetchall()}
        conn.close()
        self.assertEqual(rows["shadow_rehab"], "closed")
        self.assertEqual(rows["bot_filtered"], "open")

    def test_shadow_challenger_sell_only_closes_challenger_row(self):
        self._insert_paper_trade("0xabc", "m1", "Yes", strategy="bot_filtered", status="open")
        self._insert_paper_trade("0xabc", "m1", "Yes", strategy="shadow_challenger", status="open")
        db.append_log({
            "event_type": "shadow_challenger_sell", "trader_address": "0xabc", "timestamp": "t",
            "market_slug": "m1", "outcome": "Yes", "our_shares_remaining": 0.0,
            "pnl_usd": 1.0,
        })
        conn = sqlite3.connect(self.tmp_path)
        rows = dict(conn.execute("SELECT strategy,status FROM paper_trade").fetchall())
        conn.close()
        self.assertEqual(rows["shadow_challenger"], "closed")
        self.assertEqual(rows["bot_filtered"], "open")

    def test_paper_sell_only_closes_the_real_row(self):
        self._insert_paper_trade("0xabc", "m1", "Yes", strategy="bot_filtered", status="open")
        self._insert_paper_trade("0xabc", "m1", "Yes", strategy="shadow_rehab", status="open")
        db.append_log({
            "event_type": "paper_sell", "trader_address": "0xabc", "timestamp": "t",
            "market_slug": "m1", "outcome": "Yes", "our_shares_remaining": 0.0,
            "pnl_usd": 1.0,
        })
        conn = sqlite3.connect(self.tmp_path)
        conn.row_factory = sqlite3.Row
        rows = {r["strategy"]: r["status"] for r in conn.execute("SELECT * FROM paper_trade").fetchall()}
        conn.close()
        self.assertEqual(rows["bot_filtered"], "closed")
        self.assertEqual(rows["shadow_rehab"], "open")


class TestGetMutedWallets(_TempDbTestCase):
    def test_returns_only_muted_lowercased(self):
        self._insert_wallet_profile("0xAAAA", circuit_breaker_muted=1)
        self._insert_wallet_profile("0xBBBB", circuit_breaker_muted=0)
        self.assertEqual(db.get_muted_wallets(), {"0xaaaa"})

    def test_empty_when_nothing_muted(self):
        self._insert_wallet_profile("0xAAAA", circuit_breaker_muted=0)
        self.assertEqual(db.get_muted_wallets(), set())


class TestGetEverTrackedWallets(_TempDbTestCase):
    def test_includes_dropped_and_currently_tracked_alike(self):
        self._insert_paper_trade("0xaaaa", "m1", "Yes", strategy="bot_filtered")
        self._insert_paper_trade("0xbbbb", "m2", "No", strategy="bot_filtered")
        self.assertEqual(db.get_ever_tracked_wallets(), {"0xaaaa", "0xbbbb"})

    def test_shadow_rehab_rows_do_not_count_as_ever_tracked(self):
        self._insert_paper_trade("0xaaaa", "m1", "Yes", strategy="shadow_rehab")
        self.assertEqual(db.get_ever_tracked_wallets(), set())


class TestGetPoolRefillCandidates(_TempDbTestCase):
    def test_excludes_addresses_in_the_exclusion_set(self):
        self._insert_wallet_profile("0xaaaa", composite_score=0.9)
        self._insert_wallet_profile("0xbbbb", composite_score=0.8)
        candidates = db.get_pool_refill_candidates({"0xaaaa"}, min_composite_score=0.2)
        self.assertEqual([c["wallet_address"] for c in candidates], ["0xbbbb"])

    def test_exclusion_is_case_insensitive(self):
        self._insert_wallet_profile("0xAAAA", composite_score=0.9)
        candidates = db.get_pool_refill_candidates({"0xaaaa"}, min_composite_score=0.2)
        self.assertEqual(candidates, [])

    def test_below_min_score_excluded(self):
        self._insert_wallet_profile("0xaaaa", composite_score=0.05)
        candidates = db.get_pool_refill_candidates(set(), min_composite_score=0.2)
        self.assertEqual(candidates, [])

    def test_ranked_best_score_first(self):
        self._insert_wallet_profile("0xaaaa", composite_score=0.5)
        self._insert_wallet_profile("0xbbbb", composite_score=0.9)
        candidates = db.get_pool_refill_candidates(set(), min_composite_score=0.2)
        self.assertEqual([c["wallet_address"] for c in candidates], ["0xbbbb", "0xaaaa"])

    def test_limit_caps_result_count(self):
        self._insert_wallet_profile("0xaaaa", composite_score=0.9)
        self._insert_wallet_profile("0xbbbb", composite_score=0.8)
        candidates = db.get_pool_refill_candidates(set(), min_composite_score=0.2, limit=1)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["wallet_address"], "0xaaaa")


if __name__ == "__main__":
    unittest.main()
