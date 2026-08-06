#!/usr/bin/env python3
import unittest
import json
from pathlib import Path
import tempfile

from audit_resolution_timing import classify_resolution_timing, parse_timestamp
from analyze_clean_cohort import bootstrap, estimators, load_resolution_audit


class TestResolutionTiming(unittest.TestCase):
    def test_factual_phantom_requires_resolution_before_entry(self):
        metadata = {"closed": True, "closedTime": "2026-08-01T00:00:00Z"}
        verdict, timestamp, reason = classify_resolution_timing(1_800_000_000, metadata)
        self.assertEqual(verdict, "phantom")
        self.assertIsNotNone(timestamp)
        self.assertEqual(reason, "resolution_precedes_paper_entry")

    def test_entry_before_resolution_is_legit(self):
        resolution = parse_timestamp("2026-08-01T00:00:00Z")
        verdict, _, _ = classify_resolution_timing(resolution - 1, {
            "closed": True, "umaEndDate": "2026-08-01T00:00:00Z",
        })
        self.assertEqual(verdict, "legit")

    def test_missing_timestamp_stays_unknown(self):
        self.assertEqual(classify_resolution_timing(1, {"closed": True})[0], "unknown")

    def test_currently_open_market_is_not_historical_resolution_phantom(self):
        self.assertEqual(classify_resolution_timing(1, {"closed": False})[0], "legit")


class TestClusterBootstrap(unittest.TestCase):
    def test_resolution_audit_separates_phantom_and_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.json"
            path.write_text(json.dumps({
                "audit_version": "resolution-timing-v1",
                "rows": [
                    {"id": "p", "verdict": "phantom"},
                    {"id": "u", "verdict": "unknown"},
                    {"id": "l", "verdict": "legit"},
                ],
            }))
            phantom, unknown, summary = load_resolution_audit(path)
        self.assertEqual(phantom, {"p"})
        self.assertEqual(unknown, {"u"})
        self.assertEqual(summary["factual_phantom_count_excluded"], 1)
        self.assertEqual(summary["unknown_count_excluded_from_fact_clean_estimators"], 1)

    def test_estimators_separate_equal_and_cost_weighting(self):
        rows = [
            {"wallet": "a", "pnl": 1.0, "cost": 1.0},
            {"wallet": "b", "pnl": -2.0, "cost": 10.0},
        ]
        result = estimators(rows)
        self.assertAlmostEqual(result["equal_weight_mean_return"], 0.4)
        self.assertAlmostEqual(result["cost_weighted_roi"], -1 / 11)

    def test_bootstrap_is_deterministic_and_returns_intervals(self):
        rows = [
            {"wallet": "a", "pnl": 1.0, "cost": 2.0},
            {"wallet": "a", "pnl": -1.0, "cost": 2.0},
            {"wallet": "b", "pnl": 2.0, "cost": 2.0},
        ]
        first = bootstrap(rows, draws=100, seed=7, cluster_wallet=True)
        second = bootstrap(rows, draws=100, seed=7, cluster_wallet=True)
        self.assertEqual(first, second)
        self.assertLessEqual(
            first["cost_weighted_roi"]["lower_95"],
            first["cost_weighted_roi"]["upper_95"],
        )


if __name__ == "__main__":
    unittest.main()
