#!/usr/bin/env python3

import csv
import json
from pathlib import Path
import tempfile
import unittest

from phase0_autopsy import analyze_with_polars, normalize_journals


def _signal(signal_id, side="BUY"):
    execution = (
        {
            "average_price_micros": 510_000,
            "requested_usd_micros": 3_000_000,
            "filled_usd_micros": 3_000_000,
            "shares_micros": 5_882_352,
            "fill_ratio_ppm": 1_000_000,
            "insufficient_liquidity": False,
        }
        if side == "BUY" else
        {
            "average_price_micros": 490_000,
            "requested_shares_micros": 2_000_000,
            "filled_shares_micros": 2_000_000,
            "liquidation_ratio_ppm": 1_000_000,
            "insufficient_liquidity": False,
        }
    )
    return {
        "event_type": "wallet_signal",
        "signal_event_id": signal_id,
        "correlation_id": signal_id,
        "api_trade_id": "trade",
        "first_local_seen_timestamp_ms": 1_000,
        "source_reported_timestamp_ms": 900,
        "reported_visibility_lag_ms": 100,
        "signal": {
            "user_address": "0xABC", "market_slug": "market",
            "outcome": "Yes", "side": side, "price": 0.5,
        },
        "decision_book_age_ns": 1,
        "decision_book": {
            "received_timestamp_ms": 999,
            "received_monotonic_ns": 999_000_000,
            "local_generation": 4,
            "reconnect_epoch": 1,
            "book_hash": "t0",
        },
        "shadow_lifecycle": {
            "tiers": {"3": {"action": f"hypothetical_{side.lower()}", "execution": execution}}
        },
        "quality_flags": [],
        "error": None,
    }


def _delayed(signal_id, price, known=True, event_id=None):
    return {
        "event_type": "delayed_book_observation",
        "event_id": event_id or f"{signal_id}:t+100ms",
        "correlation_id": signal_id,
        "target_delay_ms": 100,
        "capture_lateness_ns": 2_000_000,
        "book_known_by_capture_deadline": known,
        "target_snapshot_status": (
            "latest_generation_was_already_known_by_target"
            if known else "not_proven_known_by_target"
        ),
        "book": {
            "received_timestamp_ms": 1_090,
            "received_monotonic_ns": 1_090_000_000,
            "local_generation": 5,
            "reconnect_epoch": 1,
            "book_hash": "later",
        },
        "tier_execution_observations": {
            "3": {
                "average_price_micros": price,
                "requested_usd_micros": 3_000_000,
                "filled_usd_micros": 3_000_000,
                "shares_micros": 5_660_377,
                "fill_ratio_ppm": 1_000_000,
                "insufficient_liquidity": False,
            }
        },
    }


class TestPhase0Autopsy(unittest.TestCase):
    def _normalize(self, records):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        source = Path(temp.name) / "source.jsonl"
        output = Path(temp.name) / "normalized.csv"
        source.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
        audit = normalize_journals([source], output)
        with output.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        return audit, rows

    def test_exact_correlation_join_and_buy_deterioration(self):
        audit, rows = self._normalize([_delayed("other", 520_000), _signal("signal"), _delayed("signal", 530_000)])
        self.assertEqual(audit["unmatched_delayed_observations"], 1)
        self.assertEqual(len(rows), 2)
        delayed = next(row for row in rows if row["observation_label"] == "T+100ms")
        self.assertEqual(delayed["signal_event_id"], "signal")
        self.assertEqual(int(delayed["deterioration_from_source_micros"]), 30_000)
        self.assertEqual(int(delayed["deterioration_from_t0_micros"]), 20_000)
        self.assertEqual(delayed["causal_valid"], "True")

    def test_unknown_by_deadline_is_censored_not_silently_accepted(self):
        _audit, rows = self._normalize([_signal("signal"), _delayed("signal", 530_000, known=False)])
        delayed = next(row for row in rows if row["observation_label"] == "T+100ms")
        self.assertEqual(delayed["causal_valid"], "False")
        self.assertEqual(delayed["causal_status"], "not_proven_known_by_target")

    def test_sell_deterioration_has_correct_sign(self):
        signal = _signal("sell", side="SELL")
        delayed = _delayed("sell", 470_000)
        delayed["tier_execution_observations"]["3"] = {
            "average_price_micros": 470_000,
            "requested_shares_micros": 2_000_000,
            "filled_shares_micros": 2_000_000,
            "liquidation_ratio_ppm": 1_000_000,
            "insufficient_liquidity": False,
        }
        _audit, rows = self._normalize([signal, delayed])
        row = next(item for item in rows if item["observation_label"] == "T+100ms")
        self.assertEqual(int(row["deterioration_from_source_micros"]), 30_000)
        self.assertEqual(int(row["deterioration_from_t0_micros"]), 20_000)

    def test_duplicate_delayed_event_is_counted_and_not_double_weighted(self):
        delayed = _delayed("signal", 530_000)
        audit, rows = self._normalize([_signal("signal"), delayed, delayed])
        self.assertEqual(audit["duplicate_delayed_observations"], 1)
        self.assertEqual(len(rows), 2)

    def test_optional_polars_pipeline_writes_parquet_summary_and_plot(self):
        try:
            import polars  # noqa: F401
            import plotly  # noqa: F401
        except ImportError:
            self.skipTest("offline research dependencies are not installed")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        source = Path(temp.name) / "source.jsonl"
        output = Path(temp.name) / "normalized.csv"
        source.write_text(
            "".join(
                json.dumps(record) + "\n"
                for record in [_signal("signal"), _delayed("signal", 530_000)]
            ),
            encoding="utf-8",
        )
        normalize_journals([source], output)
        result = analyze_with_polars(output, temp.name, write_plot=True)
        self.assertTrue(Path(result["parquet"]).exists())
        self.assertTrue(Path(result["summary"]).exists())
        self.assertTrue(Path(result["plot"]).exists())
        summary = json.loads(Path(result["summary"]).read_text(encoding="utf-8"))
        self.assertEqual(len(summary), 2)
        delayed = next(item for item in summary if item["observation_delay_ms"] == 100)
        self.assertEqual(delayed["t0_deterioration_p50_micros"], 20_000.0)


if __name__ == "__main__":
    unittest.main()
