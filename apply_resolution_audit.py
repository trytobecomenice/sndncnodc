#!/usr/bin/env python3
"""Apply factual resolution-time classifications from a reviewed audit.

Dry-run is the default. Applying requires the caller to repeat the exact
candidate-manifest SHA-256 printed by dry-run. Rows are marked only; no trade,
PnL, or event-log row is deleted or rewritten. HWM is deliberately untouched.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import config


SOURCE_AUDIT_VERSION = "resolution-timing-v1"
CLASSIFIER_VERSION = "paper-ledger-resolution-facts-v2"


def load_candidates(path):
    report = json.loads(Path(path).read_text())
    if report.get("audit_version") != SOURCE_AUDIT_VERSION:
        raise RuntimeError(f"unsupported audit version: {report.get('audit_version')!r}")
    candidates = sorted(
        ({
            "id": row["id"],
            "resolution_timestamp": int(row["resolution_timestamp"]),
            "reason": row["verdict_reason"],
        } for row in report.get("rows", []) if row.get("verdict") == "phantom"),
        key=lambda row: row["id"],
    )
    manifest = json.dumps(candidates, sort_keys=True, separators=(",", ":"))
    return report, candidates, hashlib.sha256(manifest.encode()).hexdigest()


def inspect_database(conn, candidates):
    ids = [row["id"] for row in candidates]
    if not ids:
        return {"found": 0, "already_flagged": 0, "missing_ids": []}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id,COALESCE(is_phantom,0) is_phantom FROM paper_trade "
        f"WHERE id IN ({placeholders})", ids,
    ).fetchall()
    found = {row[0]: int(row[1]) for row in rows}
    return {
        "found": len(found),
        "already_flagged": sum(found.values()),
        "missing_ids": sorted(set(ids) - set(found)),
    }


def apply_candidates(conn, candidates):
    classified_at = int(time.time())
    with conn:
        for row in candidates:
            conn.execute(
                "UPDATE paper_trade SET is_phantom=1,phantom_reason=?,"
                "phantom_classifier_version=?,phantom_classified_at=? "
                "WHERE id=? AND is_demo_data=0 AND COALESCE(is_phantom,0)=0",
                (
                    f"resolution_precedes_entry:{row['resolution_timestamp']}",
                    CLASSIFIER_VERSION, classified_at, row["id"],
                ),
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.SQLITE_PATH)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-manifest-sha256")
    args = parser.parse_args()

    source, candidates, digest = load_candidates(args.audit)
    if args.apply and args.expected_manifest_sha256 != digest:
        parser.error("--apply requires the exact dry-run --expected-manifest-sha256")

    uri = f"file:{Path(args.db).resolve()}?mode=ro"
    conn = sqlite3.connect(args.db if args.apply else uri, uri=not args.apply, timeout=30)
    try:
        before = inspect_database(conn, candidates)
        if before["missing_ids"]:
            raise RuntimeError(f"audit rows missing from DB: {before['missing_ids']}")
        if args.apply:
            apply_candidates(conn, candidates)
        after = inspect_database(conn, candidates)
    finally:
        conn.close()

    report = {
        "mode": "apply" if args.apply else "dry_run",
        "classifier_version": CLASSIFIER_VERSION,
        "source_audit_version": source.get("audit_version"),
        "source_generated_at": source.get("generated_at"),
        "source_unknown_count": source.get("verdict_counts", {}).get("unknown"),
        "candidate_count": len(candidates),
        "candidate_realized_pnl_usd": round(sum(
            float(row.get("realized_pnl_usd") or 0)
            for row in source.get("rows", []) if row.get("verdict") == "phantom"
        ), 6),
        "candidate_manifest_sha256": digest,
        "database_before": before,
        "database_after": after,
        "hwm_changed": False,
        "raw_rows_deleted": 0,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
