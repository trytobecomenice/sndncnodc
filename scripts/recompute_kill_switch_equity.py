#!/usr/bin/env python3
"""Read-only, timestamped equity review required before a kill-switch reset."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot
import config
import db
import risk_manager


def review_equity(max_workers=10):
    positions = db.load_state()["positions"]

    def fetch(item):
        key, position = item
        parts = key.split("|", 2)
        if len(parts) != 3:
            return key, position, None, None, "malformed_position_key"
        best_bid, indicative, error = bot.get_market_prices(parts[1], parts[2])
        return key, position, best_bid, indicative, error

    observations = []
    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as executor:
        futures = [executor.submit(fetch, item) for item in positions.items()]
        for future in as_completed(futures):
            key, position, best_bid, indicative, error = future.result()
            observations.append({
                "position_key": key,
                "shares": float(position.get("shares") or 0),
                "cost_basis_usd": float(position.get("cost_basis_usd") or 0),
                "best_bid": best_bid,
                "indicative_price": indicative,
                "error": error,
            })
    observations.sort(key=lambda row: row["position_key"])

    realized = db.realized_pnl_total()
    risk_prices = {
        row["position_key"]: row["indicative_price"]
        for row in observations
        if isinstance(row["indicative_price"], (int, float))
        and math.isfinite(float(row["indicative_price"]))
    }
    risk_breakdown = risk_manager.compute_equity_breakdown(
        positions, risk_prices, realized
    )

    # Strict liquidation lower bound: no bid means zero exit proceeds, never
    # cost carry or a guessed midpoint. This is intentionally more conservative
    # than the bot's configured risk mark and is reported side by side.
    liquidation_unrealized = sum(
        (float(row["best_bid"]) * row["shares"] if row["best_bid"] is not None else 0.0)
        - row["cost_basis_usd"]
        for row in observations
    )
    liquidation_equity = config.PAPER_BANKROLL_USD + realized + liquidation_unrealized
    hwm = db.get_risk_value("equity_hwm")
    _, risk_triggers = risk_manager.evaluate_equity(risk_breakdown["total_equity"], hwm)
    _, liquidation_triggers = risk_manager.evaluate_equity(liquidation_equity, hwm)
    integrity = db.get_realized_ledger_integrity_status()

    return {
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "ledger_integrity": integrity,
        "equity_hwm_usd": hwm,
        "realized_pnl_usd": realized,
        "position_count": len(observations),
        "indicative_price_count": len(risk_prices),
        "executable_bid_count": sum(row["best_bid"] is not None for row in observations),
        "risk_equity": risk_breakdown,
        "risk_equity_triggers": risk_triggers,
        "strict_liquidation_unrealized_pnl_usd": liquidation_unrealized,
        "strict_liquidation_equity_usd": liquidation_equity,
        "strict_liquidation_triggers": liquidation_triggers,
        "positions": observations,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-workers", type=int, default=10)
    parser.add_argument("--output")
    args = parser.parse_args()
    report = review_equity(args.max_workers)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload)
    print(payload, end="")


if __name__ == "__main__":
    main()
