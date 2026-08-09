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


def minimum_rate_gate(name, numerator, denominator, minimum, minimum_denominator):
    threshold = {"minimum_rate": minimum,
                 "minimum_denominator": minimum_denominator}
    if denominator < minimum_denominator:
        return GateResult(name, "UNKNOWN", {
            "numerator": numerator, "denominator": denominator, "rate": None,
        }, threshold, "denominator below preregistered evidence minimum")
    rate = numerator / denominator
    return GateResult(name, "PASS" if rate >= minimum else "FAIL", {
        "numerator": numerator, "denominator": denominator, "rate": rate,
    }, threshold,
        "rate meets threshold" if rate >= minimum else "rate below threshold")


def minimum_count_gate(name, observed, minimum):
    return GateResult(name, "PASS" if observed >= minimum else "UNKNOWN",
                      {"count": observed}, {"minimum_count": minimum},
                      "count meets evidence minimum" if observed >= minimum
                      else "count below preregistered evidence minimum")


def maximum_count_gate(name, observed, maximum):
    return GateResult(name, "PASS" if observed <= maximum else "FAIL",
                      {"count": observed}, {"maximum_count": maximum},
                      "count within threshold" if observed <= maximum else "count exceeds threshold")


def maximum_ratio_gate(name, numerator, denominator, maximum, minimum_denominator):
    threshold = {"maximum_ratio": maximum,
                 "minimum_denominator": minimum_denominator}
    if denominator is None or denominator < minimum_denominator:
        return GateResult(name, "UNKNOWN", {
            "numerator": numerator, "denominator": denominator, "ratio": None,
        }, threshold, "denominator below preregistered evidence minimum")
    ratio = numerator / denominator
    return GateResult(name, "PASS" if ratio <= maximum else "FAIL", {
        "numerator": numerator, "denominator": denominator, "ratio": ratio,
    }, threshold,
        "ratio within threshold" if ratio <= maximum else "ratio exceeds threshold")


def maximum_rate_gate(name, numerator, denominator, maximum, minimum_denominator):
    threshold = {"maximum_rate": maximum,
                 "minimum_denominator": minimum_denominator}
    if denominator < minimum_denominator:
        return GateResult(name, "UNKNOWN", {
            "numerator": numerator, "denominator": denominator, "rate": None,
        }, threshold, "denominator below preregistered evidence minimum")
    rate = numerator / denominator
    return GateResult(name, "PASS" if rate <= maximum else "FAIL", {
        "numerator": numerator, "denominator": denominator, "rate": rate,
    }, threshold, "rate within threshold" if rate <= maximum else "rate exceeds threshold")


def evaluate_ttp_gates(*, sweep_count, fetch_attempted, successful, executable,
                       quarantined_count, suspected_count, oldest_suspected_age_seconds,
                       quarantined_cost_basis_usd, conservative_equity_usd,
                       new_quarantines, policy):
    """Return separately diagnosable pipeline (A) and inventory (B) gates."""
    return [
        minimum_count_gate("ttp_pipeline_sweep_evidence", sweep_count,
                           policy["ttp_minimum_sweeps"]),
        minimum_rate_gate("ttp_pipeline_price_read", successful, fetch_attempted,
                          policy["ttp_price_read_success_rate_min"],
                          policy["ttp_rate_minimum_fetch_attempts"]),
        minimum_rate_gate("ttp_pipeline_executable_bid", executable, fetch_attempted,
                          policy["ttp_executable_bid_rate_min"],
                          policy["ttp_rate_minimum_fetch_attempts"]),
        maximum_count_gate("unadjudicated_structural_suspects", suspected_count, 0),
        GateResult(
            "structural_suspect_adjudication_sla",
            "PASS" if oldest_suspected_age_seconds is None else
            ("PASS" if oldest_suspected_age_seconds <= policy["structural_suspect_sla_seconds"]
             else "FAIL"),
            {"oldest_age_seconds": oldest_suspected_age_seconds},
            {"maximum_age_seconds": policy["structural_suspect_sla_seconds"]},
            "no active suspect" if oldest_suspected_age_seconds is None else
            ("within adjudication SLA" if oldest_suspected_age_seconds
             <= policy["structural_suspect_sla_seconds"] else "adjudication SLA exceeded"),
        ),
        maximum_count_gate("structural_unpriceable_count", quarantined_count,
                           policy["legacy_quarantine_count_max"]),
        maximum_ratio_gate("structural_unpriceable_equity_ratio",
                           quarantined_cost_basis_usd, conservative_equity_usd,
                           policy["quarantined_cost_basis_to_equity_max"],
                           policy["quarantined_ratio_minimum_equity_usd"]),
        maximum_count_gate("new_quarantines_in_window", new_quarantines, 0),
    ]


def gate_failures(gates):
    return [gate.name for gate in gates if gate.status != "PASS"]
