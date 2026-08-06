#!/usr/bin/env python3
"""One-click, fail-closed Phase-0 24-hour post-mortem generator."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from phase0_autopsy import analyze_with_polars, normalize_journals
from research.autopsy_features import (
    cluster_signals,
    open_lots_by_wallet_tier,
    read_normalized_rows,
    realized_pnl_by_wallet_tier,
)


MICROS = 1_000_000
REQUIRED_HOURS = 24


def _integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _wall_timestamp_ms(record):
    for field in (
        "timestamp_ms", "poll_completed_ms", "first_local_seen_timestamp_ms",
        "source_reported_timestamp_ms",
    ):
        value = _integer(record.get(field))
        if value is not None:
            return value
    return None


def audit_journals(journal_paths, required_hours=REQUIRED_HOURS):
    counts = Counter()
    malformed = 0
    minimum = None
    maximum = None
    for journal_path in journal_paths:
        with Path(journal_path).open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                counts[str(record.get("event_type") or "UNKNOWN")] += 1
                timestamp = _wall_timestamp_ms(record)
                if timestamp is not None:
                    minimum = timestamp if minimum is None else min(minimum, timestamp)
                    maximum = timestamp if maximum is None else max(maximum, timestamp)
    span_hours = (
        (maximum - minimum) / 3_600_000
        if minimum is not None and maximum is not None else None
    )
    return {
        "required_hours": float(required_hours),
        "observed_span_hours": span_hours,
        "window_complete": bool(span_hours is not None and span_hours >= required_hours),
        "first_timestamp_ms": minimum,
        "last_timestamp_ms": maximum,
        "malformed_json_lines": malformed,
        "event_type_counts": dict(sorted(counts.items())),
    }


def _median(values):
    values = [int(value) for value in values if value is not None]
    return statistics.median(values) if values else None


def displayed_fill_survival(rows, tiers=(3, 5), horizons_ms=(100, 500),
                            sides=("BUY", "SELL")):
    """Report displayed-book persistence; never label it live fill probability."""
    indexed = defaultdict(dict)
    for row in rows:
        tier = _integer(row.get("tier_usd"))
        delay = _integer(row.get("observation_delay_ms"))
        signal_id = str(row.get("signal_event_id") or "")
        side = str(row.get("side") or "UNKNOWN").upper()
        if signal_id and tier is not None and delay is not None:
            indexed[(signal_id, tier, side)][delay] = row
    output = []
    for tier in tiers:
        for side in sides:
            for horizon in horizons_ms:
                eligible = []
                for (signal_id, row_tier, row_side), checkpoints in indexed.items():
                    if row_tier != int(tier) or row_side != str(side).upper():
                        continue
                    t0, later = checkpoints.get(0), checkpoints.get(int(horizon))
                    if not t0 or not later or not t0.get("causal_valid"):
                        continue
                    eligible.append((signal_id, t0, later))
                causal_later = [item for item in eligible if item[2].get("causal_valid")]
                t0_full = [
                    item for item in causal_later
                    if _integer(item[1].get("fill_ratio_ppm")) == MICROS
                ]
                survived = [
                    item for item in t0_full
                    if _integer(item[2].get("fill_ratio_ppm")) == MICROS
                ]
                output.append({
                    "tier_usd": int(tier),
                    "side": str(side).upper(),
                    "horizon_ms": int(horizon),
                    "t0_eligible_signal_count": len(eligible),
                    "causal_later_signal_count": len(causal_later),
                    "t0_full_fill_signal_count": len(t0_full),
                    "displayed_full_fill_survival_count": len(survived),
                    "displayed_full_fill_survival_rate": (
                        len(survived) / len(t0_full) if t0_full else None
                    ),
                    "execution_markout_p50_micros": _median(
                        item[2].get("deterioration_from_t0_micros")
                        for item in causal_later
                    ),
                    "interpretation_guard": (
                        "Displayed causal checkpoint persistence, not live fill probability or PnL."
                    ),
                })
    return output


def tax_lot_report(journal_paths):
    summaries, details = open_lots_by_wallet_tier(journal_paths)
    by_tier = defaultdict(lambda: {
        "open_lot_count": 0, "known_cost_basis_micros": 0,
        "unknown_cost_count": 0, "ages_hours": [],
    })
    for lot in details:
        tier = int(lot["tier_usd"])
        bucket = by_tier[tier]
        bucket["open_lot_count"] += 1
        cost = _integer(lot.get("cost_basis_micros"))
        if cost is None:
            bucket["unknown_cost_count"] += 1
        else:
            bucket["known_cost_basis_micros"] += cost
        if lot.get("age_hours") is not None:
            bucket["ages_hours"].append(float(lot["age_hours"]))
    realized = realized_pnl_by_wallet_tier(journal_paths)
    output = []
    for tier in sorted(by_tier):
        bucket = by_tier[tier]
        ages = bucket.pop("ages_hours")
        output.append({
            "tier_usd": tier,
            **bucket,
            "known_cost_basis_usd": bucket["known_cost_basis_micros"] / MICROS,
            "realized_source_exit_aligned_pnl_usd": (
                sum(value for (wallet, value_tier), value in realized.items()
                    if value_tier == tier) / MICROS
            ),
            "oldest_age_hours": max(ages) if ages else None,
            "median_age_hours": statistics.median(ages) if ages else None,
            "age_buckets": {
                "lt_6h": sum(age < 6 for age in ages),
                "6h_to_24h": sum(6 <= age < 24 for age in ages),
                "24h_to_48h": sum(24 <= age < 48 for age in ages),
                "gte_48h": sum(age >= 48 for age in ages),
                "unknown": bucket["open_lot_count"] - len(ages),
            },
            "valuation_guard": "Open lots remain unrealized; no fixed-horizon forced exit.",
        })
    return {"by_tier": output, "detail_count": len(details), "wallet_tier_count": len(summaries)}


def capability_report(rows):
    observed_delays = sorted({
        _integer(row.get("observation_delay_ms")) for row in rows
        if _integer(row.get("observation_delay_ms")) is not None
    })
    return {
        "economic_utility_t_minus_1s_to_t_plus_5m": {
            "status": "UNAVAILABLE",
            "observed_checkpoint_delays_ms": observed_delays,
            "missing_required_checkpoints_ms": [
                value for value in (-1_000, 300_000) if value not in observed_delays
            ],
            "reason": (
                "A utility score requires causal T-1s/T+5m books plus an explicit "
                "utility target (source-aligned exit or resolved outcome); checkpoints alone "
                "are insufficient."
            ),
        },
        "brier_and_log_loss": {
            "status": "UNAVAILABLE",
            "reason": (
                "The Phase-0 journal has no paired forecast probability and resolved binary outcome."
            ),
            "required_fields": [
                "forecast_probability", "binary_resolution", "model_version",
                "forecast_timestamp_ms",
            ],
        },
        "mtbt": {
            "status": "UNAVAILABLE",
            "reason": (
                "Signal-triggered checkpoints do not preserve every book-message arrival."
            ),
            "available_proxy": "checkpoint generation/hash change and book age",
        },
    }


def generate_report(journal_paths, output_dir, *, required_hours=REQUIRED_HOURS,
                    allow_incomplete=False, write_plot=True):
    journal_paths = [Path(path) for path in journal_paths]
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    audit = audit_journals(journal_paths, required_hours)
    normalized_csv = output_dir / "phase0_autopsy_observations.csv"
    normalization = normalize_journals(journal_paths, normalized_csv)
    rows = list(read_normalized_rows(normalized_csv))
    assignments, clusters = cluster_signals(rows)
    unique_signals = {
        str(row.get("signal_event_id")): row for row in rows if row.get("signal_event_id")
    }
    artifacts = {"status": "NOT_REQUESTED"}
    if write_plot:
        try:
            artifacts = {
                "status": "GENERATED",
                **analyze_with_polars(normalized_csv, output_dir, write_plot=True),
            }
        except RuntimeError as exc:
            artifacts = {"status": "UNAVAILABLE_OPTIONAL_DEPENDENCY", "reason": str(exc)}
    report = {
        "schema_version": "phase0-24h-postmortem-v1",
        "status": (
            "READY_FOR_REVIEW" if audit["window_complete"]
            else "PRELIMINARY_INCOMPLETE_WINDOW" if allow_incomplete
            else "FAIL_CLOSED_INCOMPLETE_WINDOW"
        ),
        "window_audit": audit,
        "normalization": normalization,
        "cohort": {
            "signal_count": len(unique_signals),
            "wallet_count": len({row.get("wallet") for row in unique_signals.values()}),
            "market_count": len({row.get("market_slug") for row in unique_signals.values()}),
            "buy_signal_count": sum(row.get("side") == "BUY" for row in unique_signals.values()),
            "sell_signal_count": sum(row.get("side") == "SELL" for row in unique_signals.values()),
            "independent_cluster_count": len(clusters),
            "multi_wallet_cluster_count": sum(item["wallet_count"] > 1 for item in clusters),
            "cluster_assignments_count": len(assignments),
        },
        "s3_s5_displayed_fill_survival_and_markout": displayed_fill_survival(rows),
        "tax_lots": tax_lot_report(journal_paths),
        "capabilities": capability_report(rows),
        "analysis_artifacts": artifacts,
        "decision_guard": (
            "This report cannot authorize live trading. Markout diagnoses execution; only "
            "source-exit-aligned lots count as realized PnL."
        ),
    }
    report_path = output_dir / "phase0_24h_postmortem_report.json"
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journals", nargs="+")
    parser.add_argument("--output-dir", default="data/phase0-postmortem")
    parser.add_argument("--required-hours", type=float, default=REQUIRED_HOURS)
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args(argv)
    report = generate_report(
        args.journals, args.output_dir, required_hours=args.required_hours,
        allow_incomplete=args.allow_incomplete, write_plot=not args.no_plot,
    )
    print(json.dumps({
        "status": report["status"], "report_path": report["report_path"],
        "window_audit": report["window_audit"],
    }, indent=2, sort_keys=True))
    return 0 if report["status"] != "FAIL_CLOSED_INCOMPLETE_WINDOW" else 2


if __name__ == "__main__":
    raise SystemExit(main())
