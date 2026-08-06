#!/usr/bin/env python3

import json
from pathlib import Path
import tempfile
import unittest

from inspect_phase0_soak import inspect_journal


class TestInspectPhase0Soak(unittest.TestCase):
    def test_streaming_summary_keeps_unknown_causality_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "soak.jsonl"
            records = [
                {"event_type": "recorder_started", "timestamp_ms": 1_000},
                {"event_type": "poll_cycle", "poll_completed_ms": 1_100,
                 "duration_ms": 100, "errors": []},
                {"event_type": "wallet_signal", "first_local_seen_timestamp_ms": 1_200,
                 "reported_visibility_lag_ms": 200, "signal": {"side": "BUY"},
                 "quality_flags": [], "shadow_lifecycle": {"ledger_after": {
                     "realized_pnl_micros": {"5": -50_000}
                 }}},
                {"event_type": "delayed_book_observation", "target_delay_ms": 100,
                 "capture_lateness_ns": 2_000_000,
                 "book_known_by_capture_deadline": True},
                {"event_type": "recorder_stopped", "timestamp_ms": 2_000},
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            report = inspect_journal(path)
        self.assertEqual(report["malformed_json_lines"], 0)
        self.assertEqual(report["signal_sides"], {"BUY": 1})
        self.assertEqual(report["delayed_book"]["100"]["availability_ratio"], 1.0)
        self.assertEqual(report["latest_realized_shadow_pnl_usd"]["5"], -0.05)
        self.assertIn("do not prove", report["interpretation_guard"])


if __name__ == "__main__":
    unittest.main()
