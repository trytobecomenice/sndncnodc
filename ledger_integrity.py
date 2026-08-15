#!/usr/bin/env python3
"""Durable E/A/L accounting invariants and retention seals.

E = bot_event_log realized evidence (while inside retention)
A = paper_trade_realized_allocation
L = paper_trade cumulative columns

Old E rows may only be pruned after their canonical E/A evidence is sealed.
Seals are append-only at the SQLite trigger layer and are re-derived from A
during every audit. Monetary and share aggregates use integer micros.
"""

from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import sqlite3
import time
import uuid


SEAL_TABLE = "paper_trade_event_seal"
SEALER_VERSION = "paper-ledger-sealer-v2"
MICRO = Decimal("1000000")
GENESIS_CHAIN_SHA256 = "0" * 64


def _micros(value):
    if value is None:
        return 0
    return int((Decimal(str(value)) * MICRO).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _sum_micros(values):
    """Quantize once after summing, preserving conservation across partial fills.

    Quantizing every fill first is not additive at a half-micro boundary: two
    individually rounded reductions can differ by one micro-share from the
    rounded acquired quantity even when the source decimals conserve exactly.
    """
    total = sum((Decimal(str(value or 0)) for value in values), Decimal("0"))
    return int((total * MICRO).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _allocation_columns(conn):
    return {row[1] for row in conn.execute(
        "PRAGMA table_info(paper_trade_realized_allocation)"
    ).fetchall()}


def _canonical_allocation(row):
    values = [
        row["event_id"], row["paper_trade_id"] or "", str(int(row["event_timestamp"])),
        str(int(row["event_sequence"])),
        row["event_type"], row["strategy"], str(_micros(row["pnl_usd"])),
        str(_micros(row["cost_basis_usd"])), row["allocation_status"],
        str(_micros(row["shares_closed"])), str(_micros(row["shares_remaining"])),
        row["termination_cause"], row["termination_classifier_version"],
    ]
    return "\x1f".join(values).encode("utf-8") + b"\n"


def _digest_rows(rows):
    digest = hashlib.sha256()
    pnl_micros = shares_micros = 0
    for row in rows:
        digest.update(_canonical_allocation(row))
        pnl_micros += _micros(row["pnl_usd"])
        shares_micros += _micros(row["shares_closed"])
    return digest.hexdigest(), pnl_micros, shares_micros


def _chain_digest(previous, range_start, range_end, event_count, pnl_micros,
                  shares_micros, canonical_sha256, sealer_version):
    payload = "\x1f".join(str(value) for value in (
        previous, int(range_start), int(range_end), int(event_count),
        int(pnl_micros), int(shares_micros), canonical_sha256, sealer_version,
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _allocation_rows(conn, where="1=1", params=()):
    columns = _allocation_columns(conn)
    shares_closed = "a.shares_closed" if "shares_closed" in columns else "NULL"
    shares_remaining = "a.shares_remaining" if "shares_remaining" in columns else "NULL"
    if "event_sequence" not in columns:
        raise RuntimeError("allocation ledger lacks durable event_sequence")
    event_sequence = "a.event_sequence"
    return conn.execute(
        "SELECT a.event_id,a.paper_trade_id,a.event_timestamp,"
        f"{event_sequence} event_sequence,a.event_type,a.strategy,"
        "a.pnl_usd,a.cost_basis_usd,a.allocation_status,"
        f"{shares_closed} shares_closed,{shares_remaining} shares_remaining,"
        "a.termination_cause,a.termination_classifier_version "
        f"FROM paper_trade_realized_allocation a WHERE {where} "
        "ORDER BY a.event_timestamp,event_sequence,a.event_id", params,
    ).fetchall()


def seal_chain_state(conn):
    """Small externally anchorable state; detects valid-prefix truncation."""
    if not _has_table(conn, SEAL_TABLE):
        return {
            "seal_table_present": False,
            "chain_head_sha256": GENESIS_CHAIN_SHA256,
            "seal_count": 0,
            "latest_range_end": None,
        }
    row = conn.execute(
        f"SELECT COUNT(*) seal_count,MAX(range_end) latest_range_end FROM {SEAL_TABLE}"
    ).fetchone()
    count = int(row["seal_count"] or 0)
    latest = conn.execute(
        f"SELECT chain_sha256,range_end FROM {SEAL_TABLE} ORDER BY range_end DESC LIMIT 1"
    ).fetchone()
    if bool(latest) != bool(count):
        raise RuntimeError("inconsistent seal-chain cardinality")
    return {
        "seal_table_present": True,
        "chain_head_sha256": latest["chain_sha256"] if latest else GENESIS_CHAIN_SHA256,
        "seal_count": count,
        "latest_range_end": int(latest["range_end"]) if latest else None,
    }


def seal_realized_events_before(conn, cutoff, realized_event_types):
    """Seal currently retained realized evidence before destructive pruning.

    Must run inside the same write transaction as DELETE. Any unresolved,
    missing, or economically divergent event aborts pruning.
    """
    if not (_has_table(conn, SEAL_TABLE)
            and _has_table(conn, "paper_trade_realized_allocation")):
        return None  # backward-compatible until migration 0028 is deployed
    placeholders = ",".join("?" for _ in realized_event_types)
    events = conn.execute(
        "SELECT id,timestamp,event_type,payload_json FROM bot_event_log "
        f"WHERE timestamp<? AND event_type IN ({placeholders}) ORDER BY timestamp,id",
        (cutoff, *realized_event_types),
    ).fetchall()
    if not events:
        return None
    ids = [row["id"] for row in events]
    # Avoid SQLite's bind-variable ceiling on the first large retention seal.
    # The timestamp predicate bounds the scan; event IDs remain the authority
    # for exact membership below.
    allocations = _allocation_rows(conn, "a.event_timestamp<?", (cutoff,))
    by_id = {row["event_id"]: row for row in allocations}
    if len(by_id) != len(events):
        missing = sorted(set(ids) - set(by_id))
        raise RuntimeError(f"cannot seal incomplete realized evidence; missing={missing[:5]}")
    for event in events:
        allocation = by_id[event["id"]]
        if (allocation["allocation_status"] not in
                {"matched", "historical_unreconstructable"}
                or not allocation["paper_trade_id"]):
            raise RuntimeError(f"cannot seal unresolved allocation {event['id']}")
        payload = json.loads(event["payload_json"])
        if _micros(payload.get("pnl_usd")) != _micros(allocation["pnl_usd"]):
            raise RuntimeError(f"event/allocation PnL mismatch for {event['id']}")
        if allocation["shares_closed"] is not None and (
                _micros(payload.get("our_shares_closed"))
                != _micros(allocation["shares_closed"])):
            raise RuntimeError(f"event/allocation shares mismatch for {event['id']}")
    ordered = [by_id[event_id] for event_id in ids]
    canonical_sha256, pnl_micros, shares_micros = _digest_rows(ordered)
    range_start = min(row["timestamp"] for row in events)
    range_end = max(row["timestamp"] for row in events)
    existing = conn.execute(
        f"SELECT COUNT(*) FROM {SEAL_TABLE} WHERE NOT(range_end<? OR range_start>?)",
        (range_start, range_end),
    ).fetchone()[0]
    if existing:
        raise RuntimeError("refusing overlapping realized-event seal range")
    previous = conn.execute(
        f"SELECT chain_sha256,range_end FROM {SEAL_TABLE} ORDER BY range_end DESC LIMIT 1"
    ).fetchone()
    previous_chain_sha256 = previous["chain_sha256"] if previous else GENESIS_CHAIN_SHA256
    if previous and int(range_start) <= int(previous["range_end"]):
        raise RuntimeError("refusing non-monotonic realized-event seal range")
    chain_sha256 = _chain_digest(
        previous_chain_sha256, range_start, range_end, len(ordered), pnl_micros,
        shares_micros, canonical_sha256, SEALER_VERSION,
    )
    seal = {
        "id": str(uuid.uuid4()), "range_start": range_start, "range_end": range_end,
        "event_count": len(ordered), "pnl_micros": pnl_micros,
        "shares_micros": shares_micros, "canonical_sha256": canonical_sha256,
        "previous_chain_sha256": previous_chain_sha256,
        "chain_sha256": chain_sha256,
        "sealer_version": SEALER_VERSION, "sealed_at": int(time.time()),
    }
    conn.execute(
        f"INSERT INTO {SEAL_TABLE}(id,range_start,range_end,event_count,pnl_micros,"
        "shares_micros,canonical_sha256,previous_chain_sha256,chain_sha256,"
        "sealer_version,sealed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        tuple(seal[key] for key in (
            "id", "range_start", "range_end", "event_count", "pnl_micros",
            "shares_micros", "canonical_sha256", "previous_chain_sha256",
            "chain_sha256", "sealer_version", "sealed_at")),
    )
    return seal


def audit_ledger(conn, realized_event_types):
    """Return machine-readable integrity evidence; never mutates the DB."""
    result = {"status": "PASS", "failures": [], "warnings": []}
    if not _has_table(conn, "paper_trade_realized_allocation"):
        return {"status": "UNKNOWN", "failures": [],
                "warnings": ["allocation_table_missing"]}
    event_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(bot_event_log)").fetchall()
    }
    source_sequence_invalid = 0
    if ("event_sequence" not in event_columns
            or not _has_table(conn, "bot_event_sequence_counter")):
        source_sequence_invalid = 1
    else:
        invalid_rows = conn.execute(
            "SELECT COUNT(*) FROM bot_event_log WHERE event_sequence IS NULL OR event_sequence<=0"
        ).fetchone()[0]
        duplicate_rows = conn.execute(
            "SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM bot_event_log "
            "GROUP BY event_sequence HAVING COUNT(*)>1)"
        ).fetchone()[0]
        counter = conn.execute(
            "SELECT next_value FROM bot_event_sequence_counter WHERE singleton=1"
        ).fetchone()
        max_sequence = conn.execute(
            "SELECT COALESCE(MAX(event_sequence),0) FROM bot_event_log"
        ).fetchone()[0]
        source_sequence_invalid = int(invalid_rows or 0) + int(duplicate_rows or 0)
        if not counter or int(counter["next_value"]) <= int(max_sequence):
            source_sequence_invalid += 1
    result["source_event_sequence_invalid"] = source_sequence_invalid
    if source_sequence_invalid:
        result["failures"].append("source_event_sequence_invalid")
    placeholders = ",".join("?" for _ in realized_event_types)

    missing_a = conn.execute(
        "SELECT COUNT(*) FROM bot_event_log e "
        f"WHERE e.event_type IN ({placeholders}) AND NOT EXISTS("
        "SELECT 1 FROM paper_trade_realized_allocation a WHERE a.event_id=e.id)",
        tuple(realized_event_types),
    ).fetchone()[0]
    missing_e = conn.execute(
        "SELECT COUNT(*) FROM paper_trade_realized_allocation a "
        "WHERE NOT EXISTS(SELECT 1 FROM bot_event_log e WHERE e.id=a.event_id) "
        + (f"AND NOT EXISTS(SELECT 1 FROM {SEAL_TABLE} s WHERE "
           "a.event_timestamp BETWEEN s.range_start AND s.range_end)"
           if _has_table(conn, SEAL_TABLE) else ""),
    ).fetchone()[0]
    economic_mismatch = 0
    has_event_sequence = "event_sequence" in _allocation_columns(conn)
    retained = conn.execute(
        "SELECT a.event_id,a.pnl_usd,a.cost_basis_usd,a.shares_closed,"
        + ("a.event_sequence," if has_event_sequence else "NULL event_sequence,")
        + "e.event_sequence evidence_sequence,e.payload_json "
        "FROM paper_trade_realized_allocation a JOIN bot_event_log e ON e.id=a.event_id"
        if "shares_closed" in _allocation_columns(conn) else
        "SELECT a.event_id,a.pnl_usd,a.cost_basis_usd,NULL shares_closed,"
        + ("a.event_sequence," if has_event_sequence else "NULL event_sequence,")
        + "e.event_sequence evidence_sequence,e.payload_json "
        "FROM paper_trade_realized_allocation a JOIN bot_event_log e ON e.id=a.event_id"
    ).fetchall()
    for row in retained:
        payload = json.loads(row["payload_json"])
        if _micros(payload.get("pnl_usd")) != _micros(row["pnl_usd"]):
            economic_mismatch += 1
        payload_basis = payload.get("cost_basis_usd")
        if (isinstance(payload_basis, bool)
                or not isinstance(payload_basis, (int, float))
                or row["cost_basis_usd"] is None
                or _micros(payload_basis) != _micros(row["cost_basis_usd"])):
            economic_mismatch += 1
        if (row["shares_closed"] is not None
                and _micros(payload.get("our_shares_closed")) != _micros(row["shares_closed"])):
            economic_mismatch += 1
        if (row["event_sequence"] is not None
                and int(row["event_sequence"]) != int(row["evidence_sequence"])):
            economic_mismatch += 1
    unresolved = conn.execute(
        "SELECT COUNT(*) FROM paper_trade_realized_allocation "
        "WHERE allocation_status NOT IN ('matched','historical_unreconstructable') "
        "OR paper_trade_id IS NULL"
    ).fetchone()[0]
    historical_unreconstructable = conn.execute(
        "SELECT COUNT(*) FROM paper_trade_realized_allocation "
        "WHERE allocation_status='historical_unreconstructable'"
    ).fetchone()[0]
    result.update({"retained_event_missing_allocation": missing_a,
                   "unsealed_allocation_missing_event": missing_e,
                   "retained_event_economic_mismatch": economic_mismatch,
                   "unresolved_allocations": unresolved,
                   "historical_unreconstructable_allocations": historical_unreconstructable})
    if missing_a:
        result["failures"].append("retained_event_missing_allocation")
    if missing_e:
        result["failures"].append("unsealed_allocation_missing_event")
    if economic_mismatch:
        result["failures"].append("retained_event_economic_mismatch")
    if unresolved:
        result["failures"].append("unresolved_allocation")

    # A -> L count and integer-micro PnL equality, per lot.
    lot_mismatches = []
    paper_columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_trade)").fetchall()}
    has_cumulative_basis = "cumulative_realized_cost_basis_usd" in paper_columns
    cumulative_basis_select = (
        "p.cumulative_realized_cost_basis_usd" if has_cumulative_basis else "NULL"
    )
    rows = conn.execute(
        "SELECT p.id,p.realized_event_count,p.cumulative_realized_pnl_usd,"
        f"{cumulative_basis_select} cumulative_realized_cost_basis_usd,"
        "COUNT(a.event_id) allocation_count,COALESCE(SUM(a.pnl_usd),0) allocation_pnl,"
        "COALESCE(SUM(a.cost_basis_usd),0) allocation_cost_basis "
        "FROM paper_trade p LEFT JOIN paper_trade_realized_allocation a "
        "ON a.paper_trade_id=p.id AND a.allocation_status='matched' GROUP BY p.id"
    ).fetchall()
    for row in rows:
        if (int(row["realized_event_count"] or 0) != int(row["allocation_count"] or 0)
                or _micros(row["cumulative_realized_pnl_usd"])
                != _micros(row["allocation_pnl"])
                or (has_cumulative_basis
                    and _micros(row["cumulative_realized_cost_basis_usd"])
                    != _micros(row["allocation_cost_basis"]))):
            lot_mismatches.append(row["id"])
    result["lot_ledger_mismatch_count"] = len(lot_mismatches)
    if lot_mismatches:
        result["failures"].append("allocation_to_lot_mismatch")

    # Every historical seal must still be exactly reproducible from A.
    seal_mismatches = []
    if _has_table(conn, SEAL_TABLE):
        previous_chain_sha256 = GENESIS_CHAIN_SHA256
        for seal in conn.execute(f"SELECT * FROM {SEAL_TABLE} ORDER BY range_start").fetchall():
            allocations = _allocation_rows(
                conn, "a.event_timestamp BETWEEN ? AND ?",
                (seal["range_start"], seal["range_end"]),
            )
            digest, pnl_micros, shares_micros = _digest_rows(allocations)
            expected_chain_sha256 = _chain_digest(
                previous_chain_sha256, seal["range_start"], seal["range_end"],
                seal["event_count"], seal["pnl_micros"], seal["shares_micros"],
                seal["canonical_sha256"], seal["sealer_version"],
            )
            if (len(allocations) != seal["event_count"] or pnl_micros != seal["pnl_micros"]
                    or shares_micros != seal["shares_micros"]
                    or digest != seal["canonical_sha256"]
                    or seal["previous_chain_sha256"] != previous_chain_sha256
                    or seal["chain_sha256"] != expected_chain_sha256
                    or seal["sealer_version"] != SEALER_VERSION):
                seal_mismatches.append(seal["id"])
            previous_chain_sha256 = seal["chain_sha256"]
    result["seal_mismatch_count"] = len(seal_mismatches)
    if seal_mismatches:
        result["failures"].append("retention_seal_mismatch")

    # Quantity conservation. total_acquired_shares is the durable acquisition
    # authority; allocations carry every reduction and its post-event balance.
    columns = _allocation_columns(conn)
    quantity_unknown = quantity_mismatch = 0
    if (not {"shares_closed", "shares_remaining"}.issubset(columns)
            or not {"total_acquired_shares", "our_shares", "status"}.issubset(paper_columns)):
        quantity_unknown = 1
    else:
        by_lot = {}
        for row in _allocation_rows(conn, "a.allocation_status='matched'"):
            by_lot.setdefault(row["paper_trade_id"], []).append(row)
        lots = conn.execute(
            "SELECT id,total_acquired_shares,our_shares,status FROM paper_trade "
            "WHERE total_acquired_shares IS NOT NULL"
        ).fetchall()
        for lot in lots:
            paper_trade_id = lot["id"]
            lot_rows = by_lot.get(paper_trade_id, [])
            if not lot_rows:
                # A never-reduced open lot still has a conservation equation:
                # acquired == current remaining. A closed lot without any
                # realized allocation cannot prove how its shares disappeared.
                if lot["status"] == "open":
                    quantity_mismatch += int(
                        _micros(lot["total_acquired_shares"]) != _micros(lot["our_shares"])
                    )
                else:
                    quantity_unknown += 1
                continue
            if any(row["shares_closed"] is None or row["shares_remaining"] is None
                   for row in lot_rows):
                quantity_unknown += 1
                continue
            sold = _sum_micros(row["shares_closed"] for row in lot_rows)
            remaining = _micros(lot_rows[-1]["shares_remaining"])
            accounted = _sum_micros(
                [*(row["shares_closed"] for row in lot_rows),
                 lot_rows[-1]["shares_remaining"]]
            )
            bad = (sold < 0 or remaining < 0
                   or _micros(lot["total_acquired_shares"]) != accounted)
            if lot["status"] == "closed" and remaining != 0:
                bad = True
            if lot["status"] == "open" and _micros(lot["our_shares"]) != remaining:
                bad = True
            quantity_mismatch += int(bad)
    result["quantity_mismatch_lots"] = quantity_mismatch
    result["quantity_unknown_lots"] = quantity_unknown
    if quantity_mismatch:
        result["failures"].append("sell_quantity_conservation_mismatch")
    if quantity_unknown:
        result["warnings"].append("quantity_conservation_not_proven")
    if result["failures"]:
        result["status"] = "FAIL"
    elif result["warnings"]:
        result["status"] = "UNKNOWN"
    return result
