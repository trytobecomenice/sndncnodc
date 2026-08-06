#!/usr/bin/env python3
"""Build an offline Polymarket event/market/outcome topology and constraints map."""

import argparse
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def _list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def fetch_events(*, limit=100, max_events=0, active=True, closed=False, timeout=20):
    events = []
    offset = 0
    while True:
        page_limit = min(int(limit), int(max_events) - len(events)) if max_events else int(limit)
        if page_limit <= 0:
            break
        query = urlencode({
            "limit": page_limit, "offset": offset,
            "active": str(bool(active)).lower(), "closed": str(bool(closed)).lower(),
        })
        request = Request(f"{GAMMA_EVENTS_URL}?{query}", headers={
            "Accept": "application/json", "User-Agent": "phase0-event-graph/1"
        })
        with urlopen(request, timeout=timeout) as response:
            page = json.load(response)
        if not isinstance(page, list):
            raise ValueError("Gamma events response must be a list")
        events.extend(page)
        if len(page) < page_limit or (max_events and len(events) >= max_events):
            break
        offset += len(page)
    return events


def build_event_graph(events, synced_at_epoch_ms=None):
    import networkx as nx

    synced_at_epoch_ms = int(
        time.time_ns() // 1_000_000 if synced_at_epoch_ms is None else synced_at_epoch_ms
    )
    graph = nx.DiGraph(
        schema_version="polymarket-event-topology-v1",
        interpretation="structural_topology_not_pricing_or_arbitrage",
        last_synced_epoch_ms=synced_at_epoch_ms,
    )
    constraints = []
    audit = {
        "event_count": 0, "market_count": 0, "outcome_count": 0,
        "standard_binary_market_count": 0, "negative_risk_event_count": 0,
        "augmented_negative_risk_event_count": 0,
        "markets_missing_outcomes": 0,
    }
    for event in events:
        event_id = str(event.get("id") or event.get("slug") or "")
        if not event_id:
            continue
        event_node = f"event:{event_id}"
        neg_risk = event.get("negRisk") is True
        markets = event.get("markets") or []
        augmented = bool(
            event.get("negRiskAugmented") is True
            or any((market or {}).get("negRiskOther") is True for market in markets)
        )
        graph.add_node(
            event_node, node_type="event", event_id=event_id,
            slug=event.get("slug"), title=event.get("title"),
            neg_risk=neg_risk, neg_risk_market_id=event.get("negRiskMarketID"),
            augmented_neg_risk=augmented,
            last_synced_epoch_ms=synced_at_epoch_ms,
            source_updated_at=event.get("updatedAt"),
            source_active=event.get("active"), source_closed=event.get("closed"),
            completeness_status=(
                "OPEN_MUTABLE"
                if event.get("active") is True and event.get("closed") is not True
                else "CLOSED_SNAPSHOT_NOT_PROVEN_IMMUTABLE"
                if event.get("closed") is True
                else "UNKNOWN_MUTABILITY"
            ),
        )
        audit["event_count"] += 1
        audit["negative_risk_event_count"] += int(neg_risk)
        audit["augmented_negative_risk_event_count"] += int(augmented)
        event_market_nodes = []
        for market in markets:
            market_id = str(market.get("id") or market.get("conditionId") or market.get("slug") or "")
            if not market_id:
                continue
            market_node = f"market:{market_id}"
            outcomes = [str(value) for value in _list(market.get("outcomes"))]
            standard_binary = {value.strip().lower() for value in outcomes} == {"yes", "no"}
            graph.add_node(
                market_node, node_type="market", market_id=market_id,
                condition_id=market.get("conditionId"), slug=market.get("slug"),
                question=market.get("question"), standard_binary=standard_binary,
                neg_risk_other=market.get("negRiskOther") is True,
            )
            graph.add_edge(event_node, market_node, relation="contains_market")
            event_market_nodes.append(market_node)
            audit["market_count"] += 1
            audit["standard_binary_market_count"] += int(standard_binary)
            audit["markets_missing_outcomes"] += int(not outcomes)
            outcome_nodes = []
            for index, outcome in enumerate(outcomes):
                outcome_node = f"outcome:{market_id}:{index}"
                graph.add_node(
                    outcome_node, node_type="outcome", label=outcome,
                    outcome_index=index, market_id=market_id,
                )
                graph.add_edge(market_node, outcome_node, relation="offers_outcome")
                outcome_nodes.append(outcome_node)
                audit["outcome_count"] += 1
            if len(outcome_nodes) > 1:
                constraints.append({
                    "constraint_type": "within_market_mutually_exclusive",
                    "event_node": event_node,
                    "market_node": market_node,
                    "outcome_nodes": outcome_nodes,
                    "evidence": "market_outcome_structure",
                })
        if neg_risk and len(event_market_nodes) > 1:
            constraints.append({
                "constraint_type": "neg_risk_exactly_one_market_yes",
                "event_node": event_node,
                "market_nodes": event_market_nodes,
                "evidence": "gamma_event_negRisk_true",
                "augmented": augmented,
                "interpretation_guard": (
                    "Placeholder/Other outcomes require separate handling; this is a topology "
                    "constraint and not an executable arbitrage quote."
                ),
            })
        membership = "|".join(sorted(event_market_nodes))
        graph.nodes[event_node]["observed_market_count"] = len(event_market_nodes)
        graph.nodes[event_node]["market_membership_hash"] = hashlib.sha256(
            membership.encode("utf-8")
        ).hexdigest()
    if not nx.is_directed_acyclic_graph(graph):
        raise ValueError("event topology unexpectedly contains a cycle")
    audit["constraint_count"] = len(constraints)
    return graph, constraints, audit


def serialize_graph(graph, constraints, audit):
    import networkx as nx

    return {
        "schema_version": "polymarket-event-knowledge-graph-v1",
        "topology": nx.node_link_data(graph, edges="edges"),
        "constraints": constraints,
        "audit": audit,
        "guardrails": [
            "Standard event sibling markets are not assumed mutually exclusive.",
            "No pricing, correlation, margin, or trade decision is produced.",
            "Negative-risk constraints require the explicit Gamma negRisk flag.",
        ],
    }


def compare_snapshots(previous, current):
    """Return structural changes; never silently bless a changed topology."""
    previous_topology = previous.get("topology") or {}
    current_topology = current.get("topology") or {}
    previous_nodes = {
        str(node.get("id")): node for node in previous_topology.get("nodes") or ()
    }
    current_nodes = {
        str(node.get("id")): node for node in current_topology.get("nodes") or ()
    }
    changed_membership = []
    for node_id in sorted(set(previous_nodes) & set(current_nodes)):
        before = previous_nodes[node_id]
        after = current_nodes[node_id]
        if (
            before.get("node_type") == "event"
            and before.get("market_membership_hash")
            != after.get("market_membership_hash")
        ):
            changed_membership.append({
                "event_node": node_id,
                "previous_market_count": before.get("observed_market_count"),
                "current_market_count": after.get("observed_market_count"),
                "previous_membership_hash": before.get("market_membership_hash"),
                "current_membership_hash": after.get("market_membership_hash"),
            })
    previous_sync_ms = previous_topology.get("graph", {}).get("last_synced_epoch_ms")
    current_sync_ms = current_topology.get("graph", {}).get("last_synced_epoch_ms")
    return {
        "previous_sync_epoch_ms": previous_sync_ms,
        "current_sync_epoch_ms": current_sync_ms,
        "added_nodes": sorted(set(current_nodes) - set(previous_nodes)),
        "removed_nodes": sorted(set(previous_nodes) - set(current_nodes)),
        "events_with_changed_market_membership": changed_membership,
        "topology_changed": bool(
            set(current_nodes) != set(previous_nodes) or changed_membership
        ),
        "action_guard": (
            "Any topology change requires downstream research artifacts to be recomputed; "
            "this diff does not authorize pricing, margin, or execution."
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", help="Offline Gamma events fixture; omit to call public API")
    parser.add_argument("--output", default="research/output/event-knowledge-graph-v1.json")
    parser.add_argument("--previous", help="Optional previous graph snapshot for topology diff")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-events", type=int, default=0)
    parser.add_argument("--include-closed", action="store_true")
    args = parser.parse_args(argv)
    if args.input_json:
        events = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    else:
        events = fetch_events(
            limit=args.limit, max_events=args.max_events,
            active=not args.include_closed, closed=args.include_closed,
        )
    graph, constraints, audit = build_event_graph(events)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = serialize_graph(graph, constraints, audit)
    if args.previous:
        previous = json.loads(Path(args.previous).read_text(encoding="utf-8"))
        payload["change_audit"] = compare_snapshots(previous, payload)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"output": str(output), "audit": audit}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
