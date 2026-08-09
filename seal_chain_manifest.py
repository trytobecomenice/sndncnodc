#!/usr/bin/env python3
"""Record or verify an external anchor for the realized-event seal chain.

The manifest is intentionally tiny and non-overwriting. Internal chain audit
detects mutation; this external anchor detects a database replaced by an older,
internally valid prefix. Store it off the production DB volume.
"""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import time

from ledger_integrity import seal_chain_state


RECORDER_VERSION = "paper-ledger-seal-manifest-v1"
STATE_FIELDS = ("seal_table_present", "chain_head_sha256", "seal_count", "latest_range_end")


def read_state(db_path):
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return seal_chain_state(conn)
    finally:
        conn.close()


def record_manifest(db_path, output_path, now=None):
    output_path = Path(output_path)
    if output_path.exists():
        raise RuntimeError(f"refusing to overwrite external seal manifest: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    state = read_state(db_path)
    manifest = {
        **state,
        "recorded_at": int(now or time.time()),
        "recorder_version": RECORDER_VERSION,
    }
    # Exclusive create prevents a concurrent operator from replacing the
    # recovery anchor after checking existence.
    with output_path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return manifest


def _manifest_from_state(state, now=None):
    return {**state, "recorded_at": int(now or time.time()),
            "recorder_version": RECORDER_VERSION}


def verify_state_against_manifest(state, manifest):
    if manifest.get("recorder_version") != RECORDER_VERSION:
        raise RuntimeError("unsupported seal manifest recorder version")
    mismatches = {
        field: {"expected": manifest.get(field), "actual": state.get(field)}
        for field in STATE_FIELDS if manifest.get(field) != state.get(field)
    }
    if mismatches:
        raise RuntimeError(
            f"seal-chain external-manifest mismatch: {json.dumps(mismatches, sort_keys=True)}"
        )


def latest_final_manifest(directory):
    directory = Path(directory)
    pending = sorted(directory.glob("*.pending.json")) if directory.exists() else []
    if pending:
        raise RuntimeError(f"unresolved pending seal manifest: {pending[0]}")
    manifests = sorted(directory.glob("seal-*.json")) if directory.exists() else []
    if not manifests:
        raise RuntimeError("no external seal-chain anchor; explicit genesis record required")
    return manifests[-1]


def verify_latest_anchor(conn, directory):
    path = latest_final_manifest(directory)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    state = seal_chain_state(conn)
    verify_state_against_manifest(state, manifest)
    return path


def stage_manifest(conn, directory, anchor_id, now=None):
    """Durably stage the uncommitted DB chain state on the external volume."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if list(directory.glob("*.pending.json")):
        raise RuntimeError("unresolved pending seal manifest blocks pruning")
    manifest = _manifest_from_state(seal_chain_state(conn), now=now)
    stem = f"seal-{manifest['seal_count']:012d}-{manifest['chain_head_sha256'][:16]}-{anchor_id}"
    pending = directory / f"{stem}.pending.json"
    final = directory / f"{stem}.json"
    if final.exists():
        raise RuntimeError(f"refusing duplicate external seal anchor: {final}")
    with pending.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return pending, final


def finalize_staged_manifest(pending, final):
    os.replace(pending, final)
    directory_fd = os.open(Path(final).parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def recover_pending_manifest(db_path, directory):
    """Finalize only a pending anchor that exactly matches committed DB state."""
    directory = Path(directory)
    pending_files = sorted(directory.glob("*.pending.json"))
    if len(pending_files) != 1:
        raise RuntimeError(f"expected exactly one pending seal manifest; found {len(pending_files)}")
    pending = pending_files[0]
    manifest = json.loads(pending.read_text(encoding="utf-8"))
    verify_state_against_manifest(read_state(db_path), manifest)
    final = pending.with_name(pending.name.replace(".pending.json", ".json"))
    if final.exists():
        raise RuntimeError(f"refusing to overwrite finalized seal manifest: {final}")
    finalize_staged_manifest(pending, final)
    return {"status": "RECOVERED", "final": str(final), "manifest": manifest}


def verify_manifest(db_path, manifest_path):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    actual = read_state(db_path)
    verify_state_against_manifest(actual, manifest)
    return {"status": "PASS", "manifest": manifest, "actual": actual}


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record")
    record.add_argument("--db", type=Path, required=True)
    record.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--db", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    recover = subparsers.add_parser("recover-pending")
    recover.add_argument("--db", type=Path, required=True)
    recover.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "record":
        result = record_manifest(args.db, args.output)
    elif args.command == "verify":
        result = verify_manifest(args.db, args.manifest)
    else:
        result = recover_pending_manifest(args.db, args.directory)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
