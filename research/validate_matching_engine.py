#!/usr/bin/env python3
"""Validate the virtual matcher against venue-grade golden observations.

A one-second public trade tape is not accepted as fill ground truth: it mixes
other participants, cancels, replenishment, and unknown queue priority. Cases
must come from venue/SDK golden vectors or attributable micro-live fills.
"""

import argparse
import json
from pathlib import Path

from research.virtual_matching_engine import simulate_cash_buy, simulate_share_sell


ELIGIBLE_EVIDENCE = {"VENUE_GOLDEN_VECTOR", "MICRO_LIVE_ATTRIBUTABLE_FILL"}
METRICS = (
    "filled_cash_micros", "filled_shares_micros", "vwap_price_micros",
    "taker_fee_cash_micros",
)


def _relative_error(predicted, observed):
    if predicted is None or observed is None:
        return None
    return abs(int(predicted) - int(observed)) / max(1, abs(int(observed)))


def validate_cases(cases, maximum_error=0.05):
    results = []
    eligible_count = 0
    for index, case in enumerate(cases):
        case_id = str(case.get("case_id") or f"case-{index}")
        evidence = str(case.get("evidence_type") or "UNKNOWN").upper()
        if evidence not in ELIGIBLE_EVIDENCE:
            results.append({
                "case_id": case_id, "status": "INELIGIBLE_EVIDENCE",
                "evidence_type": evidence,
                "reason": "Public aggregate trade tape is not attributable fill ground truth.",
            })
            continue
        eligible_count += 1
        request = case.get("request") or {}
        side = str(request.get("side") or "").upper()
        common = {
            "fee_rate": request.get("fee_rate"),
            "fee_precision_micros": int(request.get("fee_precision_micros") or 10),
        }
        if side == "BUY":
            predicted = simulate_cash_buy(
                case.get("book") or {}, request.get("cash_usd"),
                request.get("order_type", "FAK"),
                cash_budget_mode=request.get("cash_budget_mode", "ORDER_NOTIONAL"),
                **common,
            )
        elif side == "SELL":
            predicted = simulate_share_sell(
                case.get("book") or {}, request.get("shares"),
                request.get("order_type", "FAK"), **common,
            )
        else:
            results.append({"case_id": case_id, "status": "INVALID_REQUEST_SIDE"})
            continue
        observed = case.get("observed") or {}
        errors = {
            metric: _relative_error(predicted.get(metric), observed.get(metric))
            for metric in METRICS
        }
        measured = [value for value in errors.values() if value is not None]
        maximum_case_error = max(measured) if measured else None
        results.append({
            "case_id": case_id,
            "status": (
                "PASS" if maximum_case_error is not None
                and maximum_case_error <= float(maximum_error) else "FAIL"
            ),
            "evidence_type": evidence,
            "maximum_relative_error": maximum_case_error,
            "metric_relative_errors": errors,
            "predicted": predicted,
            "observed": observed,
        })
    passed = eligible_count > 0 and all(
        item["status"] == "PASS" for item in results
        if item.get("evidence_type") in ELIGIBLE_EVIDENCE
    )
    return {
        "status": "PASS" if passed else "FAIL_CLOSED",
        "maximum_allowed_relative_error": float(maximum_error),
        "eligible_case_count": eligible_count,
        "case_count": len(results),
        "cases": results,
        "readiness_guard": (
            "Historical aggregate tape may be analyzed separately but cannot authorize live mode."
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases_jsonl")
    parser.add_argument("--maximum-error", type=float, default=0.05)
    parser.add_argument("--output", default="research/output/matching-validation.json")
    args = parser.parse_args(argv)
    cases = []
    with Path(args.cases_jsonl).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                cases.append(json.loads(line))
    report = validate_cases(cases, args.maximum_error)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"output": str(output), **{
        key: report[key] for key in ("status", "eligible_case_count", "case_count")
    }}, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
