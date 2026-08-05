#!/usr/bin/env python3

import unittest

from passive_integrity import measure_passive_integrity


class TestPassiveIntegrityMeasurement(unittest.TestCase):
    def test_derives_queue_and_book_age_from_existing_timestamps(self):
        measurement = measure_passive_integrity(
            {
                "_ingress_monotonic_ns": 990_000_000,
                "_parse_started_monotonic_ns": 995_000_000,
                "_parse_completed_monotonic_ns": 999_000_000,
                "_enqueued_monotonic_ns": 1_000_000_000,
            },
            {
                "book_timestamp_ms": 10_000,
                "received_timestamp_ms": 10_020,
                "received_monotonic_ns": 1_040_000_000,
                "parse_started_monotonic_ns": 1_041_000_000,
                "parse_completed_monotonic_ns": 1_044_000_000,
            },
            decision_monotonic_ns=1_050_000_000,
            decision_timestamp_ms=10_030,
        )
        self.assertEqual(measurement.decision_queue_age_ms, 50.0)
        self.assertEqual(measurement.decision_path_age_ms, 60.0)
        self.assertEqual(measurement.signal_parse_duration_ms, 4.0)
        self.assertEqual(measurement.book_parse_duration_ms, 3.0)
        self.assertEqual(measurement.book_server_age_at_receive_ms, 20.0)
        self.assertEqual(measurement.book_local_residence_ms, 10.0)
        self.assertEqual(measurement.effective_book_age_ms, 30.0)
        self.assertEqual(measurement.quality_flags, ())

    def test_pre_parse_gil_stall_is_included_in_buy_safety_age(self):
        measurement = measure_passive_integrity(
            {
                "_ingress_monotonic_ns": 1_000_000_000,
                "_parse_started_monotonic_ns": 1_001_000_000,
                "_parse_completed_monotonic_ns": 1_201_000_000,
                "_enqueued_monotonic_ns": 1_205_000_000,
            },
            {
                "book_timestamp_ms": 10_000,
                "received_timestamp_ms": 10_000,
                "received_monotonic_ns": 1_210_000_000,
                "parse_started_monotonic_ns": 1_211_000_000,
                "parse_completed_monotonic_ns": 1_212_000_000,
            },
            decision_monotonic_ns=1_220_000_000,
            decision_timestamp_ms=10_220,
        )
        self.assertEqual(measurement.signal_parse_duration_ms, 200.0)
        self.assertEqual(measurement.scheduler_lag_ms, 220.0)
        self.assertEqual(measurement.decision_queue_age_ms, 15.0)

    def test_never_subtracts_epoch_time_from_monotonic_time(self):
        measurement = measure_passive_integrity(
            {"_enqueued_monotonic_ns": 1_000_000},
            {
                "book_timestamp_ms": 1_700_000_000_000,
                "received_timestamp_ms": 1_700_000_000_005,
                "received_monotonic_ns": 1_500_000,
            },
            decision_monotonic_ns=2_000_000,
            decision_timestamp_ms=1_700_000_000_006,
        )
        self.assertEqual(measurement.effective_book_age_ms, 5.5)

    def test_server_clock_ahead_is_clamped_and_exposed_as_uncertainty(self):
        measurement = measure_passive_integrity(
            {"_enqueued_monotonic_ns": 1_000_000},
            {
                "book_timestamp_ms": 10_050,
                "received_timestamp_ms": 10_000,
                "received_monotonic_ns": 1_000_000,
            },
            decision_monotonic_ns=2_000_000,
            decision_timestamp_ms=10_001,
        )
        self.assertEqual(measurement.book_server_age_at_receive_ms, 0.0)
        self.assertEqual(measurement.clock_uncertainty_ms, 50.0)
        self.assertIn("book_server_clock_ahead", measurement.quality_flags)

    def test_missing_timestamps_remain_unknown_and_fail_interlock_validation(self):
        measurement = measure_passive_integrity(
            {}, {}, decision_monotonic_ns=2_000_000, decision_timestamp_ms=10_000
        )
        sample = measurement.to_interlock_sample(
            sequence_coherent=False,
            minimum_audit_available=True,
        )
        self.assertIsNone(sample.market_data_age_ms)
        self.assertIsNone(sample.decision_queue_age_ms)
        self.assertIn("missing_book_wall_timestamp", measurement.quality_flags)


if __name__ == "__main__":
    unittest.main()
