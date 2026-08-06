#!/usr/bin/env python3
import sqlite3
import unittest

from audit_paper_ledger import CLASSIFIER_VERSION, apply_report, build_report, classify_rows


class TestPaperLedgerClassifier(unittest.TestCase):
    @staticmethod
    def _row(row_id, slug, opened, closed, reason="resolved", cost=10.0, pnl=1.0):
        return {
            "id": row_id,
            "market_slug": slug,
            "opened_at": opened,
            "closed_at": closed,
            "close_reason": reason,
            "cost_basis_usd": cost,
            "realized_pnl_usd": pnl,
        }

    def test_reopen_after_local_resolution_is_confirmed(self):
        rows = [
            self._row("first", "market", 100, 200),
            self._row("replay", "market", 201, 250),
        ]
        result = classify_rows(rows)
        self.assertNotIn("first", result)
        self.assertEqual(result["replay"], ["reopened_after_local_resolution"])

    def test_same_market_opened_before_resolution_is_not_replay(self):
        rows = [
            self._row("a", "market", 100, 300),
            self._row("b", "market", 200, 310),
        ]
        self.assertEqual(classify_rows(rows), {})

    def test_dated_slug_waits_until_full_utc_day_has_passed(self):
        event_day = 1_788_134_400  # 2026-08-31 00:00:00 UTC
        rows = [
            self._row("same-day", "game-2026-08-31-home", event_day + 80_000, event_day + 82_000),
            self._row("after-day", "game-2026-08-31-home", event_day + 86_400, event_day + 90_000),
        ]
        result = classify_rows(rows)
        self.assertNotIn("same-day", result)
        self.assertIn("dated_slug_after_event_day", result["after-day"])

    def test_non_resolved_dated_close_is_not_labeled_by_date_alone(self):
        rows = [self._row(
            "sell", "game-2026-08-31-home", 1_788_134_400 + 90_000,
            1_788_134_400 + 91_000, reason="source_sell",
        )]
        self.assertEqual(classify_rows(rows), {})


class TestPaperLedgerApply(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
        CREATE TABLE paper_trade (
          id TEXT PRIMARY KEY, strategy TEXT, market_slug TEXT, status TEXT,
          opened_at INTEGER, closed_at INTEGER, close_reason TEXT,
          cost_basis_usd REAL, realized_pnl_usd REAL, is_demo_data INTEGER DEFAULT 0,
          is_phantom INTEGER DEFAULT 0 NOT NULL, phantom_reason TEXT,
          phantom_classifier_version TEXT, phantom_classified_at INTEGER
        );
        CREATE TABLE bot_risk_state (
          key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at INTEGER
        );
        INSERT INTO paper_trade VALUES
          ('a','bot_filtered','market','closed',100,200,'resolved',10,5,0,0,NULL,NULL,NULL),
          ('b','bot_filtered','market','closed',201,250,'resolved',10,5,0,0,NULL,NULL,NULL);
        INSERT INTO bot_risk_state VALUES ('equity_hwm','1853.36',1);
        INSERT INTO bot_risk_state VALUES ('drawdown_warning','{}',1);
        """)

    def tearDown(self):
        self.conn.close()

    def test_apply_marks_only_candidate_and_can_reset_hwm(self):
        report = build_report(self.conn)
        self.assertEqual(report["confirmed_phantom_candidates"]["count"], 1)
        apply_report(self.conn, report, reset_hwm=True)
        rows = self.conn.execute(
            "SELECT id,is_phantom,phantom_reason,phantom_classifier_version "
            "FROM paper_trade ORDER BY id"
        ).fetchall()
        self.assertEqual(tuple(rows[0]), ("a", 0, None, None))
        self.assertEqual(tuple(rows[1])[0:2], ("b", 1))
        self.assertEqual(rows[1][2], "reopened_after_local_resolution")
        self.assertEqual(rows[1][3], CLASSIFIER_VERSION)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bot_risk_state WHERE key IN ('equity_hwm','drawdown_warning')"
        ).fetchone()[0], 0)

    def test_open_replay_is_quarantined_without_becoming_realized_pnl(self):
        self.conn.execute(
            "INSERT INTO paper_trade VALUES "
            "('c','bot_filtered','market','open',251,NULL,NULL,7,NULL,0,0,NULL,NULL,NULL)"
        )
        report = build_report(self.conn)
        self.assertEqual(report["confirmed_phantom_candidates"]["count"], 1)
        self.assertEqual(report["confirmed_open_phantom_candidates"], {
            "count": 1, "cost_basis_usd": 7.0,
        })
        apply_report(self.conn, report)
        self.assertEqual(self.conn.execute(
            "SELECT is_phantom FROM paper_trade WHERE id='c'"
        ).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
