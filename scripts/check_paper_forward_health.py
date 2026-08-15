#!/usr/bin/env python3
"""Read-only health gate for the 24-hour paper forward observation."""

import argparse
import json
import os
import sqlite3
import time
from collections import Counter, deque
from pathlib import Path


GOOD_PHASE0_EVENTS = {
    "poll_cycle",
    "wallet_signal_ingress",
    "wallet_signal",
    "delayed_book_observation",
}


def _matching_pids(required_fragments):
    matches = []
    proc = Path("/proc")
    if not proc.exists():
        return matches
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except (OSError, UnicodeDecodeError):
            continue
        if all(fragment in command for fragment in required_fragments):
            matches.append(int(entry.name))
    return sorted(matches)


def _phase0_tail(path, line_count=500):
    recent = deque(maxlen=line_count)
    with path.open() as handle:
        for line in handle:
            if line.strip():
                recent.append(line)
    counts = Counter()
    malformed = 0
    for line in recent:
        try:
            counts[json.loads(line).get("event_type", "missing")] += 1
        except (TypeError, json.JSONDecodeError):
            malformed += 1
    return counts, malformed


def collect_health(db_path, phase0_path, since_epoch, now=None, grace_seconds=600):
    now = int(now or time.time())
    failures = []
    warnings = []
    bot_pids = _matching_pids(("python", "bot.py"))
    recorder_pids = _matching_pids(("python", "phase0_soak_recorder.py"))
    if len(bot_pids) != 1:
        failures.append(f"expected exactly one bot.py process, found {bot_pids}")
    if len(recorder_pids) != 1:
        failures.append(f"expected exactly one Phase-0 recorder, found {recorder_pids}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    active_tracked = conn.execute(
        "SELECT count(*) FROM wallet_profile "
        "WHERE status='track' AND circuit_breaker_muted=0"
    ).fetchone()[0]
    min_tracked = 3
    if active_tracked < min_tracked:
        failures.append(f"active tracked roster {active_tracked} is below guard {min_tracked}")

    event_count, latest_event = conn.execute(
        "SELECT count(*), max(timestamp) FROM bot_event_log WHERE timestamp >= ?",
        (since_epoch,),
    ).fetchone()
    if now - since_epoch >= grace_seconds and event_count == 0:
        failures.append("no bot_event_log evidence written since observation start")

    sizing_rows = conn.execute(
        "SELECT json_extract(score_breakdown_json, '$.sizing_tier'), count(*) "
        "FROM decision_journal WHERE created_at >= ? AND json_valid(score_breakdown_json) "
        "AND json_extract(score_breakdown_json, '$.sizing_tier') IS NOT NULL "
        "GROUP BY 1 ORDER BY 2 DESC",
        (since_epoch,),
    ).fetchall()
    wrong_unprovenanced_size = conn.execute(
        "SELECT count(*) FROM decision_journal WHERE created_at >= ? "
        "AND json_valid(score_breakdown_json) "
        "AND json_extract(score_breakdown_json, '$.sizing_tier')='unprovenanced' "
        "AND abs(json_extract(score_breakdown_json, '$.trade_size_usd') - 3.0) > 0.000001",
        (since_epoch,),
    ).fetchone()[0]
    if wrong_unprovenanced_size:
        failures.append(f"{wrong_unprovenanced_size} unprovenanced decisions were not sized at $3")

    live_event_count = conn.execute(
        "SELECT count(*) FROM bot_event_log WHERE timestamp >= ? AND event_type LIKE 'live_%'",
        (since_epoch,),
    ).fetchone()[0]
    if live_event_count:
        failures.append(f"paper observation emitted {live_event_count} live_* events")
    conn.close()

    phase0_age_seconds = None
    phase0_counts = Counter()
    phase0_malformed = 0
    if not phase0_path.exists():
        failures.append(f"Phase-0 journal is missing: {phase0_path}")
    else:
        phase0_age_seconds = max(0, now - int(phase0_path.stat().st_mtime))
        if phase0_age_seconds > 180:
            failures.append(f"Phase-0 journal is stale by {phase0_age_seconds}s")
        phase0_counts, phase0_malformed = _phase0_tail(phase0_path)
        if phase0_malformed:
            failures.append(f"Phase-0 tail contains {phase0_malformed} malformed JSON line(s)")
        if not any(phase0_counts[name] for name in GOOD_PHASE0_EVENTS):
            failures.append("last 500 Phase-0 rows contain no usable evidence events")
        if phase0_counts and sum(phase0_counts.values()) == phase0_counts["ws_disconnected"]:
            failures.append("last 500 Phase-0 rows are all ws_disconnected")

    if not sizing_rows:
        warnings.append("no post-start sizing decisions yet; recheck after signals arrive")

    return {
        "ok": not failures,
        "checked_at_epoch": now,
        "since_epoch": since_epoch,
        "processes": {"bot": bot_pids, "phase0_recorder": recorder_pids},
        "active_tracked_wallets": active_tracked,
        "minimum_tracked_wallets": min_tracked,
        "bot_events_since_start": event_count,
        "latest_bot_event_epoch": latest_event,
        "sizing_tiers_since_start": dict(sizing_rows),
        "wrong_unprovenanced_size_count": wrong_unprovenanced_size,
        "live_event_count": live_event_count,
        "phase0_journal_age_seconds": phase0_age_seconds,
        "phase0_tail_event_counts": dict(phase0_counts),
        "failures": failures,
        "warnings": warnings,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/app.db")
    parser.add_argument("--phase0-journal", default="data/phase0_soak_v1.jsonl")
    parser.add_argument("--since-epoch", type=int, required=True)
    parser.add_argument("--grace-seconds", type=int, default=600)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    result = collect_health(
        os.path.abspath(args.db),
        Path(args.phase0_journal).resolve(),
        args.since_epoch,
        grace_seconds=args.grace_seconds,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
