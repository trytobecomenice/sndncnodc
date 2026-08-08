#!/usr/bin/env python3
"""Read-only operator preflight using the exact evaluator gate functions."""

import argparse
import json
from pathlib import Path
import sqlite3
import time

import config
from evaluate_protocol_v2 import evaluate_preconditions


def preflight(db_path, protocol_path, now_ts=None):
    protocol = json.loads(Path(protocol_path).read_text())
    now_ts = int(now_ts or time.time())
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        checks, reasons = evaluate_preconditions(conn, protocol, now_ts)
    finally:
        conn.close()
    qualification = checks.get("qualification_gates", [])
    return {
        "evaluated_at": now_ts,
        "status": "READY" if not reasons else "NOT_READY",
        "reasons": reasons,
        "qualification_gates": qualification,
        "checks": checks,
        "epoch_clock_started": False,
        "live_authorized": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.SQLITE_PATH)
    parser.add_argument("--protocol", default="research/protocol_v2_draft.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = preflight(args.db, args.protocol)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
