#!/usr/bin/env python3
"""Read-only allocation of realized event PnL to closed Paper tax lots.

`paper_trade.realized_pnl_usd` stores only the event that finally closed a
partially sold lot. Economic realized PnL lives in every immutable close event.
This tool assigns an event only when wallet/market/outcome and the lot's
[opened_at, closed_at] interval produce exactly one match. Zero or multiple
matches remain explicit evidence gaps and are never forced into a clean total.
"""

import argparse
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import config
from db import (
    _REALIZED_PNL_EVENT_TYPES,
    _realized_strategy_for_event_type,
    _termination_cause_for_event,
)


REPORT_VERSION = "paper-event-reconciliation-v4"
MICRO = Decimal("1000000")


def _micros(value):
    return int((Decimal(str(value or 0)) * MICRO).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    ))


def _sum_micros(values):
    total = sum((Decimal(str(value or 0)) for value in values), Decimal("0"))
    return int((total * MICRO).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _evidence_digest(rows, fields):
    digest = hashlib.sha256()
    for row in rows:
        payload = [row.get(field) for field in fields]
        digest.update(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def allocate_events(trades, events):
    by_key = defaultdict(list)
    for trade in trades:
        key = (trade["strategy"], trade["wallet_address"], trade["market_slug"], trade["outcome"])
        by_key[key].append(trade)

    allocations = defaultdict(list)
    unmatched = []
    ambiguous = []
    for event in events:
        key = (event["strategy"], event["trader_address"], event["market_slug"], event["outcome"])
        candidates = [trade for trade in by_key.get(key, [])
                      if trade["opened_at"] <= event["timestamp"] <= trade["allocation_end_at"]]
        if len(candidates) == 1:
            allocations[candidates[0]["id"]].append(event)
        elif not candidates:
            unmatched.append(event)
        else:
            ambiguous.append({
                "event_id": event["id"],
                "candidate_trade_ids": sorted(trade["id"] for trade in candidates),
            })
    return allocations, unmatched, ambiguous


def reconcile(db_path):
    resolved_db_path = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{resolved_db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        trades = [dict(row) for row in conn.execute(
            "SELECT id,strategy,lower(wallet_address) wallet_address,market_slug,outcome,"
            "status,opened_at,closed_at,close_reason,cost_basis_usd,realized_pnl_usd,"
            "our_shares,total_acquired_shares,COALESCE(is_phantom,0) is_phantom,"
            "phantom_reason,phantom_classifier_version,phantom_classified_at "
            "FROM paper_trade WHERE strategy IN ('bot_filtered','shadow_rehab','shadow_challenger') "
            "AND is_demo_data=0 ORDER BY opened_at,id"
        )]
        lower = min(row["opened_at"] for row in trades)
        upper = conn.execute("SELECT COALESCE(MAX(timestamp),?) FROM bot_event_log", (lower,)).fetchone()[0]
        for trade in trades:
            trade["allocation_end_at"] = trade["closed_at"] or upper
        placeholders = ",".join("?" for _ in _REALIZED_PNL_EVENT_TYPES)
        events = [dict(row) for row in conn.execute(
            f"SELECT event_sequence,id,timestamp,event_type,lower(trader_address) trader_address,"
            f"market_slug,outcome,json_extract(payload_json,'$.pnl_usd') pnl_usd,"
            f"json_extract(payload_json,'$.cost_basis_usd') cost_basis_usd,"
            f"json_extract(payload_json,'$.termination_cause') termination_cause,"
            f"json_extract(payload_json,'$.source_shares_at_termination') source_shares_at_termination,"
            f"json_extract(payload_json,'$.our_shares_closed') shares_closed,"
            f"json_extract(payload_json,'$.our_shares_remaining') shares_remaining "
            f"FROM bot_event_log WHERE timestamp BETWEEN ? AND ? "
            f"AND event_type IN ({placeholders}) ORDER BY timestamp,event_sequence",
            (lower, upper, *_REALIZED_PNL_EVENT_TYPES),
        )]
        for event in events:
            event["strategy"] = _realized_strategy_for_event_type(event["event_type"])
        event_count, max_event_sequence, max_event_timestamp = conn.execute(
            f"SELECT COUNT(*),MAX(event_sequence),MAX(timestamp) FROM bot_event_log "
            f"WHERE event_type IN ({placeholders})", _REALIZED_PNL_EVENT_TYPES,
        ).fetchone()
        total_event_count, source_max_sequence = conn.execute(
            "SELECT COUNT(*),MAX(event_sequence) FROM bot_event_log"
        ).fetchone()
        counter_row = conn.execute(
            "SELECT next_value FROM bot_event_sequence_counter WHERE singleton=1"
        ).fetchone()
        source_snapshot = {
            "database_path": str(resolved_db_path),
            "database_size_bytes": os.stat(resolved_db_path).st_size,
            "database_mtime_ns": os.stat(resolved_db_path).st_mtime_ns,
            "schema_version": conn.execute("PRAGMA schema_version").fetchone()[0],
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "page_count": conn.execute("PRAGMA page_count").fetchone()[0],
            "page_size": conn.execute("PRAGMA page_size").fetchone()[0],
            "total_event_count": total_event_count,
            "source_max_event_sequence": source_max_sequence,
            "sequence_counter_next_value": counter_row[0] if counter_row else None,
            "realized_event_count": event_count,
            "realized_event_max_sequence": max_event_sequence,
            "realized_event_max_timestamp": max_event_timestamp,
            "sequence_coherent": bool(
                source_max_sequence and counter_row and counter_row[0] > source_max_sequence
            ),
        }
    finally:
        conn.close()

    allocations, unmatched, ambiguous = allocate_events(trades, events)
    event_allocations = []
    rows = []
    for trade in trades:
        assigned = allocations.get(trade["id"], [])
        event_pnl = sum(float(event["pnl_usd"] or 0) for event in assigned)
        event_allocations.extend({
            "event_id": event["id"],
            "paper_trade_id": trade["id"],
            "event_timestamp": event["timestamp"],
            "event_sequence": event["event_sequence"],
            "event_type": event["event_type"],
            "strategy": trade.get("strategy", "bot_filtered"),
            "pnl_usd": float(event["pnl_usd"] or 0),
            "cost_basis_usd": (float(event["cost_basis_usd"])
                               if event.get("cost_basis_usd") is not None else None),
            "termination_cause": _termination_cause_for_event(event),
            "source_shares_at_termination": event.get("source_shares_at_termination"),
            "shares_closed": event.get("shares_closed"),
            "shares_remaining": event.get("shares_remaining"),
        } for event in assigned)
        rows.append({
            **trade,
            "allocated_event_count": len(assigned),
            "allocated_event_pnl_usd": event_pnl,
            "row_minus_event_pnl_usd": float(trade["realized_pnl_usd"] or 0) - event_pnl,
            "allocation_status": "matched" if assigned else "no_event_match",
        })

    matched = [row for row in rows if row["allocation_status"] == "matched"]
    clean = [row for row in matched if not row["is_phantom"]]
    phantom = [row for row in matched if row["is_phantom"]]
    strategy_event_breakdown = {}
    event_type_breakdown = {}
    for event in events:
        strategy = event["strategy"]
        strategy_event_breakdown[strategy] = strategy_event_breakdown.get(strategy, 0) + 1
        event_type = event["event_type"]
        event_type_breakdown[event_type] = event_type_breakdown.get(event_type, 0) + 1

    trades_by_id = {row["id"]: row for row in trades}
    cohort_breakdown = defaultdict(lambda: {
        "event_count": 0, "pnl_usd": 0.0, "cost_basis_usd": 0.0,
    })
    allocations_by_trade = defaultdict(list)
    for allocation in event_allocations:
        trade = trades_by_id[allocation["paper_trade_id"]]
        factual_status = "phantom" if trade["is_phantom"] else "fact_clean"
        key = "|".join((
            allocation["strategy"], factual_status, trade["status"],
            allocation["termination_cause"],
        ))
        cohort_breakdown[key]["event_count"] += 1
        cohort_breakdown[key]["pnl_usd"] += allocation["pnl_usd"]
        cohort_breakdown[key]["cost_basis_usd"] += float(
            allocation.get("cost_basis_usd") or 0
        )
        allocations_by_trade[allocation["paper_trade_id"]].append(allocation)

    quantity_mismatch_trade_ids = []
    quantity_unknown_trade_ids = []
    historical_reconstructable_trade_count = 0
    existing_authority_trade_count = 0
    for trade_id, assigned in allocations_by_trade.items():
        trade = trades_by_id[trade_id]
        assigned.sort(key=lambda row: (
            row["event_timestamp"], row["event_sequence"], row["event_id"]
        ))
        if any(row.get("shares_closed") is None or row.get("shares_remaining") is None
               for row in assigned):
            quantity_unknown_trade_ids.append(trade_id)
            continue
        reconstructed = _sum_micros(
            [*(row["shares_closed"] for row in assigned),
             assigned[-1]["shares_remaining"]]
        )
        if trade.get("total_acquired_shares") is None:
            historical_reconstructable_trade_count += 1
        else:
            existing_authority_trade_count += 1
            if _micros(trade["total_acquired_shares"]) != reconstructed:
                quantity_mismatch_trade_ids.append(trade_id)
        if trade["status"] == "closed" and _micros(assigned[-1]["shares_remaining"]) != 0:
            quantity_mismatch_trade_ids.append(trade_id)

    historical_unreconstructable_trade_ids = set(quantity_unknown_trade_ids)
    historical_unreconstructable_event_count = 0
    for allocation in event_allocations:
        if allocation["paper_trade_id"] in historical_unreconstructable_trade_ids:
            allocation["allocation_status"] = "historical_unreconstructable"
            allocation["unreconstructable_reason"] = "missing_share_conservation_evidence"
            historical_unreconstructable_event_count += 1
        else:
            allocation["allocation_status"] = "matched"

    trade_digest_fields = (
        "id", "strategy", "wallet_address", "market_slug", "outcome", "status",
        "opened_at", "closed_at", "cost_basis_usd", "realized_pnl_usd",
        "is_phantom", "phantom_classifier_version", "total_acquired_shares", "our_shares",
    )
    event_digest_fields = (
        "event_sequence", "id", "timestamp", "event_type", "strategy",
        "trader_address", "market_slug", "outcome", "pnl_usd", "cost_basis_usd",
        "shares_closed", "shares_remaining",
    )
    source_snapshot["trade_evidence_sha256"] = _evidence_digest(
        sorted(trades, key=lambda row: row["id"]), trade_digest_fields
    )
    source_snapshot["realized_event_evidence_sha256"] = _evidence_digest(
        events, event_digest_fields
    )

    return {
        "report_version": REPORT_VERSION,
        "generated_at": int(time.time()),
        "trade_count": len(trades),
        "closed_trade_count": sum(row["status"] == "closed" for row in trades),
        "open_trade_count": sum(row["status"] == "open" for row in trades),
        "event_count_in_trade_time_range": len(events),
        "event_count_by_strategy": dict(sorted(strategy_event_breakdown.items())),
        "event_count_by_type": dict(sorted(event_type_breakdown.items())),
        "matched_trade_count": len(matched),
        "no_event_match_trade_count": len(rows) - len(matched),
        "unmatched_event_count": len(unmatched),
        "ambiguous_event_count": len(ambiguous),
        "row_ledger_realized_pnl_usd": sum(float(row["realized_pnl_usd"] or 0) for row in trades),
        "allocated_event_realized_pnl_usd": sum(row["allocated_event_pnl_usd"] for row in matched),
        "row_minus_allocated_event_pnl_usd": (
            sum(float(row["realized_pnl_usd"] or 0) for row in trades)
            - sum(row["allocated_event_pnl_usd"] for row in matched)
        ),
        "allocated_event_cost_basis_usd": sum(
            float(row.get("cost_basis_usd") or 0) for row in event_allocations
        ),
        "fact_clean_allocated_event_pnl_usd": sum(row["allocated_event_pnl_usd"] for row in clean),
        "fact_clean_closed_event_pnl_usd": sum(
            row["allocated_event_pnl_usd"] for row in clean if row["status"] == "closed"
        ),
        "fact_clean_open_partial_event_pnl_usd": sum(
            row["allocated_event_pnl_usd"] for row in clean if row["status"] == "open"
        ),
        "phantom_allocated_event_pnl_usd": sum(row["allocated_event_pnl_usd"] for row in phantom),
        "fact_clean_cost_basis_usd": sum(float(row["cost_basis_usd"] or 0) for row in clean),
        "fact_clean_trade_count": len(clean),
        "fact_clean_closed_trade_count": sum(row["status"] == "closed" for row in clean),
        "source_snapshot": source_snapshot,
        "cohort_breakdown": dict(sorted(cohort_breakdown.items())),
        "quantity_conservation": {
            "existing_authority_trade_count": existing_authority_trade_count,
            "historical_reconstructable_trade_count": historical_reconstructable_trade_count,
            "mismatch_trade_count": len(set(quantity_mismatch_trade_ids)),
            "mismatch_trade_ids": sorted(set(quantity_mismatch_trade_ids)),
            "unknown_trade_count": len(quantity_unknown_trade_ids),
            "unknown_trade_ids": sorted(quantity_unknown_trade_ids),
        },
        "historical_unreconstructable_event_count": (
            historical_unreconstructable_event_count + len(unmatched) + len(ambiguous)
        ),
        "historical_unreconstructable_trade_count": len(
            historical_unreconstructable_trade_ids
        ),
        "rows": rows,
        "event_allocations": sorted(
            event_allocations,
            key=lambda row: (row["event_timestamp"], row["event_sequence"], row["event_id"]),
        ),
        "unmatched_event_ids": [row["id"] for row in unmatched],
        "ambiguous_events": ambiguous,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.SQLITE_PATH)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = reconcile(args.db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: value for key, value in report.items()
                      if key not in {"rows", "event_allocations", "unmatched_event_ids",
                                     "ambiguous_events"}},
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
