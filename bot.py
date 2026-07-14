#!/usr/bin/env python3
"""
Polymarket copytrading bot (paper mode).

Watches TRACKED_TRADERS via `bullpen tracker feed`, and for each new trade:
- BUY  -> open/add to a simulated FIXED_TRADE_USD position in the same market+outcome
- SELL -> proportional: if the trader sells N% of their (observed) position in that
          market+outcome, we sell N% of our own simulated position too

"Observed" position size is tracked from trades seen since this bot started (or since
bootstrap) — pre-existing holdings the trader had before we started watching are not
visible to us, so a sell that exceeds what we've observed is clamped to selling 100%
of our own position.

Everything (fills, skips, errors) is appended to TRADE_LOG_PATH as a JSON array
so a future dashboard can read it directly.
"""

import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone

import config


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def run_bullpen_json(args):
    result = subprocess.run(
        ["bullpen"] + args + ["--output", "json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    data = None
    if result.stdout.strip():
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = None

    # A trade command can exit non-zero (e.g. exit 4 "trade execution
    # failed") while still printing a JSON error body to stdout — check the
    # exit code independent of whether stdout parsed, or a rejected/reverted
    # order silently sails through as if it succeeded.
    if result.returncode != 0:
        detail = result.stderr.strip()
        if isinstance(data, dict):
            detail = data.get("error") or data.get("error_code") or data.get("message") or detail
        raise RuntimeError(
            f"bullpen {' '.join(args)} exited {result.returncode}: {detail or 'no error detail'}"
        )
    if data is None:
        raise RuntimeError(f"bullpen {' '.join(args)} produced no parseable JSON output: {result.stdout!r}")
    if isinstance(data, dict) and data.get("ok") is False:
        raise RuntimeError(f"bullpen {' '.join(args)} error: {data.get('error')}")
    return data


# Statuses `bullpen polymarket buy/sell --output json` can report. Only
# MATCHED means the CLI actually settled shares/cash on-chain — UNMATCHED
# (no counterparty, e.g. swept by a much larger concurrent order), DELAYED,
# or a resting LIVE limit order are not fills. A 0 exit code plus parseable
# JSON only proves the CLI accepted the request, not that it filled.
FILLED_TRADE_STATUSES = {"MATCHED"}


def require_filled(response, action_desc):
    status = str(response.get("status") or "").upper()
    tx_hashes = response.get("transaction_hashes") or []
    if status not in FILLED_TRADE_STATUSES or not tx_hashes:
        raise RuntimeError(
            f"{action_desc} did not confirm an on-chain fill "
            f"(status={status or 'missing'}, transaction_hashes={tx_hashes})"
        )
    return response


def load_state():
    if not os.path.exists(config.STATE_PATH):
        return {"seen_trade_ids": [], "positions": {}, "source_positions": {}}
    with open(config.STATE_PATH) as f:
        state = json.load(f)
    state.setdefault("source_positions", {})
    return state


def save_state(state):
    with open(config.STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def append_log(event):
    log = []
    if os.path.exists(config.TRADE_LOG_PATH):
        with open(config.TRADE_LOG_PATH) as f:
            log = json.load(f)
    log.append(event)
    with open(config.TRADE_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)
    print(f"[{event['timestamp']}] {event['event_type']}: {event.get('market_slug', '')} {event.get('outcome', '')}")


def position_key(trader, market_slug, outcome):
    return f"{trader}|{market_slug}|{outcome}"


def process_trade(trade, positions, source_positions):
    trader = trade["user_address"]
    nickname = config.TRACKED_TRADERS.get(trader, trader)
    market_slug = trade.get("market_slug") or ""
    outcome = trade.get("outcome") or ""
    side = trade.get("side", "").upper()
    price = trade.get("price")
    source_size_usd = trade.get("size_usd")
    trade_id = trade.get("trade_id")

    base_event = {
        "timestamp": now_iso(),
        "source_trade_id": trade_id,
        "source_timestamp": trade.get("timestamp"),
        "trader_address": trader,
        "trader_nickname": nickname,
        "market_slug": market_slug,
        "market_title": trade.get("market_title") or "",
        "outcome": outcome,
        "side": side,
        "source_price": price,
        "source_size_usd": source_size_usd,
        "mode": "paper" if not config.LIVE_MODE else "live",
    }

    if not market_slug or not outcome:
        append_log({**base_event, "event_type": "unresolved_trade",
                    "reason": "market_slug/outcome missing from feed"})
        return

    if not price or price <= 0:
        append_log({**base_event, "event_type": "error",
                    "error": f"invalid price on source trade: {price}"})
        return

    key = position_key(trader, market_slug, outcome)

    if side == "BUY":
        source_shares = source_size_usd / price if source_size_usd else 0.0
        source_positions[key] = source_positions.get(key, 0.0) + source_shares

        our_shares = config.FIXED_TRADE_USD / price

        # Execute (if live) BEFORE touching our position ledger. If the live
        # order fails, is unmatched, or reverts on-chain, log it as a
        # failed_trade and bail out — we must NOT record a position we
        # never actually acquired.
        if config.LIVE_MODE:
            max_price = round(price * (1 + config.SLIPPAGE_TOLERANCE), 4)
            try:
                require_filled(run_bullpen_json([
                    "polymarket", "buy", market_slug, outcome, str(config.FIXED_TRADE_USD),
                    "--max-price", str(max_price), "--yes",
                ]), "live buy")
            except Exception as e:
                append_log({**base_event, "event_type": "failed_trade", "reason": str(e)})
                return

        pos = positions.get(key, {"shares": 0.0, "cost_basis_usd": 0.0, "avg_entry_price": 0.0})
        new_shares = pos["shares"] + our_shares
        new_cost = pos["cost_basis_usd"] + config.FIXED_TRADE_USD
        pos["avg_entry_price"] = new_cost / new_shares if new_shares else 0.0
        pos["shares"] = new_shares
        pos["cost_basis_usd"] = new_cost
        positions[key] = pos

        append_log({**base_event, "event_type": "paper_buy" if not config.LIVE_MODE else "live_buy",
                    "our_trade_usd": config.FIXED_TRADE_USD,
                    "our_shares": our_shares,
                    "position_shares_after": pos["shares"],
                    "position_avg_entry_price": pos["avg_entry_price"]})

    elif side == "SELL":
        pos = positions.get(key)
        if not pos or pos["shares"] <= 0:
            append_log({**base_event, "event_type": "skip_sell_no_position",
                        "reason": "we hold no simulated position in this market/outcome"})
            return

        source_shares_held = source_positions.get(key, 0.0)
        source_shares_sold = source_size_usd / price if source_size_usd else 0.0
        fraction_sold = 1.0 if source_shares_held <= 0 else min(1.0, source_shares_sold / source_shares_held)
        source_positions[key] = max(0.0, source_shares_held - source_shares_sold)

        shares_closed = pos["shares"] * fraction_sold
        cost_basis_closed = pos["cost_basis_usd"] * fraction_sold
        proceeds_usd = shares_closed * price
        pnl_usd = proceeds_usd - cost_basis_closed

        # Execute (if live) BEFORE touching our position ledger — same
        # reasoning as BUY: a failed, unmatched, or reverted sell must not
        # be recorded as closed.
        if config.LIVE_MODE:
            min_price = round(price * (1 - config.SLIPPAGE_TOLERANCE), 4)
            try:
                require_filled(run_bullpen_json([
                    "polymarket", "sell", market_slug, outcome, str(shares_closed),
                    "--min-price", str(min_price), "--yes",
                ]), "live sell")
            except Exception as e:
                append_log({**base_event, "event_type": "failed_trade", "reason": str(e)})
                return

        pos["shares"] -= shares_closed
        pos["cost_basis_usd"] -= cost_basis_closed
        if pos["shares"] <= 1e-9:
            del positions[key]
        else:
            positions[key] = pos

        append_log({**base_event, "event_type": "paper_sell" if not config.LIVE_MODE else "live_sell",
                    "fraction_sold": fraction_sold,
                    "our_shares_closed": shares_closed,
                    "our_shares_remaining": positions.get(key, {}).get("shares", 0.0),
                    "proceeds_usd": proceeds_usd,
                    "cost_basis_usd": cost_basis_closed,
                    "pnl_usd": pnl_usd})

    else:
        append_log({**base_event, "event_type": "error",
                    "error": f"unrecognized side: {side}"})


def main():
    print(f"Copybot starting — mode={'LIVE' if config.LIVE_MODE else 'PAPER'}, "
          f"tracking {len(config.TRACKED_TRADERS)} trader(s), "
          f"${config.FIXED_TRADE_USD}/trade, polling every {config.POLL_INTERVAL_SECONDS}s")

    state = load_state()
    seen_ids = deque(state["seen_trade_ids"], maxlen=2000)
    seen_set = set(seen_ids)
    positions = state["positions"]
    source_positions = state["source_positions"]

    bootstrap = not os.path.exists(config.STATE_PATH) or not state["seen_trade_ids"]
    if bootstrap:
        try:
            feed = run_bullpen_json(["tracker", "feed", "--limit", str(config.FEED_LIMIT)])
            trades = feed.get("trades", [])
            for t in trades:
                tid = t.get("trade_id")
                if tid:
                    seen_ids.append(tid)
                    seen_set.add(tid)
            append_log({"timestamp": now_iso(), "event_type": "bootstrap",
                        "note": f"baseline-skipped {len(trades)} pre-existing trades; "
                                f"only trades after this point will be copied"})
            save_state({"seen_trade_ids": list(seen_ids), "positions": positions,
                        "source_positions": source_positions})
        except Exception as e:
            append_log({"timestamp": now_iso(), "event_type": "error",
                        "error": f"bootstrap failed: {e}"})

    while True:
        try:
            feed = run_bullpen_json(["tracker", "feed", "--limit", str(config.FEED_LIMIT)])
            trades = feed.get("trades", [])
            new_trades = [t for t in trades if t.get("trade_id") not in seen_set]
            new_trades.sort(key=lambda t: t.get("timestamp", ""))

            for trade in new_trades:
                tid = trade.get("trade_id")
                if tid:
                    seen_ids.append(tid)
                    seen_set.add(tid)
                try:
                    process_trade(trade, positions, source_positions)
                except Exception as e:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "source_trade_id": tid,
                                "trader_address": trade.get("user_address"),
                                "error": str(e)})

            save_state({"seen_trade_ids": list(seen_ids), "positions": positions,
                        "source_positions": source_positions})

        except Exception as e:
            append_log({"timestamp": now_iso(), "event_type": "error",
                        "error": f"poll cycle failed: {e}"})

        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCopybot stopped.")
        sys.exit(0)
