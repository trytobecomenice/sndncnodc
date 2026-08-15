#!/usr/bin/env python3
"""Promote an audited reconciliation report into the durable PnL ledger.

The default is read-only validation.  Applying requires the exact SHA-256 of
the report bytes and refuses any unmatched/ambiguous event.  The allocation
rows, per-lot cumulative values, completeness verification, and readiness key
are committed atomically; no historical event, row-level PnL, or phantom flag
is deleted or rewritten.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import config
from db import (
    _REALIZED_ALLOCATION_TABLE,
    _REALIZED_ALLOCATOR_VERSION,
    _REALIZED_LEDGER_READY_KEY,
    _REALIZED_PNL_EVENT_TYPES,
    _TERMINATION_CLASSIFIER_VERSION,
)
from reconcile_paper_trade_events import REPORT_VERSION
from ledger_integrity import audit_ledger


def report_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_report(report):
    if report.get("report_version") != REPORT_VERSION:
        raise ValueError(f"expected {REPORT_VERSION}, got {report.get('report_version')!r}")
    if report.get("unmatched_event_count") != 0 or report.get("ambiguous_event_count") != 0:
        raise ValueError("report has unmatched or ambiguous realized events")
    allocations = report.get("event_allocations")
    if not isinstance(allocations, list):
        raise ValueError("report has no event_allocations list")
    event_ids = [row.get("event_id") for row in allocations]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("report contains duplicate event allocations")
    if len(allocations) != report.get("event_count_in_trade_time_range"):
        raise ValueError("allocation count does not equal audited event count")
    sequences = [row.get("event_sequence") for row in allocations]
    if any(not isinstance(value, int) or value <= 0 for value in sequences):
        raise ValueError("report has invalid durable event sequence")
    if len(sequences) != len(set(sequences)):
        raise ValueError("report contains duplicate durable event sequences")
    snapshot = report.get("source_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("sequence_coherent") is not True:
        raise ValueError("report lacks a coherent source snapshot")
    if snapshot.get("realized_event_count") != len(allocations):
        raise ValueError("source snapshot realized-event count disagrees with report")
    for digest_key in ("trade_evidence_sha256", "realized_event_evidence_sha256"):
        digest = snapshot.get(digest_key)
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"report lacks {digest_key}")
    quantity = report.get("quantity_conservation")
    if not isinstance(quantity, dict) or quantity.get("mismatch_trade_count") != 0:
        raise ValueError("report contains a proven quantity mismatch")
    statuses = [row.get("allocation_status", "matched") for row in allocations]
    if any(value not in {"matched", "historical_unreconstructable"} for value in statuses):
        raise ValueError("report contains an unsupported allocation status")
    historical_count = sum(value == "historical_unreconstructable" for value in statuses)
    if historical_count != report.get("historical_unreconstructable_event_count"):
        raise ValueError("historical-unreconstructable coverage disagrees with allocations")
    if quantity.get("unknown_trade_count") != report.get(
            "historical_unreconstructable_trade_count"):
        raise ValueError("historical-unreconstructable lot coverage disagrees with quantity audit")
    return allocations


def apply_report(db_path, report, sha256):
    allocations = validate_report(report)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (_REALIZED_ALLOCATION_TABLE,),
        ).fetchone() is None:
            raise RuntimeError("allocation migration has not been applied")

        placeholders = ",".join("?" for _ in _REALIZED_PNL_EVENT_TYPES)
        source_row = conn.execute(
            f"SELECT COUNT(*) n,MAX(event_sequence) max_sequence,MAX(timestamp) max_timestamp "
            f"FROM bot_event_log WHERE event_type IN ({placeholders})",
            _REALIZED_PNL_EVENT_TYPES,
        ).fetchone()
        snapshot = report["source_snapshot"]
        observed_snapshot = (
            int(source_row["n"]), int(source_row["max_sequence"] or 0),
            int(source_row["max_timestamp"] or 0),
        )
        expected_snapshot = (
            int(snapshot["realized_event_count"]),
            int(snapshot["realized_event_max_sequence"] or 0),
            int(snapshot["realized_event_max_timestamp"] or 0),
        )
        if observed_snapshot != expected_snapshot:
            raise RuntimeError(
                f"source DB changed after report: expected={expected_snapshot}, "
                f"observed={observed_snapshot}"
            )

        now = int(time.time())
        for row in allocations:
            allocation_status = row.get("allocation_status", "matched")
            allocation_source = (
                "historical_backfill" if allocation_status == "matched"
                else "historical_unreconstructable"
            )
            conn.execute(
                f"INSERT INTO {_REALIZED_ALLOCATION_TABLE} "
                "(event_id,paper_trade_id,event_timestamp,event_type,strategy,pnl_usd,"
                "event_sequence,"
                "cost_basis_usd,allocation_status,candidate_count,allocator_version,"
                "allocation_source,termination_cause,source_shares_at_termination,"
                "shares_closed,shares_remaining,termination_classifier_version,allocated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(event_id) DO UPDATE SET "
                "paper_trade_id=excluded.paper_trade_id,event_timestamp=excluded.event_timestamp,"
                "event_sequence=excluded.event_sequence,"
                "event_type=excluded.event_type,strategy=excluded.strategy,pnl_usd=excluded.pnl_usd,"
                "cost_basis_usd=excluded.cost_basis_usd,allocation_status=excluded.allocation_status,"
                "candidate_count=excluded.candidate_count,allocator_version=excluded.allocator_version,"
                "allocation_source=excluded.allocation_source,termination_cause=excluded.termination_cause,"
                "source_shares_at_termination=excluded.source_shares_at_termination,"
                "shares_closed=excluded.shares_closed,shares_remaining=excluded.shares_remaining,"
                "termination_classifier_version=excluded.termination_classifier_version,"
                "allocated_at=excluded.allocated_at",
                (row["event_id"], row["paper_trade_id"], int(row["event_timestamp"]),
                 row["event_type"], row.get("strategy", "bot_filtered"), float(row["pnl_usd"]),
                 int(row["event_sequence"]),
                 row.get("cost_basis_usd"), allocation_status, 1,
                 _REALIZED_ALLOCATOR_VERSION, allocation_source,
                 row.get("termination_cause", "UNKNOWN"),
                 row.get("source_shares_at_termination"), row.get("shares_closed"),
                 row.get("shares_remaining"), _TERMINATION_CLASSIFIER_VERSION, now),
            )

        conn.execute(
            "UPDATE paper_trade SET cumulative_realized_pnl_usd=COALESCE(("
            f"SELECT SUM(a.pnl_usd) FROM {_REALIZED_ALLOCATION_TABLE} a "
            "WHERE a.paper_trade_id=paper_trade.id AND a.allocation_status='matched'"
            "),0), cumulative_realized_cost_basis_usd=COALESCE(("
            f"SELECT SUM(a.cost_basis_usd) FROM {_REALIZED_ALLOCATION_TABLE} a "
            "WHERE a.paper_trade_id=paper_trade.id AND a.allocation_status='matched'"
            "),0), realized_event_count=(SELECT COUNT(*) FROM "
            f"{_REALIZED_ALLOCATION_TABLE} a WHERE a.paper_trade_id=paper_trade.id "
            "AND a.allocation_status='matched')"
        )
        conn.execute(
            "UPDATE paper_trade SET total_acquired_shares=(SELECT "
            "CASE WHEN COUNT(*)=0 THEN NULL "
            "WHEN SUM(CASE WHEN a.shares_closed IS NULL OR a.shares_remaining IS NULL THEN 1 ELSE 0 END)>0 "
            "THEN NULL ELSE SUM(a.shares_closed)+COALESCE((SELECT a2.shares_remaining FROM "
            f"{_REALIZED_ALLOCATION_TABLE} a2 WHERE a2.paper_trade_id=paper_trade.id "
            "AND a2.allocation_status='matched' ORDER BY a2.event_timestamp DESC,"
            "a2.event_sequence DESC LIMIT 1),0) END "
            f"FROM {_REALIZED_ALLOCATION_TABLE} a WHERE a.paper_trade_id=paper_trade.id "
            "AND a.allocation_status='matched') WHERE total_acquired_shares IS NULL"
        )

        unresolved = conn.execute(
            f"SELECT COUNT(*) n FROM {_REALIZED_ALLOCATION_TABLE} "
            "WHERE allocation_status NOT IN ('matched','historical_unreconstructable') "
            "OR paper_trade_id IS NULL"
        ).fetchone()["n"]
        missing = conn.execute(
            f"SELECT COUNT(*) n FROM bot_event_log e WHERE e.event_type IN ({placeholders}) "
            f"AND NOT EXISTS (SELECT 1 FROM {_REALIZED_ALLOCATION_TABLE} a WHERE a.event_id=e.id)",
            _REALIZED_PNL_EVENT_TYPES,
        ).fetchone()["n"]
        if unresolved or missing:
            raise RuntimeError(f"ledger incomplete: unresolved={unresolved}, missing={missing}")

        # Promotion is the trust boundary, not a promise to audit later.  Run
        # every available E/A/L, event-economics, seal and quantity invariant
        # against the uncommitted transaction and refuse readiness on any
        # failure. UNKNOWN quantity evidence remains explicit in warnings and
        # cannot be mistaken for PASS by protocol preflight.
        integrity = audit_ledger(conn, _REALIZED_PNL_EVENT_TYPES)
        if integrity.get("failures"):
            raise RuntimeError(
                "ledger integrity failed before readiness: "
                + ",".join(integrity["failures"])
            )

        ready_value = json.dumps({
            "ready": True,
            "allocator_version": _REALIZED_ALLOCATOR_VERSION,
            "report_version": report["report_version"],
            "report_sha256": sha256,
            "event_count": len(allocations),
            "integrity_status": integrity.get("status"),
            "integrity_warnings": integrity.get("warnings", []),
            "promoted_at": now,
        }, sort_keys=True)
        conn.execute(
            "INSERT INTO bot_risk_state(key,value_json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (_REALIZED_LEDGER_READY_KEY, ready_value, now),
        )
        conn.commit()
        return {
            "event_count": len(allocations), "unresolved": unresolved,
            "missing": missing, "integrity": integrity,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.SQLITE_PATH)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    sha256 = report_sha256(args.report)
    report = json.loads(args.report.read_text())
    allocations = validate_report(report)
    summary = {"report_sha256": sha256, "event_count": len(allocations), "valid": True}
    if args.apply:
        if not args.expected_sha256 or args.expected_sha256 != sha256:
            raise SystemExit("--apply requires the exact --expected-sha256")
        summary.update(apply_report(args.db, report, sha256))
        summary["applied"] = True
    else:
        summary["applied"] = False
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
