#!/usr/bin/env python3

import unittest

from passive_integrity import measure_passive_integrity


class TestPassiveIntegrityMeasurement(unittest.TestCase):
    def test_derives_queue_and_book_age_from_existing_timestamps(self):
        measurement = measure_passive_integrity(
            {"_enqueued_monotonic_ns": 1_000_000_000},
            {
                "book_timestamp_ms": 10_000,
                "received_timestamp_ms": 10_020,
                "received_monotonic_ns": 1_040_000_000,
            },
            decision_monotonic_ns=1_050_000_000,
            decision_timestamp_ms=10_030,
        )
        self.assertEqual(measurement.decision_queue_age_ms, 50.0)
        self.assertEqual(measurement.book_server_age_at_receive_ms, 20.0)
        self.assertEqual(measurement.book_local_residence_ms, 10.0)
        self.assertEqual(measurement.effective_book_age_ms, 30.0)
        self.assertEqual(measurement.quality_flags, ())

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
