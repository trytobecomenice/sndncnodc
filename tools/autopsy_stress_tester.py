#!/usr/bin/env python3
"""Run hostile Phase-0 data through the real normalizer and research features."""

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from phase0_autopsy import normalize_journals  # noqa: E402
from research.autopsy_features import cluster_signals, read_normalized_rows  # noqa: E402
from tools.mock_data_generator import write_hostile_journal  # noqa: E402


def run_stress_test(output_dir, cluster_window_ms=300_000):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    journal = output_dir / "phase0-hostile.jsonl"
    normalized = output_dir / "phase0-hostile-normalized.csv"
    write_hostile_journal(journal)
    audit = normalize_journals([journal], normalized)
    rows = list(read_normalized_rows(normalized))
    assignments, clusters = cluster_signals(rows, window_ms=cluster_window_ms)

    flash = next(
        row for row in rows
        if row["signal_event_id"] == "stress-flash-sell"
        and row["observation_delay_ms"] == 100
    )
    cluster_candidates = [
        cluster for cluster in clusters if cluster["market_slug"] == "cluster-market"
    ]
    ghost = next(
        row for row in rows
        if row["signal_event_id"] == "stress-ghost"
        and row["observation_delay_ms"] == 100
    )
    checks = {
        "flash_sell_side_adjusted_deterioration_is_245000_micros": (
            flash["deterioration_from_t0_micros"] == 245_000
        ),
        "five_collinear_wallets_are_one_cluster": (
            len(cluster_candidates) == 1
            and cluster_candidates[0]["signal_count"] == 5
            and cluster_candidates[0]["wallet_count"] == 5
            and len({assignments[f"stress-cluster-{index}"] for index in range(5)}) == 1
        ),
        "ghost_book_is_censored_and_unpriced": (
            ghost["causal_valid"] is False
            and ghost["executable_price_micros"] is None
        ),
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "normalization_audit": audit,
        "cluster_count": len(clusters),
        "output_dir": str(output_dir),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="research/output/autopsy-stress")
    parser.add_argument("--cluster-window-ms", type=int, default=300_000)
    args = parser.parse_args(argv)
    result = run_stress_test(args.output_dir, args.cluster_window_ms)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
