#!/usr/bin/env python3

import unittest

from entry_interlock import (
    EntryInterlockState,
    IntegritySample,
    IntegrityThresholds,
    InterlockStatus,
    entry_interlock_state_from_risk_value,
    evaluate_entry_interlock,
)


THRESHOLDS = IntegrityThresholds(
    max_event_loop_lag_ms=100,
    max_market_data_age_ms=1_000,
    max_decision_queue_age_ms=200,
    recovery_window_ms=1_000,
    min_recovery_samples=3,
)


def sample(at_ms, lag=10, age=100, queue_age=20, coherent=True, audit=True):
    return IntegritySample(at_ms, lag, age, queue_age, coherent, audit)


class TestEntryInterlock(unittest.TestCase):
    def test_single_execution_integrity_breach_trips_immediately(self):
        transition = evaluate_entry_interlock(
            EntryInterlockState(), sample(100, lag=101), THRESHOLDS
        )
        self.assertTrue(transition.changed)
        self.assertTrue(transition.state.active)
        self.assertEqual(transition.state.reasons, ("event_loop_lag_exceeded",))

    def test_sequence_uncertainty_trips_even_when_latency_is_low(self):
        transition = evaluate_entry_interlock(
            EntryInterlockState(), sample(100, coherent=False), THRESHOLDS
        )
        self.assertIn("sequence_not_coherent", transition.state.reasons)

    def test_recovery_requires_both_count_and_window(self):
        state = evaluate_entry_interlock(
            EntryInterlockState(), sample(0, age=1_001), THRESHOLDS
        ).state
        state = evaluate_entry_interlock(state, sample(100), THRESHOLDS).state
        state = evaluate_entry_interlock(state, sample(900), THRESHOLDS).state
        state = evaluate_entry_interlock(state, sample(1_099), THRESHOLDS).state
        self.assertTrue(state.active)
        transition = evaluate_entry_interlock(state, sample(1_100), THRESHOLDS)
        self.assertTrue(transition.changed)
        self.assertEqual(transition.state.status, InterlockStatus.HEALTHY)

    def test_new_breach_resets_recovery_progress(self):
        state = evaluate_entry_interlock(
            EntryInterlockState(), sample(0, queue_age=201), THRESHOLDS
        ).state
        state = evaluate_entry_interlock(state, sample(100), THRESHOLDS).state
        self.assertEqual(state.healthy_recovery_samples, 1)
        state = evaluate_entry_interlock(state, sample(200, audit=False), THRESHOLDS).state
        self.assertEqual(state.healthy_recovery_samples, 0)
        self.assertIsNone(state.recovery_started_monotonic_ms)

    def test_invalid_or_regressing_metrics_fail_closed(self):
        state = evaluate_entry_interlock(EntryInterlockState(), sample(100), THRESHOLDS).state
        transition = evaluate_entry_interlock(state, sample(99, lag=float("nan")), THRESHOLDS)
        self.assertIn("monotonic_clock_regressed", transition.state.reasons)
        self.assertIn("event_loop_lag_invalid", transition.state.reasons)

    def test_risk_value_is_json_safe_only_while_active(self):
        self.assertIsNone(EntryInterlockState().risk_value())
        state = evaluate_entry_interlock(
            EntryInterlockState(), sample(100, coherent=False), THRESHOLDS
        ).state
        self.assertEqual(state.risk_value()["status"], "interlocked")
        self.assertEqual(state.risk_value()["reasons"], ["sequence_not_coherent"])

    def test_active_state_restarts_recovery_window_from_current_monotonic_clock(self):
        state = entry_interlock_state_from_risk_value(
            {"active": True, "status": "interlocked", "reasons": ["stale"]},
            observed_monotonic_ms=5_000,
        )
        self.assertTrue(state.active)
        self.assertEqual(state.changed_at_monotonic_ms, 5_000)
        self.assertEqual(state.recovery_started_monotonic_ms, None)

    def test_malformed_persisted_value_restores_fail_closed(self):
        state = entry_interlock_state_from_risk_value({"active": "maybe"}, 5_000)
        self.assertTrue(state.active)
        self.assertEqual(state.reasons, ("persisted_entry_interlock_state_malformed",))


if __name__ == "__main__":
    unittest.main()
