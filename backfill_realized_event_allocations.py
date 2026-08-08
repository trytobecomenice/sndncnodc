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

        now = int(time.time())
        for row in allocations:
            conn.execute(
                f"INSERT INTO {_REALIZED_ALLOCATION_TABLE} "
                "(event_id,paper_trade_id,event_timestamp,event_type,strategy,pnl_usd,"
                "cost_basis_usd,allocation_status,candidate_count,allocator_version,"
                "allocation_source,termination_cause,source_shares_at_termination,"
                "termination_classifier_version,allocated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(event_id) DO UPDATE SET "
                "paper_trade_id=excluded.paper_trade_id,event_timestamp=excluded.event_timestamp,"
                "event_type=excluded.event_type,strategy=excluded.strategy,pnl_usd=excluded.pnl_usd,"
                "cost_basis_usd=excluded.cost_basis_usd,allocation_status=excluded.allocation_status,"
                "candidate_count=excluded.candidate_count,allocator_version=excluded.allocator_version,"
                "allocation_source=excluded.allocation_source,termination_cause=excluded.termination_cause,"
                "source_shares_at_termination=excluded.source_shares_at_termination,"
                "termination_classifier_version=excluded.termination_classifier_version,"
                "allocated_at=excluded.allocated_at",
                (row["event_id"], row["paper_trade_id"], int(row["event_timestamp"]),
                 row["event_type"], row.get("strategy", "bot_filtered"), float(row["pnl_usd"]),
                 row.get("cost_basis_usd"), "matched", 1, _REALIZED_ALLOCATOR_VERSION,
                 "historical_backfill", row.get("termination_cause", "UNKNOWN"),
                 row.get("source_shares_at_termination"), _TERMINATION_CLASSIFIER_VERSION, now),
            )

        conn.execute(
            "UPDATE paper_trade SET cumulative_realized_pnl_usd=COALESCE(("
            f"SELECT SUM(a.pnl_usd) FROM {_REALIZED_ALLOCATION_TABLE} a "
            "WHERE a.paper_trade_id=paper_trade.id AND a.allocation_status='matched'"
            "),0), realized_event_count=(SELECT COUNT(*) FROM "
            f"{_REALIZED_ALLOCATION_TABLE} a WHERE a.paper_trade_id=paper_trade.id "
            "AND a.allocation_status='matched')"
        )

        unresolved = conn.execute(
            f"SELECT COUNT(*) n FROM {_REALIZED_ALLOCATION_TABLE} "
            "WHERE allocation_status!='matched' OR paper_trade_id IS NULL"
        ).fetchone()["n"]
        placeholders = ",".join("?" for _ in _REALIZED_PNL_EVENT_TYPES)
        missing = conn.execute(
            f"SELECT COUNT(*) n FROM bot_event_log e WHERE e.event_type IN ({placeholders}) "
            f"AND NOT EXISTS (SELECT 1 FROM {_REALIZED_ALLOCATION_TABLE} a WHERE a.event_id=e.id)",
            _REALIZED_PNL_EVENT_TYPES,
        ).fetchone()["n"]
        if unresolved or missing:
            raise RuntimeError(f"ledger incomplete: unresolved={unresolved}, missing={missing}")

        ready_value = json.dumps({
            "ready": True,
            "allocator_version": _REALIZED_ALLOCATOR_VERSION,
            "report_version": report["report_version"],
            "report_sha256": sha256,
            "event_count": len(allocations),
            "promoted_at": now,
        }, sort_keys=True)
        conn.execute(
            "INSERT INTO bot_risk_state(key,value_json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (_REALIZED_LEDGER_READY_KEY, ready_value, now),
        )
        conn.commit()
        return {"event_count": len(allocations), "unresolved": unresolved, "missing": missing}
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
