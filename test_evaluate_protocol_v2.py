#!/usr/bin/env python3
import unittest

from evaluate_protocol_v2 import adjudicate


class TestProtocolAdjudication(unittest.TestCase):
    def test_system_integrity_precedes_every_statistical_result(self):
        self.assertEqual(adjudicate(system_integrity_failure=True, sample_ready=True,
                                    lower_confidence_bound=1), "SYSTEM_INTEGRITY_KILL")

    def test_max_duration_distinguishes_bad_instrument_from_more_waiting(self):
        self.assertEqual(adjudicate(sample_ready=False), "INSUFFICIENT_EVIDENCE")
        self.assertEqual(adjudicate(sample_ready=False, max_duration_reached=True),
                         "INSTRUMENT_INSUFFICIENT")

    def test_stress_can_veto_but_never_create_pass(self):
        self.assertEqual(adjudicate(sample_ready=True, lower_confidence_bound=0.01), "PASS")
        self.assertEqual(adjudicate(sample_ready=True, lower_confidence_bound=0.01,
                                    stress_veto=True), "INCONCLUSIVE")
        self.assertEqual(adjudicate(sample_ready=True, lower_confidence_bound=-0.01),
                         "INCONCLUSIVE")

    def test_primary_kill_rejects(self):
        self.assertEqual(adjudicate(sample_ready=True, primary_kill=True,
                                    lower_confidence_bound=1), "REJECTED")


if __name__ == "__main__":
    unittest.main()
