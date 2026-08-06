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
import json
from pathlib import Path
import sqlite3
import time

import config
from db import _REALIZED_PNL_EVENT_TYPES


REPORT_VERSION = "paper-event-reconciliation-v1"


def allocate_events(trades, events):
    by_key = defaultdict(list)
    for trade in trades:
        key = (trade["wallet_address"], trade["market_slug"], trade["outcome"])
        by_key[key].append(trade)

    allocations = defaultdict(list)
    unmatched = []
    ambiguous = []
    for event in events:
        key = (event["trader_address"], event["market_slug"], event["outcome"])
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
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        trades = [dict(row) for row in conn.execute(
            "SELECT id,lower(wallet_address) wallet_address,market_slug,outcome,"
            "status,opened_at,closed_at,close_reason,cost_basis_usd,realized_pnl_usd,"
            "COALESCE(is_phantom,0) is_phantom,phantom_classifier_version "
            "FROM paper_trade WHERE strategy='bot_filtered' "
            "AND is_demo_data=0 ORDER BY opened_at,id"
        )]
        lower = min(row["opened_at"] for row in trades)
        upper = conn.execute("SELECT COALESCE(MAX(timestamp),?) FROM bot_event_log", (lower,)).fetchone()[0]
        for trade in trades:
            trade["allocation_end_at"] = trade["closed_at"] or upper
        placeholders = ",".join("?" for _ in _REALIZED_PNL_EVENT_TYPES)
        events = [dict(row) for row in conn.execute(
            f"SELECT id,timestamp,event_type,lower(trader_address) trader_address,"
            f"market_slug,outcome,json_extract(payload_json,'$.pnl_usd') pnl_usd "
            f"FROM bot_event_log WHERE timestamp BETWEEN ? AND ? "
            f"AND event_type IN ({placeholders}) ORDER BY timestamp,id",
            (lower, upper, *_REALIZED_PNL_EVENT_TYPES),
        )]
    finally:
        conn.close()

    allocations, unmatched, ambiguous = allocate_events(trades, events)
    rows = []
    for trade in trades:
        assigned = allocations.get(trade["id"], [])
        event_pnl = sum(float(event["pnl_usd"] or 0) for event in assigned)
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
    return {
        "report_version": REPORT_VERSION,
        "generated_at": int(time.time()),
        "trade_count": len(trades),
        "closed_trade_count": sum(row["status"] == "closed" for row in trades),
        "open_trade_count": sum(row["status"] == "open" for row in trades),
        "event_count_in_trade_time_range": len(events),
        "matched_trade_count": len(matched),
        "no_event_match_trade_count": len(rows) - len(matched),
        "unmatched_event_count": len(unmatched),
        "ambiguous_event_count": len(ambiguous),
        "row_ledger_realized_pnl_usd": sum(float(row["realized_pnl_usd"] or 0) for row in trades),
        "allocated_event_realized_pnl_usd": sum(row["allocated_event_pnl_usd"] for row in matched),
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
        "rows": rows,
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
                      if key not in {"rows", "unmatched_event_ids", "ambiguous_events"}},
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
