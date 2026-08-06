#!/usr/bin/env python3
"""Observation-only cohort, cluster, session, PnL, and depth features.

Nothing in this module writes the bot database, changes a roster, or decides
whether a signal should trade.  Defaults are report dimensions, not gates.
"""

from collections import defaultdict
import csv
from datetime import datetime, time as wall_time, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import statistics
from zoneinfo import ZoneInfo


MICROS = 1_000_000


def _integer(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _boolean(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def read_normalized_rows(path):
    integer_fields = {
        "source_reported_timestamp_ms", "first_local_seen_timestamp_ms",
        "reported_visibility_lag_ms", "tier_usd", "observation_delay_ms",
        "source_price_micros", "t0_executable_price_micros",
        "executable_price_micros", "deterioration_from_source_micros",
        "deterioration_from_t0_micros", "requested_usd_micros",
        "filled_usd_micros", "requested_shares_micros", "filled_shares_micros",
        "fill_ratio_ppm", "capture_lateness_ns", "book_received_timestamp_ms",
        "book_received_monotonic_ns", "book_local_generation",
        "book_reconnect_epoch",
    }
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            row = dict(raw)
            for field in integer_fields:
                row[field] = _integer(row.get(field))
            row["causal_valid"] = _boolean(row.get("causal_valid"))
            row["insufficient_liquidity"] = _boolean(
                row.get("insufficient_liquidity")
            )
            yield row


def session_label(timestamp_ms, timezone_name="America/New_York",
                  session_start="09:30", session_end="16:00"):
    """Return a configurable reporting stratum, never a trading decision."""
    if timestamp_ms is None:
        return "UNKNOWN"
    zone = ZoneInfo(timezone_name)
    observed = datetime.fromtimestamp(int(timestamp_ms) / 1000, timezone.utc).astimezone(zone)
    start_hour, start_minute = (int(part) for part in session_start.split(":"))
    end_hour, end_minute = (int(part) for part in session_end.split(":"))
    inside = wall_time(start_hour, start_minute) <= observed.time() < wall_time(
        end_hour, end_minute
    )
    return "US_HOURS" if inside else "NON_US_HOURS"


def cluster_signals(rows, window_ms=300_000, max_diameter_ms=None):
    """Cluster same-market/same-side signals without unbounded chaining.

    This detects information cascades, not wallet identity.  One cluster is one
    candidate independent observation for uncertainty/sample-size reporting.
    `window_ms` limits adjacent gaps while `max_diameter_ms` limits the absolute
    first-to-last span.  By default both limits are equal.
    """
    max_diameter_ms = int(window_ms if max_diameter_ms is None else max_diameter_ms)
    unique = {}
    for row in rows:
        signal_id = row.get("signal_event_id")
        timestamp = _integer(row.get("first_local_seen_timestamp_ms"))
        if not signal_id or timestamp is None:
            continue
        unique.setdefault(str(signal_id), {
            "signal_event_id": str(signal_id),
            "wallet": str(row.get("wallet") or "").lower(),
            "market_slug": str(row.get("market_slug") or ""),
            "side": str(row.get("side") or "").upper(),
            "first_local_seen_timestamp_ms": timestamp,
        })

    grouped = defaultdict(list)
    for signal in unique.values():
        grouped[(signal["market_slug"], signal["side"])].append(signal)

    assignments = {}
    clusters = []
    for (market_slug, side), signals in sorted(grouped.items()):
        signals.sort(key=lambda item: (
            item["first_local_seen_timestamp_ms"], item["signal_event_id"]
        ))
        current = []
        for signal in signals:
            adjacent_gap = (
                signal["first_local_seen_timestamp_ms"]
                - current[-1]["first_local_seen_timestamp_ms"]
                if current else 0
            )
            diameter = (
                signal["first_local_seen_timestamp_ms"]
                - current[0]["first_local_seen_timestamp_ms"]
                if current else 0
            )
            if current and (
                adjacent_gap > int(window_ms) or diameter > max_diameter_ms
            ):
                clusters.append(_finish_cluster(
                    market_slug, side, current, window_ms, max_diameter_ms
                ))
                current = []
            current.append(signal)
        if current:
            clusters.append(_finish_cluster(
                market_slug, side, current, window_ms, max_diameter_ms
            ))

    for cluster in clusters:
        for signal_id in cluster["signal_event_ids"]:
            assignments[signal_id] = cluster["cluster_id"]
    return assignments, clusters


def _finish_cluster(market_slug, side, signals, window_ms, max_diameter_ms):
    identity = "|".join((
        market_slug,
        side,
        str(signals[0]["first_local_seen_timestamp_ms"]),
        ",".join(item["signal_event_id"] for item in signals),
    ))
    cluster_id = "signal-cluster:" + hashlib.sha256(identity.encode()).hexdigest()[:16]
    wallets = sorted({item["wallet"] for item in signals})
    return {
        "cluster_id": cluster_id,
        "market_slug": market_slug,
        "side": side,
        "start_timestamp_ms": signals[0]["first_local_seen_timestamp_ms"],
        "end_timestamp_ms": signals[-1]["first_local_seen_timestamp_ms"],
        "window_ms": int(window_ms),
        "max_diameter_ms": int(max_diameter_ms),
        "actual_diameter_ms": (
            signals[-1]["first_local_seen_timestamp_ms"]
            - signals[0]["first_local_seen_timestamp_ms"]
        ),
        "signal_count": len(signals),
        "wallet_count": len(wallets),
        "wallets": wallets,
        "signal_event_ids": [item["signal_event_id"] for item in signals],
        "interpretation": "information_cascade_candidate_not_wallet_identity",
        "boundary_model": "BOUNDED_TIME_NO_CONTINUOUS_MESSAGE_RATE_SERIES",
    }


def activity_aware_clustering_capability():
    """Declare why message-rate cascade termination is not yet estimable."""
    return {
        "status": "UNAVAILABLE_FROM_CURRENT_PHASE0_SCHEMA",
        "reason": (
            "Signal-triggered T0/T+ books do not form a continuous order-book message-rate "
            "series. Inferring a return-to-baseline boundary would fabricate missing data."
        ),
        "required_evidence": (
            "Continuous per-market message counts in fixed monotonic-time buckets, an "
            "explicit rolling baseline, and a hard maximum diameter safety cap."
        ),
    }


def realized_pnl_by_wallet_tier(journal_paths):
    """Sum per-SELL realized shadow PnL without double-counting ledger totals."""
    totals = defaultdict(int)
    seen_attributions = set()
    for journal_path in journal_paths:
        with Path(journal_path).open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if record.get("event_type") != "wallet_signal":
                    continue
                attribution_id = (str(journal_path), str(record.get("event_id")))
                if attribution_id in seen_attributions:
                    continue
                seen_attributions.add(attribution_id)
                signal = record.get("signal") or {}
                wallet = str(signal.get("user_address") or "").lower()
                for tier, item in ((record.get("shadow_lifecycle") or {}).get("tiers") or {}).items():
                    value = (item or {}).get("realized_pnl_usd_micros")
                    if wallet and value is not None:
                        totals[(wallet, int(tier))] += int(value)
    return dict(totals)


def cohort_summary(rows, cluster_assignments=None, realized_pnl=None, open_lots=None):
    cluster_assignments = cluster_assignments or {}
    realized_pnl = realized_pnl or {}
    open_lots = open_lots or {}
    buckets = defaultdict(lambda: {
        "signals": set(), "clusters": set(), "source_gaps": [],
        "post_t0_decay": [], "causal": 0, "observations": 0,
    })
    for row in rows:
        wallet = str(row.get("wallet") or "").lower()
        tier = _integer(row.get("tier_usd"))
        if not wallet or tier is None:
            continue
        bucket = buckets[(wallet, tier)]
        bucket["observations"] += 1
        if row.get("causal_valid"):
            bucket["causal"] += 1
        if int(row.get("observation_delay_ms") or 0) == 0 and row.get("causal_valid"):
            signal_id = str(row.get("signal_event_id"))
            bucket["signals"].add(signal_id)
            bucket["clusters"].add(cluster_assignments.get(signal_id, signal_id))
            if row.get("deterioration_from_source_micros") is not None:
                bucket["source_gaps"].append(row["deterioration_from_source_micros"])
        elif row.get("causal_valid") and row.get("deterioration_from_t0_micros") is not None:
            bucket["post_t0_decay"].append(row["deterioration_from_t0_micros"])

    result = []
    for (wallet, tier), bucket in sorted(buckets.items()):
        result.append({
            "wallet": wallet,
            "tier_usd": tier,
            "accepted_signal_count": len(bucket["signals"]),
            "independent_signal_cluster_count": len(bucket["clusters"]),
            "realized_shadow_pnl_usd": (
                realized_pnl[(wallet, tier)] / MICROS
                if (wallet, tier) in realized_pnl else None
            ),
            "open_committed_capital_usd": (
                open_lots.get((wallet, tier), {}).get("known_cost_basis_micros", 0)
                / MICROS
            ),
            "open_lot_count": open_lots.get((wallet, tier), {}).get("open_lot_count", 0),
            "open_lot_unknown_cost_count": open_lots.get((wallet, tier), {}).get(
                "unknown_cost_count", 0
            ),
            "oldest_open_lot_age_hours": open_lots.get((wallet, tier), {}).get(
                "oldest_age_hours"
            ),
            "median_open_lot_age_hours": open_lots.get((wallet, tier), {}).get(
                "median_age_hours"
            ),
            "median_source_price_gap": (
                statistics.median(bucket["source_gaps"]) / MICROS
                if bucket["source_gaps"] else None
            ),
            "median_post_t0_decay": (
                statistics.median(bucket["post_t0_decay"]) / MICROS
                if bucket["post_t0_decay"] else None
            ),
            "causal_coverage_ratio": (
                bucket["causal"] / bucket["observations"]
                if bucket["observations"] else None
            ),
        })
    return result


def _record_wall_ms(record):
    for field in (
        "timestamp_ms", "poll_completed_ms", "first_local_seen_timestamp_ms",
        "source_reported_timestamp_ms",
    ):
        value = _integer(record.get(field))
        if value is not None:
            return value
    return None


def open_lots_by_wallet_tier(journal_paths, cutoff_timestamp_ms=None):
    """Rebuild the latest open tax lots and their known committed capital.

    This is accounting evidence, not MtM. Unknown cost basis and unknown age
    remain explicit rather than being treated as zero.
    """
    latest_positions = {}
    lot_opened_ms = {}
    observed_cutoff = _integer(cutoff_timestamp_ms)
    seen_attributions = set()
    for journal_path in journal_paths:
        with Path(journal_path).open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                wall_ms = _record_wall_ms(record)
                if wall_ms is not None:
                    observed_cutoff = max(observed_cutoff or wall_ms, wall_ms)
                if record.get("event_type") != "wallet_signal":
                    continue
                attribution_id = (str(journal_path), str(record.get("event_id")))
                if attribution_id in seen_attributions:
                    continue
                seen_attributions.add(attribution_id)
                signal = record.get("signal") or {}
                wallet = str(signal.get("user_address") or "").lower()
                signal_id = str(
                    record.get("signal_event_id") or record.get("correlation_id") or ""
                )
                first_seen_ms = _integer(record.get("first_local_seen_timestamp_ms"))
                if signal_id and first_seen_ms is not None:
                    lot_opened_ms.setdefault(signal_id, first_seen_ms)
                position_key = str(record.get("position_key") or "|".join((
                    wallet, str(signal.get("market_slug") or ""),
                    str(signal.get("outcome") or ""),
                )))
                prior = latest_positions.get(position_key)
                state_timestamp_ms = first_seen_ms if first_seen_ms is not None else -1
                if prior is not None and prior["state_timestamp_ms"] > state_timestamp_ms:
                    continue
                latest_positions[position_key] = {
                    "wallet": wallet,
                    "market_slug": signal.get("market_slug"),
                    "outcome": signal.get("outcome"),
                    "ledger_after": (
                        (record.get("shadow_lifecycle") or {}).get("ledger_after") or {}
                    ),
                    "state_timestamp_ms": state_timestamp_ms,
                }

    details = []
    summaries = defaultdict(lambda: {
        "known_cost_basis_micros": 0, "open_lot_count": 0,
        "unknown_cost_count": 0, "ages_hours": [],
    })
    for position in latest_positions.values():
        wallet = position["wallet"]
        shadow_lots = position["ledger_after"].get("shadow_lots") or {}
        for tier, lots in shadow_lots.items():
            tier_value = int(tier)
            for lot in lots or ():
                shares = _integer(lot.get("shares_micros")) or 0
                if shares <= 0:
                    continue
                lot_id = str(lot.get("lot_id") or "")
                cost = _integer(lot.get("cost_basis_micros"))
                opened_ms = lot_opened_ms.get(lot_id)
                age_hours = (
                    max(0, observed_cutoff - opened_ms) / 3_600_000
                    if observed_cutoff is not None and opened_ms is not None else None
                )
                key = (wallet, tier_value)
                summary = summaries[key]
                summary["open_lot_count"] += 1
                if cost is None:
                    summary["unknown_cost_count"] += 1
                else:
                    summary["known_cost_basis_micros"] += cost
                if age_hours is not None:
                    summary["ages_hours"].append(age_hours)
                details.append({
                    "wallet": wallet, "tier_usd": tier_value,
                    "market_slug": position["market_slug"], "outcome": position["outcome"],
                    "lot_id": lot_id, "shares_micros": shares,
                    "cost_basis_micros": cost, "opened_timestamp_ms": opened_ms,
                    "age_hours": age_hours,
                    "valuation_status": "COST_AND_AGE_ONLY_NO_MTM",
                })

    result = {}
    for key, summary in summaries.items():
        ages = summary.pop("ages_hours")
        result[key] = {
            **summary,
            "oldest_age_hours": max(ages) if ages else None,
            "median_age_hours": statistics.median(ages) if ages else None,
            "cutoff_timestamp_ms": observed_cutoff,
        }
    return result, details


def annotate_open_lot_topology_risk(open_lot_details, current_graph,
                                    previous_graph=None):
    """Attach structural topology evidence without inventing a valuation.

    A lot is marked TOPOLOGY_RISK when its event membership changed after the
    lot opened. Open events also remain on MUTABLE_TOPOLOGY_WATCH. A numerical
    haircut is deliberately unavailable until both an executable mark and a
    calibrated haircut policy exist.
    """
    from tools.build_event_graph import compare_snapshots

    topology = (current_graph or {}).get("topology") or {}
    nodes = {str(node.get("id")): node for node in topology.get("nodes") or ()}
    market_to_event = {}
    for edge in topology.get("edges") or ():
        if edge.get("relation") == "contains_market":
            market_to_event[str(edge.get("target"))] = str(edge.get("source"))
    slug_to_market = {
        str(node.get("slug")): node_id
        for node_id, node in nodes.items()
        if node.get("node_type") == "market" and node.get("slug")
    }
    change = (
        compare_snapshots(previous_graph, current_graph)
        if previous_graph is not None else None
    )
    changed_events = {
        str(item.get("event_node"))
        for item in (change or {}).get("events_with_changed_market_membership") or ()
    }
    changed_at_ms = _integer(topology.get("graph", {}).get("last_synced_epoch_ms"))

    annotated = []
    for original in open_lot_details:
        lot = dict(original)
        market_node = slug_to_market.get(str(lot.get("market_slug") or ""))
        event_node = market_to_event.get(market_node) if market_node else None
        event = nodes.get(event_node, {})
        opened_ms = _integer(lot.get("opened_timestamp_ms"))
        crossed_change = bool(
            event_node in changed_events
            and changed_at_ms is not None and opened_ms is not None
            and opened_ms <= changed_at_ms
        )
        if crossed_change:
            risk_status = "TOPOLOGY_RISK"
        elif event.get("completeness_status") == "OPEN_MUTABLE":
            risk_status = "MUTABLE_TOPOLOGY_WATCH"
        elif event_node:
            risk_status = "NO_OBSERVED_MEMBERSHIP_CHANGE"
        else:
            risk_status = "TOPOLOGY_MAPPING_UNAVAILABLE"
        lot.update({
            "topology_risk_status": risk_status,
            "topology_event_node": event_node,
            "topology_completeness_status": event.get("completeness_status"),
            "topology_change_observed_at_ms": changed_at_ms if crossed_change else None,
            "uncertainty_haircut_fraction": None,
            "uncertainty_haircut_status": (
                "BLOCKED_NO_EXECUTABLE_MTM_OR_CALIBRATED_POLICY"
                if crossed_change else "NOT_APPLIED"
            ),
        })
        annotated.append(lot)
    return annotated


def _notional_micros(levels):
    total = Decimal("0")
    for level in levels or ():
        try:
            price, size = level
            total += Decimal(str(price)) * Decimal(str(size))
        except (InvalidOperation, TypeError, ValueError):
            continue
    return int((total * MICROS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def extract_depth_observations(journal_paths):
    """Extract visible book depth for dashboarding; no liquidity claim is made."""
    bases = {}
    paths = [Path(path) for path in journal_paths]
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event_type") != "wallet_signal":
                    continue
                signal_id = record.get("signal_event_id") or record.get("correlation_id")
                signal = record.get("signal") or {}
                bases[(str(path), str(signal_id))] = {
                    "signal_event_id": str(signal_id),
                    "wallet": str(signal.get("user_address") or "").lower(),
                    "market_slug": signal.get("market_slug"),
                    "side": str(signal.get("side") or "").upper(),
                    "timestamp_ms": record.get("first_local_seen_timestamp_ms"),
                }

    rows = []
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = record.get("event_type")
                if event_type == "wallet_signal":
                    signal_id = record.get("signal_event_id") or record.get("correlation_id")
                    delay_ms = 0
                    book = record.get("decision_book")
                    known = bool(book) and isinstance(record.get("decision_book_age_ns"), int)
                elif event_type == "delayed_book_observation":
                    signal_id = record.get("correlation_id")
                    delay_ms = int(record.get("target_delay_ms") or 0)
                    book = record.get("book")
                    known = record.get("book_known_by_capture_deadline") is True
                else:
                    continue
                base = bases.get((str(path), str(signal_id)))
                if base is None:
                    continue
                book = book or {}
                rows.append({
                    **base,
                    "observation_delay_ms": delay_ms,
                    "observation_label": "T0" if delay_ms == 0 else f"T+{delay_ms}ms",
                    "causal_valid": known,
                    "visible_bid_notional_usd": _notional_micros(book.get("bids")) / MICROS,
                    "visible_ask_notional_usd": _notional_micros(book.get("asks")) / MICROS,
                    "book_local_generation": book.get("local_generation"),
                })
    return rows


def cutoff_mtm_capability():
    """State the present evidence boundary instead of fabricating a mark."""
    return {
        "status": "UNAVAILABLE_FROM_CURRENT_PHASE0_SCHEMA",
        "reason": (
            "The recorder stores signal-triggered T0/T+ checkpoints, not a continuous "
            "cutoff-window book series for every open shadow position."
        ),
        "required_evidence": (
            "Multiple causally-known bid-side executable marks per open lot across a "
            "declared cutoff window, with coverage and dispersion reported."
        ),
    }
