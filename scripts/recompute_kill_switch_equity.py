#!/usr/bin/env python3
"""Read-only, timestamped equity review required before a kill-switch reset."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot
import config
import db
import polymarket_simulator
import risk_manager


def review_equity(max_workers=10):
    positions = db.load_state()["positions"]

    def fetch(item):
        key, position = item
        parts = key.split("|", 2)
        if len(parts) != 3:
            return {
                "position_key": key,
                "position": position,
                "best_bid": None,
                "indicative_price": None,
                "error": "malformed_position_key",
                "quote_status": "price_error",
                "book_age_seconds": None,
                "stale_best_bid": None,
            }
        best_bid, indicative, error = bot.get_market_prices(parts[1], parts[2])
        quote_status = "live_executable" if best_bid is not None else "price_error"
        book_age_seconds = None
        stale_best_bid = None
        if best_bid is None and error is None:
            # get_market_prices deliberately hides a stale book behind an
            # indicative-only result so TTP can update its peak without ever
            # selling on old data.  A reset review needs to distinguish that
            # case from a genuinely fresh one-sided/no-bid book.  The second
            # read remains diagnostic only: stale_best_bid never contributes
            # to strict_liquidation_equity_usd.
            try:
                _, book = polymarket_simulator.fetch_order_book_for_outcome(
                    parts[1], parts[2], ignore_staleness=True
                )
                timestamp_ms = book.get("book_timestamp_ms")
                if timestamp_ms is not None:
                    book_age_seconds = max(
                        0.0, time.time() - (float(timestamp_ms) / 1000.0)
                    )
                if book.get("bids"):
                    stale_best_bid = bot._positive_finite_price(book["bids"][0][0])
                quote_status = (
                    "stale_book"
                    if book_age_seconds is not None
                    and book_age_seconds > polymarket_simulator.MAX_BOOK_AGE_SECONDS
                    else "fresh_no_bid"
                )
            except Exception as diagnostic_error:
                quote_status = "diagnostic_error"
                error = f"non-executable quote diagnosis failed: {diagnostic_error}"
        return {
            "position_key": key,
            "position": position,
            "best_bid": best_bid,
            "indicative_price": indicative,
            "error": error,
            "quote_status": quote_status,
            "book_age_seconds": book_age_seconds,
            "stale_best_bid": stale_best_bid,
        }

    observations = []
    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as executor:
        futures = [executor.submit(fetch, item) for item in positions.items()]
        for future in as_completed(futures):
            result = future.result()
            key = result["position_key"]
            position = result["position"]
            observations.append({
                "position_key": key,
                "shares": float(position.get("shares") or 0),
                "cost_basis_usd": float(position.get("cost_basis_usd") or 0),
                "best_bid": result["best_bid"],
                "indicative_price": result["indicative_price"],
                "error": result["error"],
                "quote_status": result["quote_status"],
                "book_age_seconds": result["book_age_seconds"],
                "stale_best_bid": result["stale_best_bid"],
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
    stale_quote_unrealized = sum(
        (
            float(row["best_bid"]) * row["shares"]
            if row["best_bid"] is not None
            else float(row["stale_best_bid"]) * row["shares"]
            if row["stale_best_bid"] is not None
            else 0.0
        ) - row["cost_basis_usd"]
        for row in observations
    )
    stale_quote_equity = config.PAPER_BANKROLL_USD + realized + stale_quote_unrealized
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
        "quote_status_counts": {
            status: sum(row["quote_status"] == status for row in observations)
            for status in sorted({row["quote_status"] for row in observations})
        },
        "risk_equity": risk_breakdown,
        "risk_equity_triggers": risk_triggers,
        "strict_liquidation_unrealized_pnl_usd": liquidation_unrealized,
        "strict_liquidation_equity_usd": liquidation_equity,
        "strict_liquidation_triggers": liquidation_triggers,
        # Diagnostic only. A stale quote is not executable and this value
        # must never authorize a reset; it only attributes the gap between
        # the live liquidation lower bound and the normal risk mark.
        "stale_quote_equity_usd": stale_quote_equity,
        "stale_quote_unrealized_pnl_usd": stale_quote_unrealized,
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
