#!/usr/bin/env python3
"""Create and verify a transactionally consistent SQLite online backup.

Refuses to start when the destination filesystem would exceed the configured
utilization ceiling or would lack a post-backup reserve.  It never overwrites
an existing recovery point.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def filesystem_capacity(path):
    stat = path.stat().st_dev  # validate that the parent exists/is accessible
    del stat
    usage = __import__("shutil").disk_usage(path)
    return {"total": usage.total, "used": usage.used, "free": usage.free}


def validate_capacity(db_path, output_path, max_projected_utilization=0.85,
                      minimum_reserve_multiple=0.5):
    db_size = db_path.stat().st_size
    capacity = filesystem_capacity(output_path.parent)
    reserve = max(int(db_size * minimum_reserve_multiple), int(capacity["total"] * 0.15))
    required_free = db_size + reserve
    projected_used = capacity["used"] + db_size
    projected_utilization = projected_used / capacity["total"]
    result = {
        "db_size_bytes": db_size,
        "destination_total_bytes": capacity["total"],
        "destination_free_bytes": capacity["free"],
        "required_free_bytes": required_free,
        "projected_utilization": projected_utilization,
        "max_projected_utilization": max_projected_utilization,
    }
    if capacity["free"] < required_free:
        raise RuntimeError(f"insufficient backup headroom: {json.dumps(result, sort_keys=True)}")
    if projected_utilization > max_projected_utilization:
        raise RuntimeError(f"backup would cross utilization ceiling: {json.dumps(result, sort_keys=True)}")
    return result


def create_backup(db_path, output_path):
    source = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(output_path)
    try:
        source.execute("PRAGMA busy_timeout=30000")
        source.backup(destination, pages=4096, sleep=0.05)
    finally:
        destination.close()
        source.close()

    check = sqlite3.connect(f"file:{output_path.resolve()}?mode=ro", uri=True, timeout=30)
    try:
        verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if verdict != "ok":
        raise RuntimeError(f"backup integrity_check failed: {verdict}")
    return {"backup_size_bytes": output_path.stat().st_size,
            "backup_sha256": sha256_file(output_path), "integrity_check": verdict}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-projected-utilization", type=float, default=0.85)
    parser.add_argument("--minimum-reserve-multiple", type=float, default=0.5)
    args = parser.parse_args()
    if not args.db.is_file():
        raise SystemExit(f"database not found: {args.db}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite backup: {args.output}")
    capacity = validate_capacity(
        args.db, args.output, args.max_projected_utilization,
        args.minimum_reserve_multiple,
    )
    result = {**capacity, **create_backup(args.db, args.output),
              "db": str(args.db), "output": str(args.output)}
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
