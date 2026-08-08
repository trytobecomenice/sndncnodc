#!/usr/bin/env python3
"""Mechanical v2 protocol preflight and adjudication skeleton.

This evaluator intentionally refuses to start an epoch while the semantic
protocol is still a draft.  Its first job is to make every prerequisite
machine-checkable: durable PnL ledger, seven-day TTP/exit SLO, and seven-day
termination-cause capture with bounded UNKNOWN.  Statistical estimators and
their frozen hashes are added before the same-commit freeze; PASS can never
authorize Live.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import config
from db import (
    _REALIZED_ALLOCATION_TABLE,
    _REALIZED_ALLOCATOR_VERSION,
    _REALIZED_LEDGER_READY_KEY,
    _TERMINATION_CLASSIFIER_VERSION,
    _TTP_PRICE_QUARANTINE_KEY,
)


STATUSES = {
    "PRECONDITION_FAILED", "INSUFFICIENT_EVIDENCE", "INCONCLUSIVE",
    "REJECTED", "PASS", "INSTRUMENT_INSUFFICIENT", "SYSTEM_INTEGRITY_KILL",
}


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _risk_value(conn, key):
    row = conn.execute("SELECT value_json FROM bot_risk_state WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _continuous_coverage_ok(first_ts, last_ts, cutoff, now_ts, grace_seconds):
    return (
        first_ts is not None and last_ts is not None
        and first_ts <= cutoff + grace_seconds
        and last_ts >= now_ts - grace_seconds
    )


def evaluate_preconditions(conn, protocol, now_ts):
    q = protocol["qualification"]
    window_seconds = int(q["window_days"] * 86400)
    cutoff = now_ts - window_seconds
    grace_seconds = int(q["coverage_grace_seconds"])
    checks = {}
    reasons = []

    ready = _risk_value(conn, _REALIZED_LEDGER_READY_KEY)
    ledger_ok = bool(
        ready and ready.get("ready")
        and ready.get("allocator_version") == _REALIZED_ALLOCATOR_VERSION
    )
    checks["durable_ledger_ready"] = ledger_ok
    if not ledger_ok:
        reasons.append("durable_realized_allocation_ledger_not_ready")

    if _table_exists(conn, _REALIZED_ALLOCATION_TABLE):
        unresolved = conn.execute(
            f"SELECT COUNT(*) n FROM {_REALIZED_ALLOCATION_TABLE} "
            "WHERE allocation_status!='matched' OR paper_trade_id IS NULL"
        ).fetchone()["n"]
    else:
        unresolved = None
    checks["unresolved_allocations"] = unresolved
    if unresolved is None or unresolved > 0:
        reasons.append("realized_event_allocation_not_complete")

    quarantine = _risk_value(conn, _TTP_PRICE_QUARANTINE_KEY) or {}
    active_quarantines = sum(
        isinstance(item, dict) and item.get("status") == "QUARANTINED_UNPRICEABLE"
        for item in (quarantine.get("entries") or {}).values()
    )
    checks["active_unpriceable_quarantines"] = active_quarantines
    if active_quarantines:
        reasons.append("active_unpriceable_market_quarantine")

    rows = conn.execute(
        "SELECT timestamp,payload_json FROM bot_event_log "
        "WHERE event_type='ttp_sweep_observation' AND timestamp>=? ORDER BY timestamp",
        (cutoff,),
    ).fetchall()
    attempted = successful = executable = 0
    for row in rows:
        payload = json.loads(row["payload_json"])
        attempted += int(payload.get("attempted_positions") or 0)
        successful += int(payload.get("successful_price_reads") or 0)
        executable += int(payload.get("executable_bid_reads") or 0)
    timestamps = [row["timestamp"] for row in rows]
    max_gap_seconds = max(
        (right - left for left, right in zip(timestamps, timestamps[1:])), default=None
    )
    ttp_span_ok = _continuous_coverage_ok(
        rows[0]["timestamp"] if rows else None,
        rows[-1]["timestamp"] if rows else None,
        cutoff, now_ts, grace_seconds,
    ) and (max_gap_seconds is None or max_gap_seconds <= grace_seconds)
    classifier_heartbeat_ok = bool(rows) and all(
        json.loads(row["payload_json"]).get("termination_classifier_version")
        == _TERMINATION_CLASSIFIER_VERSION for row in rows
    )
    ttp_schema_ok = bool(rows) and all(
        json.loads(row["payload_json"]).get("qualification_schema_version")
        == q["ttp_observation_schema_version"] for row in rows
    )
    price_rate = successful / attempted if attempted else None
    bid_rate = executable / attempted if attempted else None
    checks["ttp"] = {"attempted": attempted, "successful": successful,
                     "executable_bid": executable, "price_rate": price_rate,
                     "executable_bid_rate": bid_rate, "full_window": ttp_span_ok,
                     "max_gap_seconds": max_gap_seconds,
                     "schema_version_ok": ttp_schema_ok,
                     "termination_classifier_heartbeat": classifier_heartbeat_ok}
    if not ttp_span_ok:
        reasons.append("ttp_qualification_window_incomplete")
    if price_rate is None or price_rate < q["ttp_price_read_success_rate_min"]:
        reasons.append("ttp_price_read_slo_failed")
    if bid_rate is None or bid_rate < q["ttp_executable_bid_rate_min"]:
        reasons.append("ttp_executable_bid_slo_failed")
    if not classifier_heartbeat_ok:
        reasons.append("termination_classifier_qualification_window_incomplete")
    if not ttp_schema_ok:
        reasons.append("ttp_observation_schema_mismatch")

    if _table_exists(conn, _REALIZED_ALLOCATION_TABLE):
        term = conn.execute(
            f"SELECT MIN(event_timestamp) first_ts,MAX(event_timestamp) last_ts,COUNT(*) total,"
            "SUM(CASE WHEN termination_cause='UNKNOWN' THEN 1 ELSE 0 END) unknown "
            f"FROM {_REALIZED_ALLOCATION_TABLE} a JOIN paper_trade p ON p.id=a.paper_trade_id "
            "WHERE a.allocation_source='live' AND a.strategy='bot_filtered' "
            "AND a.allocation_status='matched' AND a.termination_classifier_version=? "
            "AND p.status='closed' AND a.event_timestamp=p.closed_at "
            "AND a.event_timestamp>=?",
            (_TERMINATION_CLASSIFIER_VERSION, cutoff),
        ).fetchone()
        total = int(term["total"] or 0)
        unknown = int(term["unknown"] or 0)
    else:
        total = unknown = 0
    unknown_rate = unknown / total if total else None
    checks["termination_capture"] = {
        "total": total, "unknown": unknown, "unknown_rate": unknown_rate,
        "classifier_full_window": ttp_span_ok and classifier_heartbeat_ok,
    }
    if unknown_rate is None or unknown_rate > q["termination_unknown_rate_max"]:
        reasons.append("termination_unknown_rate_exceeded")

    if protocol.get("freeze_state") != "FROZEN":
        reasons.append("protocol_not_frozen")
    return checks, sorted(set(reasons))


def adjudicate(*, system_integrity_failure=False, sample_ready=False,
               max_duration_reached=False, primary_kill=False,
               lower_confidence_bound=None, stress_veto=False):
    """Pure terminal-state precedence, frozen before any v2 epoch starts."""
    if system_integrity_failure:
        return "SYSTEM_INTEGRITY_KILL"
    if not sample_ready:
        return "INSTRUMENT_INSUFFICIENT" if max_duration_reached else "INSUFFICIENT_EVIDENCE"
    if primary_kill:
        return "REJECTED"
    if lower_confidence_bound is not None and lower_confidence_bound > 0 and not stress_veto:
        return "PASS"
    return "INCONCLUSIVE"


def evaluate(db_path, protocol_path, now_ts=None):
    protocol = json.loads(Path(protocol_path).read_text())
    now_ts = int(now_ts or datetime.now(timezone.utc).timestamp())
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        checks, reasons = evaluate_preconditions(conn, protocol, now_ts)
    finally:
        conn.close()
    return {
        "protocol_version": protocol["protocol_version"],
        "evaluated_at": now_ts,
        "status": "PRECONDITION_FAILED" if reasons else "INSUFFICIENT_EVIDENCE",
        "reasons": reasons,
        "checks": checks,
        "live_authorized": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.SQLITE_PATH)
    parser.add_argument("--protocol", default="research/protocol_v2_draft.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(args.db, args.protocol)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
