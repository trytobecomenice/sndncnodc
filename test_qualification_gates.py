#!/usr/bin/env python3
import unittest

from qualification_gates import evaluate_ttp_gates


POLICY = {
    "ttp_price_read_success_rate_min": 0.99,
    "ttp_executable_bid_rate_min": 0.99,
    "ttp_rate_minimum_fetch_attempts": 10,
    "ttp_minimum_sweeps": 2,
    "structural_suspect_sla_seconds": 86400,
    "legacy_quarantine_count_max": 3,
    "quarantined_cost_basis_to_equity_max": 0.10,
    "quarantined_ratio_minimum_equity_usd": 900,
}


class TestQualificationGates(unittest.TestCase):
    def test_zero_fetch_denominator_is_unknown_not_vacuous_pass(self):
        gates = evaluate_ttp_gates(
            sweep_count=2, fetch_attempted=0, successful=0, executable=0,
            suspected_count=0, oldest_suspected_age_seconds=None,
            quarantined_count=3, quarantined_cost_basis_usd=10,
            conservative_equity_usd=1000, new_quarantines=0, policy=POLICY,
        )
        self.assertEqual(gates[1].status, "UNKNOWN")
        self.assertEqual(gates[2].status, "UNKNOWN")

    def test_new_quarantine_resets_window_even_inside_legacy_count_allowance(self):
        gates = evaluate_ttp_gates(
            sweep_count=2, fetch_attempted=100, successful=100, executable=100,
            suspected_count=0, oldest_suspected_age_seconds=None,
            quarantined_count=3, quarantined_cost_basis_usd=10,
            conservative_equity_usd=1000, new_quarantines=1, policy=POLICY,
        )
        by_name = {gate.name: gate for gate in gates}
        self.assertEqual(by_name["structural_unpriceable_count"].status, "PASS")
        self.assertEqual(by_name["new_quarantines_in_window"].status, "FAIL")

    def test_small_nonzero_sample_is_unknown(self):
        gates = evaluate_ttp_gates(
            sweep_count=2, fetch_attempted=2, successful=2, executable=2,
            quarantined_count=0, suspected_count=0,
            oldest_suspected_age_seconds=None, quarantined_cost_basis_usd=0,
            conservative_equity_usd=1000, new_quarantines=0, policy=POLICY,
        )
        by_name = {gate.name: gate for gate in gates}
        self.assertEqual(by_name["ttp_pipeline_price_read"].status, "UNKNOWN")
        self.assertEqual(by_name["ttp_pipeline_executable_bid"].status, "UNKNOWN")

    def test_ratio_is_unknown_below_frozen_equity_denominator(self):
        gates = evaluate_ttp_gates(
            sweep_count=2, fetch_attempted=10, successful=10, executable=10,
            quarantined_count=0, suspected_count=0,
            oldest_suspected_age_seconds=None, quarantined_cost_basis_usd=1,
            conservative_equity_usd=899, new_quarantines=0, policy=POLICY,
        )
        by_name = {gate.name: gate for gate in gates}
        self.assertEqual(by_name["structural_unpriceable_equity_ratio"].status, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
