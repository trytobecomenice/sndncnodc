#!/usr/bin/env python3
"""Factual resolution-time audit for candidate-clean Paper rows.

This is read-only.  It never upgrades a heuristic to ``is_phantom``.  A row
is factual phantom only when Gamma's market ``closedTime`` (or ``umaEndDate``
fallback) is strictly earlier than our ``opened_at``. Missing or malformed
metadata remains UNKNOWN, never zero and never clean by assumption.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

import config
from polymarket_simulator import fetch_market_metadata


AUDIT_VERSION = "resolution-timing-v1"


def parse_timestamp(value):
    if not value or not isinstance(value, str):
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def classify_resolution_timing(opened_at, metadata):
    if not metadata:
        return "unknown", None, "missing_metadata"
    if metadata.get("closed") is False:
        return "legit", None, "market_still_unresolved_at_audit"
    resolution_raw = metadata.get("closedTime") or metadata.get("umaEndDate")
    resolution_ts = parse_timestamp(resolution_raw)
    if resolution_ts is None:
        return "unknown", None, "closed_without_parseable_resolution_timestamp"
    if resolution_ts < int(opened_at):
        return "phantom", resolution_ts, "resolution_precedes_paper_entry"
    return "legit", resolution_ts, "paper_entry_precedes_resolution"


def audit_database(db_path, fetcher=fetch_market_metadata):
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id,wallet_address,market_slug,outcome,opened_at,closed_at,close_reason,"
            "cost_basis_usd,realized_pnl_usd FROM paper_trade "
            "WHERE strategy='bot_filtered' AND status='closed' AND is_demo_data=0 "
            "AND COALESCE(is_phantom,0)=0 ORDER BY market_slug,opened_at,id"
        ).fetchall()
    finally:
        conn.close()

    cache = {}
    errors = {}
    for slug in sorted({row["market_slug"] for row in rows}):
        try:
            cache[slug] = fetcher(slug)
        except Exception as exc:  # unknown is explicit evidence, not success
            cache[slug] = None
            errors[slug] = f"{type(exc).__name__}: {exc}"

    audited = []
    for row in rows:
        verdict, resolution_ts, reason = classify_resolution_timing(
            row["opened_at"], cache[row["market_slug"]]
        )
        audited.append({
            **dict(row), "verdict": verdict, "verdict_reason": reason,
            "resolution_timestamp": resolution_ts,
            "resolution_timestamp_iso": (
                datetime.fromtimestamp(resolution_ts, timezone.utc).isoformat()
                if resolution_ts is not None else None
            ),
        })

    counts = {key: sum(row["verdict"] == key for row in audited)
              for key in ("phantom", "legit", "unknown")}
    pnl = {key: round(sum(float(row["realized_pnl_usd"] or 0) for row in audited
                          if row["verdict"] == key), 6)
           for key in counts}
    return {
        "audit_version": AUDIT_VERSION,
        "generated_at": int(time.time()),
        "row_count": len(audited),
        "unique_market_count": len(cache),
        "verdict_counts": counts,
        "verdict_realized_pnl_usd": pnl,
        "fetch_errors": errors,
        "rows": audited,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.SQLITE_PATH)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_database(args.db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"},
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
