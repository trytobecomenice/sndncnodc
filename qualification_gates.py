#!/usr/bin/env python3
"""Pure, shared qualification gates for protocol v2.

The evaluator and operator preflight import this module.  Keeping the math
here prevents a dashboard/preflight implementation from quietly disagreeing
with the adjudicator about denominators or UNKNOWN semantics.
"""

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class GateResult:
    name: str
    status: str  # PASS | FAIL | UNKNOWN
    observed: dict
    threshold: dict
    reason: str

    def to_dict(self):
        return asdict(self)


def minimum_rate_gate(name, numerator, denominator, minimum):
    if denominator <= 0:
        return GateResult(name, "UNKNOWN", {
            "numerator": numerator, "denominator": denominator, "rate": None,
        }, {"minimum_rate": minimum}, "zero denominator is not evidence of health")
    rate = numerator / denominator
    return GateResult(name, "PASS" if rate >= minimum else "FAIL", {
        "numerator": numerator, "denominator": denominator, "rate": rate,
    }, {"minimum_rate": minimum},
        "rate meets threshold" if rate >= minimum else "rate below threshold")


def maximum_count_gate(name, observed, maximum):
    return GateResult(name, "PASS" if observed <= maximum else "FAIL",
                      {"count": observed}, {"maximum_count": maximum},
                      "count within threshold" if observed <= maximum else "count exceeds threshold")


def maximum_ratio_gate(name, numerator, denominator, maximum):
    if denominator is None or denominator <= 0:
        return GateResult(name, "UNKNOWN", {
            "numerator": numerator, "denominator": denominator, "ratio": None,
        }, {"maximum_ratio": maximum}, "positive denominator required")
    ratio = numerator / denominator
    return GateResult(name, "PASS" if ratio <= maximum else "FAIL", {
        "numerator": numerator, "denominator": denominator, "ratio": ratio,
    }, {"maximum_ratio": maximum},
        "ratio within threshold" if ratio <= maximum else "ratio exceeds threshold")


def evaluate_ttp_gates(*, fetch_attempted, successful, executable, quarantined_count,
                       quarantined_cost_basis_usd, conservative_equity_usd,
                       new_quarantines, policy):
    """Return separately diagnosable pipeline (A) and inventory (B) gates."""
    return [
        minimum_rate_gate("ttp_pipeline_price_read", successful, fetch_attempted,
                          policy["ttp_price_read_success_rate_min"]),
        minimum_rate_gate("ttp_pipeline_executable_bid", executable, fetch_attempted,
                          policy["ttp_executable_bid_rate_min"]),
        maximum_count_gate("structural_unpriceable_count", quarantined_count,
                           policy["legacy_quarantine_count_max"]),
        maximum_ratio_gate("structural_unpriceable_equity_ratio",
                           quarantined_cost_basis_usd, conservative_equity_usd,
                           policy["quarantined_cost_basis_to_equity_max"]),
        maximum_count_gate("new_quarantines_in_window", new_quarantines, 0),
    ]


def gate_failures(gates):
    return [gate.name for gate in gates if gate.status != "PASS"]
