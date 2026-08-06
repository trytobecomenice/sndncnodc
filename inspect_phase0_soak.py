#!/usr/bin/env python3
"""Summarize a Phase-0 soak JSONL without loading it all into memory."""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def inspect_journal(path):
    path = Path(path)
    counts = Counter()
    sides = Counter()
    quality = Counter()
    poll_errors = 0
    poll_durations = []
    poll_completed = []
    visibility_lags = []
    capture_lateness_ms = {100: [], 500: []}
    delayed_available = Counter()
    delayed_total = Counter()
    realized_latest = {}
    malformed = 0
    first_timestamp = None
    last_timestamp = None

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            event_type = record.get("event_type", "unknown")
            counts[event_type] += 1
            timestamp = (
                record.get("timestamp_ms")
                or record.get("first_local_seen_timestamp_ms")
                or record.get("poll_completed_ms")
            )
            if timestamp is not None:
                timestamp = int(timestamp)
                first_timestamp = timestamp if first_timestamp is None else min(first_timestamp, timestamp)
                last_timestamp = timestamp if last_timestamp is None else max(last_timestamp, timestamp)

            if event_type == "poll_cycle":
                poll_durations.append(float(record.get("duration_ms") or 0))
                if record.get("poll_completed_ms") is not None:
                    poll_completed.append(int(record["poll_completed_ms"]))
                poll_errors += len(record.get("errors") or ())
            elif event_type == "wallet_signal":
                sides[str((record.get("signal") or {}).get("side") or "unknown").upper()] += 1
                quality.update(record.get("quality_flags") or ())
                lag = record.get("reported_visibility_lag_ms")
                if isinstance(lag, (int, float)):
                    visibility_lags.append(float(lag))
                after = ((record.get("shadow_lifecycle") or {}).get("ledger_after") or {})
                for tier, value in (after.get("realized_pnl_micros") or {}).items():
                    realized_latest[tier] = int(value)
            elif event_type == "delayed_book_observation":
                target = int(record.get("target_delay_ms") or 0)
                delayed_total[target] += 1
                if record.get("book_known_by_capture_deadline"):
                    delayed_available[target] += 1
                lateness = record.get("capture_lateness_ns")
                if target in capture_lateness_ms and isinstance(lateness, (int, float)):
                    capture_lateness_ms[target].append(float(lateness) / 1_000_000)

    poll_gaps = [later - earlier for earlier, later in zip(poll_completed, poll_completed[1:])]
    return {
        "path": str(path),
        "file_size_bytes": path.stat().st_size,
        "malformed_json_lines": malformed,
        "first_timestamp_ms": first_timestamp,
        "last_timestamp_ms": last_timestamp,
        "observed_duration_ms": (
            last_timestamp - first_timestamp
            if first_timestamp is not None and last_timestamp is not None else None
        ),
        "event_counts": dict(sorted(counts.items())),
        "signal_sides": dict(sorted(sides.items())),
        "quality_flags": dict(sorted(quality.items())),
        "poll": {
            "error_count": poll_errors,
            "duration_p50_ms": statistics.median(poll_durations) if poll_durations else None,
            "duration_p95_ms": _percentile(poll_durations, 0.95),
            "completion_gap_p95_ms": _percentile(poll_gaps, 0.95),
            "completion_gap_max_ms": max(poll_gaps) if poll_gaps else None,
        },
        "reported_visibility_lag": {
            "status": "proxy_only_source_timestamp_semantics_undocumented",
            "count": len(visibility_lags),
            "p50_ms": statistics.median(visibility_lags) if visibility_lags else None,
            "p95_ms": _percentile(visibility_lags, 0.95),
        },
        "delayed_book": {
            str(target): {
                "available": delayed_available[target],
                "total": delayed_total[target],
                "availability_ratio": (
                    delayed_available[target] / delayed_total[target]
                    if delayed_total[target] else None
                ),
                "capture_lateness_p50_ms": (
                    statistics.median(capture_lateness_ms[target])
                    if capture_lateness_ms[target] else None
                ),
                "capture_lateness_p95_ms": _percentile(capture_lateness_ms[target], 0.95),
            }
            for target in (100, 500)
        },
        "latest_realized_shadow_pnl_usd": {
            tier: value / 1_000_000 for tier, value in sorted(realized_latest.items())
        },
        "ws_reconnect_count": max(0, counts["ws_connected"] - 1),
        "ws_disconnect_count": counts["ws_disconnected"],
        "interpretation_guard": (
            "No exchange sequence exists in the public WS payload. Local generations and "
            "reported timestamps measure recorder-known state only; they do not prove a "
            "blockchain-to-book causal join."
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("journal")
    args = parser.parse_args(argv)
    print(json.dumps(inspect_journal(args.journal), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
