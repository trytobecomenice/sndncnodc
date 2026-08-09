#!/usr/bin/env python3
"""Build a PnL-blind, observation-only wallet behaviour profile.

This deliberately does *not* call itself a maker/taker classifier.  Phase-0
learns about a source trade after it happened, so its decision book is not a
causal pre-trade BBO.  A post-trade book cannot prove which side supplied
liquidity.  Missing evidence remains UNKNOWN instead of being converted into
a confident trader archetype.

The output is a static research artifact.  This module has no database write,
network, roster, risk-gate, or order capability.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import statistics


PROFILE_VERSION = "wallet-archetype-evidence-v1"


def _number(value):
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return float(parsed)


def _integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quantile(values, fraction):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * float(fraction)
    lower = int(index)
    upper = min(len(ordered) - 1, lower + 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _iter_records(paths):
    for path in (Path(item) for item in paths):
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    yield path, line_number, None
                else:
                    yield path, line_number, record


def _signal_identity(path, record):
    signal = record.get("signal") or {}
    api_trade_id = record.get("api_trade_id") or signal.get("trade_id")
    if api_trade_id:
        return ("api_trade_id", str(api_trade_id))
    event_id = record.get("signal_event_id") or record.get("correlation_id")
    if event_id:
        return ("event_id", str(path), str(event_id))
    payload = json.dumps(signal, sort_keys=True, separators=(",", ":"))
    return ("payload_hash", hashlib.sha256(payload.encode()).hexdigest())


def _load_signals(paths):
    signals = []
    seen = set()
    malformed = 0
    duplicates = 0
    for path, _line_number, record in _iter_records(paths):
        if record is None:
            malformed += 1
            continue
        if record.get("event_type") != "wallet_signal":
            continue
        identity = _signal_identity(path, record)
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        signal = record.get("signal") or {}
        wallet = str(signal.get("user_address") or "").lower()
        side = str(signal.get("side") or "").upper()
        if not wallet or side not in {"BUY", "SELL"}:
            continue
        signals.append({
            "identity": identity,
            "wallet": wallet,
            "market_slug": str(signal.get("market_slug") or ""),
            "outcome": str(signal.get("outcome") or ""),
            "side": side,
            "size_usd": _number(signal.get("size_usd")),
            "source_price": _number(signal.get("price")),
            "source_reported_timestamp_ms": _integer(
                record.get("source_reported_timestamp_ms")
            ),
            "first_local_seen_timestamp_ms": _integer(
                record.get("first_local_seen_timestamp_ms")
            ),
            "reported_visibility_lag_ms": _integer(
                record.get("reported_visibility_lag_ms")
            ),
        })
    signals.sort(key=lambda item: (
        item["first_local_seen_timestamp_ms"] is None,
        item["first_local_seen_timestamp_ms"] or 0,
        repr(item["identity"]),
    ))
    return signals, {"malformed_json_lines": malformed, "duplicate_signals": duplicates}


def _cross_market_burst_participation(signals, window_ms):
    """Return signal IDs participating in same-wallet, cross-market bursts.

    This is a multi-leg *candidate*, not proof of arbitrage.  The window is
    emitted in the artifact so the descriptive statistic is reproducible.
    """
    participating = set()
    by_wallet = defaultdict(list)
    for signal in signals:
        if signal["first_local_seen_timestamp_ms"] is not None:
            by_wallet[signal["wallet"]].append(signal)
    for items in by_wallet.values():
        for left_index, left in enumerate(items):
            left_time = left["first_local_seen_timestamp_ms"]
            for right in items[left_index + 1:]:
                delta = right["first_local_seen_timestamp_ms"] - left_time
                if delta > window_ms:
                    break
                if left["market_slug"] and right["market_slug"] \
                        and left["market_slug"] != right["market_slug"]:
                    participating.add(left["identity"])
                    participating.add(right["identity"])
    return participating


def build_profiles(journal_paths, *, cross_market_window_ms=1_000):
    """Return JSON-safe evidence without reading or deriving wallet PnL."""
    signals, audit = _load_signals(journal_paths)
    burst_ids = _cross_market_burst_participation(signals, int(cross_market_window_ms))
    grouped = defaultdict(list)
    for signal in signals:
        grouped[signal["wallet"]].append(signal)

    profiles = []
    for wallet, items in sorted(grouped.items()):
        buys = [item for item in items if item["side"] == "BUY"]
        sells = [item for item in items if item["side"] == "SELL"]
        known_size = [item for item in items if item["size_usd"] is not None]
        buy_notional = sum(item["size_usd"] for item in buys if item["size_usd"] is not None)
        sell_notional = sum(item["size_usd"] for item in sells if item["size_usd"] is not None)
        total_notional = buy_notional + sell_notional
        by_contract = defaultdict(set)
        for item in items:
            by_contract[(item["market_slug"], item["outcome"])].add(item["side"])
        two_way = sum(1 for sides in by_contract.values() if sides == {"BUY", "SELL"})
        visibility = [
            item["reported_visibility_lag_ms"] for item in items
            if item["reported_visibility_lag_ms"] is not None
            and item["reported_visibility_lag_ms"] >= 0
        ]
        burst_count = sum(item["identity"] in burst_ids for item in items)
        profiles.append({
            "wallet": wallet,
            "profile_version": PROFILE_VERSION,
            "observation_counts": {
                "signals": len(items),
                "buy_signals": len(buys),
                "sell_signals": len(sells),
                "known_notional_signals": len(known_size),
                "distinct_market_outcomes": len(by_contract),
            },
            "flow_evidence": {
                "buy_notional_usd": round(buy_notional, 8),
                "sell_notional_usd": round(sell_notional, 8),
                "absolute_notional_directionality": (
                    abs(buy_notional - sell_notional) / total_notional
                    if total_notional > 0 else None
                ),
                "two_way_market_outcome_count": two_way,
                "two_way_market_outcome_ratio": (
                    two_way / len(by_contract) if by_contract else None
                ),
            },
            "timing_evidence": {
                "known_visibility_lag_count": len(visibility),
                "median_reported_visibility_lag_ms": (
                    statistics.median(visibility) if visibility else None
                ),
                "p90_reported_visibility_lag_ms": _quantile(visibility, 0.90),
                "cross_market_burst_signal_count": burst_count,
                "cross_market_burst_signal_ratio": (
                    burst_count / len(items) if items else None
                ),
                "cross_market_window_ms": int(cross_market_window_ms),
                "cross_market_interpretation": "multi_leg_candidate_not_arbitrage_proof",
            },
            "maker_taker_evidence": {
                "status": "UNKNOWN",
                "reason": (
                    "phase0 observes the wallet trade before capturing/reading the decision book; "
                    "the available book is not a causal pre-trade BBO and cannot identify the "
                    "source wallet's liquidity role"
                ),
                "required_evidence": (
                    "venue-provided maker/taker role or a causally ordered pre-trade BBO/trade tape"
                ),
            },
            "inventory_evidence": {
                "status": "LEFT_CENSORED_UNKNOWN",
                "reason": (
                    "observed flow does not prove starting inventory, off-CLOB transfers, or "
                    "cross-venue hedges"
                ),
            },
            "archetype": {
                "status": "NOT_CLASSIFIED_OBSERVATION_ONLY",
                "reason": "continuous evidence is reported without PnL-selected thresholds",
            },
        })

    return {
        "schema_version": PROFILE_VERSION,
        "research_only": True,
        "pnl_blind": True,
        "writes_bot_database": False,
        "changes_roster_or_risk": False,
        "inputs": [str(Path(item)) for item in journal_paths],
        "parameters": {"cross_market_window_ms": int(cross_market_window_ms)},
        "audit": {**audit, "unique_valid_signals": len(signals), "wallet_count": len(profiles)},
        "profiles": profiles,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journals", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("trader_profile_v1.json"))
    parser.add_argument("--cross-market-window-ms", type=int, default=1_000)
    args = parser.parse_args()
    artifact = build_profiles(
        args.journals, cross_market_window_ms=args.cross_market_window_ms
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "output": str(args.output),
        "wallet_count": artifact["audit"]["wallet_count"],
        "unique_valid_signals": artifact["audit"]["unique_valid_signals"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
