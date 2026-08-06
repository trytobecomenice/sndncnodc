#!/usr/bin/env python3
"""Classify high-confidence historical Paper look-ahead artifacts.

Dry-run is the default. ``--apply`` only marks rows; it never deletes or
rewrites PnL.  The TypeScript/Drizzle migration must be applied first.
"""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

import config


CLASSIFIER_VERSION = "paper-ledger-integrity-v1"
DATE_PATTERN = re.compile(
    r"20[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
)


def _dated_event_cutoff(slug):
    match = DATE_PATTERN.search(slug or "")
    if not match:
        return None
    try:
        day = datetime.strptime(match.group(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    # A full UTC day after the encoded date is deliberately conservative:
    # it avoids treating a same-day evening match as already completed.
    return int(day.timestamp()) + 86_400


def classify_rows(rows):
    """Return ``{paper_trade_id: [reason, ...]}`` for confirmed candidates.

    Rule 1 is causal and strongest: opening a new Paper position after this
    same local ledger has already closed the market as resolved.
    Rule 2 covers the first stale replay for unambiguous dated slugs, where
    no earlier local resolved row exists to act as an anchor.
    """
    by_market = defaultdict(list)
    for row in rows:
        by_market[row["market_slug"]].append(row)

    classified = {}
    for market_rows in by_market.values():
        resolved_before = None
        for row in sorted(
            market_rows,
            key=lambda item: (int(item["opened_at"]), int(item["closed_at"] or 0), item["id"]),
        ):
            reasons = []
            opened_at = int(row["opened_at"])
            if resolved_before is not None and opened_at > resolved_before:
                reasons.append("reopened_after_local_resolution")

            dated_cutoff = _dated_event_cutoff(row["market_slug"])
            if (
                dated_cutoff is not None
                and opened_at >= dated_cutoff
                and row["close_reason"] == "resolved"
            ):
                reasons.append("dated_slug_after_event_day")

            if reasons:
                classified[row["id"]] = sorted(set(reasons))

            if row["close_reason"] == "resolved" and row["closed_at"] is not None:
                closed_at = int(row["closed_at"])
                resolved_before = (
                    closed_at if resolved_before is None else min(resolved_before, closed_at)
                )
    return classified


def build_report(conn):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, market_slug, status, opened_at, closed_at, close_reason, "
        "cost_basis_usd, realized_pnl_usd FROM paper_trade "
        "WHERE strategy = 'bot_filtered' AND is_demo_data = 0"
    ).fetchall()
    classified = classify_rows(rows)
    closed_rows = [row for row in rows if row["status"] == "closed"]
    bad_rows = [row for row in closed_rows if row["id"] in classified]
    clean_rows = [row for row in closed_rows if row["id"] not in classified]
    bad_open_rows = [
        row for row in rows if row["status"] == "open" and row["id"] in classified
    ]

    def aggregate(items):
        cost = sum(float(row["cost_basis_usd"] or 0) for row in items)
        pnl = sum(float(row["realized_pnl_usd"] or 0) for row in items)
        return {
            "count": len(items),
            "cost_basis_usd": round(cost, 6),
            "realized_pnl_usd": round(pnl, 6),
            "roi": (pnl / cost if cost else None),
            "win_rate": (
                sum(float(row["realized_pnl_usd"] or 0) > 0 for row in items) / len(items)
                if items else None
            ),
        }

    manifest_items = [
        {"id": row_id, "reasons": classified[row_id]}
        for row_id in sorted(classified)
    ]
    manifest_json = json.dumps(manifest_items, sort_keys=True, separators=(",", ":"))
    return {
        "classifier_version": CLASSIFIER_VERSION,
        "generated_at": int(time.time()),
        "candidate_manifest_sha256": hashlib.sha256(manifest_json.encode()).hexdigest(),
        "raw": aggregate(closed_rows),
        "confirmed_phantom_candidates": aggregate(bad_rows),
        "confirmed_open_phantom_candidates": {
            "count": len(bad_open_rows),
            "cost_basis_usd": round(sum(
                float(row["cost_basis_usd"] or 0) for row in bad_open_rows
            ), 6),
        },
        "candidate_clean_remainder": aggregate(clean_rows),
        "reason_counts": dict(sorted(
            (reason, sum(reason in reasons for reasons in classified.values()))
            for reason in {reason for reasons in classified.values() for reason in reasons}
        )),
        "candidate_ids": manifest_items,
    }


def _require_integrity_schema(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_trade)")}
    required = {
        "is_phantom", "phantom_reason", "phantom_classifier_version",
        "phantom_classified_at",
    }
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(
            "integrity migration is not applied; missing paper_trade columns: "
            + ", ".join(missing)
        )


def apply_report(conn, report, *, reset_hwm=False):
    _require_integrity_schema(conn)
    classified_at = int(time.time())
    with conn:
        for item in report["candidate_ids"]:
            conn.execute(
                "UPDATE paper_trade SET is_phantom = 1, phantom_reason = ?, "
                "phantom_classifier_version = ?, phantom_classified_at = ? "
                "WHERE id = ? AND is_demo_data = 0",
                (
                    ",".join(item["reasons"]), CLASSIFIER_VERSION,
                    classified_at, item["id"],
                ),
            )
        if reset_hwm:
            # Deleting the stale ratchet is safer than hand-subtracting an
            # aggregate. On the next controlled Paper restart/TTP sweep,
            # evaluate_equity() seeds HWM from the freshly cleaned equity.
            conn.execute("DELETE FROM bot_risk_state WHERE key = 'equity_hwm'")
            conn.execute("DELETE FROM bot_risk_state WHERE key = 'drawdown_warning'")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.SQLITE_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reset-hwm", action="store_true")
    args = parser.parse_args()
    if args.reset_hwm and not args.apply:
        parser.error("--reset-hwm requires --apply")

    if args.apply:
        conn = sqlite3.connect(args.db, timeout=30)
    else:
        conn = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True, timeout=30)
    try:
        report = build_report(conn)
        report["mode"] = "apply" if args.apply else "dry_run"
        report["hwm_reset_requested"] = bool(args.reset_hwm)
        if args.apply:
            apply_report(conn, report, reset_hwm=args.reset_hwm)
            report["applied_at"] = int(time.time())
    finally:
        conn.close()

    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        console_report = {key: value for key, value in report.items() if key != "candidate_ids"}
        console_report["candidate_id_count"] = len(report["candidate_ids"])
        print(json.dumps(console_report, indent=2, sort_keys=True))
    else:
        print(rendered)


if __name__ == "__main__":
    main()
