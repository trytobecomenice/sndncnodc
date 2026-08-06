#!/usr/bin/env python3
"""Generate deterministic hostile Phase-0 JSONL for offline stress tests."""

import argparse
import json
from pathlib import Path


def _book(bid, ask, generation, *, empty=False):
    return {
        "bids": [] if empty else [[bid, 100]],
        "asks": [] if empty else [[ask, 100]],
        "received_timestamp_ms": 1_000 + generation,
        "received_monotonic_ns": (1_000 + generation) * 1_000_000,
        "local_generation": generation,
        "reconnect_epoch": 1,
        "book_hash": f"book-{generation}",
    }


def _execution(side, price, tier=3):
    if side == "BUY":
        return {
            "average_price_micros": int(price * 1_000_000),
            "requested_usd_micros": tier * 1_000_000,
            "filled_usd_micros": tier * 1_000_000,
            "shares_micros": int(tier / price * 1_000_000),
            "fill_ratio_ppm": 1_000_000,
            "insufficient_liquidity": False,
        }
    return {
        "average_price_micros": int(price * 1_000_000),
        "requested_shares_micros": 3_000_000,
        "filled_shares_micros": 3_000_000,
        "liquidation_ratio_ppm": 1_000_000,
        "insufficient_liquidity": False,
    }


def _signal(signal_id, wallet, market, side, timestamp_ms, t0_price):
    book = _book(t0_price - 0.01, t0_price + 0.01, timestamp_ms - 900)
    execution = _execution(side, t0_price)
    open_lots = (
        [{
            "lot_id": signal_id,
            "shares_micros": execution["shares_micros"],
            "cost_basis_micros": 3_000_000,
        }]
        if side == "BUY" else []
    )
    return {
        "schema_version": 1,
        "event_type": "wallet_signal",
        "event_id": f"{signal_id}:attribution",
        "signal_event_id": signal_id,
        "correlation_id": signal_id,
        "api_trade_id": f"api-{signal_id}",
        "first_local_seen_timestamp_ms": timestamp_ms,
        "source_reported_timestamp_ms": timestamp_ms - 500,
        "reported_visibility_lag_ms": 500,
        "signal": {
            "trade_id": f"trade-{signal_id}", "user_address": wallet,
            "market_slug": market, "outcome": "Yes", "side": side,
            "price": 0.50, "size_usd": 20,
        },
        "decision_book_age_ns": 1_000_000,
        "decision_book": book,
        "shadow_lifecycle": {
            "tiers": {
                "3": {
                    "action": f"hypothetical_{side.lower()}",
                    "execution": execution,
                    "realized_pnl_usd_micros": None,
                }
            },
            "ledger_after": {
                "source_shares_micros": 0,
                "shadow_lots": {"3": open_lots},
                "lot_allocation_policy": "worst_execution_first",
                "realized_pnl_micros": {"3": 0},
            },
        },
        "quality_flags": [],
        "error": None,
    }


def _delayed(signal_id, side, timestamp_ms, price, *, known=True, empty=False):
    return {
        "schema_version": 1,
        "event_type": "delayed_book_observation",
        "event_id": f"{signal_id}:t+100ms",
        "correlation_id": signal_id,
        "target_delay_ms": 100,
        "target_monotonic_ns": timestamp_ms * 1_000_000 + 100_000_000,
        "capture_monotonic_ns": timestamp_ms * 1_000_000 + 102_000_000,
        "capture_lateness_ns": 2_000_000,
        "book_known_by_capture_deadline": known,
        "target_snapshot_status": (
            "latest_generation_was_already_known_by_target"
            if known else "not_proven_known_by_target"
        ),
        "book": _book(price - 0.01, price + 0.01, timestamp_ms - 899, empty=empty),
        "tier_execution_observations": {
            "3": None if empty else _execution(side, price)
        },
    }


def hostile_records(base_timestamp_ms=1_800_000_000_000):
    records = []
    flash_id = "stress-flash-sell"
    records.extend([
        _signal(flash_id, "0xflash", "flash-market", "SELL", base_timestamp_ms, 0.49),
        _delayed(flash_id, "SELL", base_timestamp_ms, 0.245),
    ])
    for index in range(5):
        timestamp = base_timestamp_ms + 10_000 + index * 400
        signal_id = f"stress-cluster-{index}"
        records.extend([
            _signal(signal_id, f"0xcluster{index}", "cluster-market", "BUY", timestamp, 0.51),
            _delayed(signal_id, "BUY", timestamp, 0.52),
        ])
    ghost_id = "stress-ghost"
    ghost_timestamp = base_timestamp_ms + 30_000
    records.extend([
        _signal(ghost_id, "0xghost", "ghost-market", "BUY", ghost_timestamp, 0.51),
        _delayed(ghost_id, "BUY", ghost_timestamp, 0.99, known=False, empty=True),
    ])
    return records


def write_hostile_journal(path, base_timestamp_ms=1_800_000_000_000):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = hostile_records(base_timestamp_ms)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return {"path": str(path), "record_count": len(records)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="research/fixtures/phase0-hostile.jsonl")
    args = parser.parse_args(argv)
    print(json.dumps(write_hostile_journal(args.output), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
