#!/usr/bin/env python3
import unittest

from qualification_gates import evaluate_ttp_gates


POLICY = {
    "ttp_price_read_success_rate_min": 0.99,
    "ttp_executable_bid_rate_min": 0.99,
    "legacy_quarantine_count_max": 3,
    "quarantined_cost_basis_to_equity_max": 0.10,
}


class TestQualificationGates(unittest.TestCase):
    def test_zero_fetch_denominator_is_unknown_not_vacuous_pass(self):
        gates = evaluate_ttp_gates(
            fetch_attempted=0, successful=0, executable=0,
            quarantined_count=3, quarantined_cost_basis_usd=10,
            conservative_equity_usd=1000, new_quarantines=0, policy=POLICY,
        )
        self.assertEqual(gates[0].status, "UNKNOWN")
        self.assertEqual(gates[1].status, "UNKNOWN")
        self.assertEqual(gates[2].status, "PASS")

    def test_new_quarantine_resets_window_even_inside_legacy_count_allowance(self):
        gates = evaluate_ttp_gates(
            fetch_attempted=100, successful=100, executable=100,
            quarantined_count=3, quarantined_cost_basis_usd=10,
            conservative_equity_usd=1000, new_quarantines=1, policy=POLICY,
        )
        by_name = {gate.name: gate for gate in gates}
        self.assertEqual(by_name["structural_unpriceable_count"].status, "PASS")
        self.assertEqual(by_name["new_quarantines_in_window"].status, "FAIL")


if __name__ == "__main__":
    unittest.main()
