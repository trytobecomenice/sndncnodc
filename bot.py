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
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone

import config


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# Set by the SIGTERM handler (dashboard.py's stop button sends SIGTERM).
# The main loop checks it between trades and inside long sweeps so we always
# finish the trade in flight, persist state, and exit cleanly — never die
# between a live fill and its save.
SHUTDOWN_REQUESTED = False


def _handle_sigterm(signum, frame):
    global SHUTDOWN_REQUESTED
    SHUTDOWN_REQUESTED = True
    print("SIGTERM received — finishing current work, saving state, then exiting.")


def atomic_write_json(path, obj):
    """All persistence goes through here: write to a sibling .tmp, fsync,
    then os.replace() so a crash/kill mid-write can never leave a truncated
    JSON file at `path` — the old complete file survives until the new one
    is fully on disk.
    """
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


class BullpenTimeoutError(RuntimeError):
    """The bullpen subprocess hit its timeout. For money-moving calls this
    is fundamentally different from a clean failure: the order MAY have
    executed on-chain even though we never saw the response — callers must
    log it as unknown_fill_state for manual reconciliation, never retry it.
    """


if config.PRIVATE_POLYGON_RPC_URL:
    # Applies to every bullpen subprocess call below (feed polling AND live
    # buy/sell) since it's a plain env var bullpen itself reads on startup —
    # see config.PRIVATE_POLYGON_RPC_URL for how this was verified.
    os.environ["BULLPEN_POLYGON_RPC_URL"] = config.PRIVATE_POLYGON_RPC_URL


def run_bullpen_json(args, retries=1, retry_delay=0.5):
    """retries=1 means "try once, no retry" — that's the default and MUST stay
    the default for any call that can move funds (buy/sell). Only read-only
    calls (tracker feed) should pass retries>1: a retried buy/sell risks
    double-executing a trade that actually filled but errored on the
    response leg.
    """
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return _run_bullpen_json_once(args)
        except RuntimeError as e:
            last_error = e
            if attempt < retries:
                time.sleep(retry_delay)
    raise last_error


def _run_bullpen_json_once(args):
    try:
        result = subprocess.run(
            ["bullpen"] + args + ["--output", "json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        # Raised as a RuntimeError subclass so the retry loop above still
        # retries read-only calls, while trade call sites can distinguish
        # "timed out, fill state unknown" from a clean rejection.
        raise BullpenTimeoutError(
            f"bullpen {' '.join(args)} timed out after 60s; "
            f"if this was a trade, the order MAY still have executed"
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


def check_spread_tolerance(market_slug, outcome, amount, side):
    """Risk 1 (spread/liquidity) pre-trade check. Calls `bullpen polymarket
    preview` for a fresh read of the CURRENT book (independent of the
    possibly-stale price the tracker feed reported for the source trade) and
    rejects the copy if the relative spread (spread / price) exceeds
    config.SPREAD_TOLERANCE, or if preview itself flags a liquidity warning.

    NOTE: preview's `spread` field is an ABSOLUTE price-tick spread (e.g.
    0.01), not a fraction of price -- dividing by price is required to get a
    comparable relative number across outcomes trading near $0.05 vs near
    $0.95. Verified empirically: a thin long-shot market and a liquid ~50/50
    market can report the identical absolute spread while differing by 10x+
    in relative terms.

    Fails safe: if preview itself errors (network/timeout/parse), this
    returns not-ok rather than skipping the check -- we'd rather miss a copy
    than fire one blind into an unknown book.
    """
    try:
        preview = run_bullpen_json([
            "polymarket", "preview", market_slug, outcome, str(amount),
            "--side", "buy" if side == "BUY" else "sell",
        ], retries=config.FEED_FETCH_RETRIES, retry_delay=config.FEED_FETCH_RETRY_DELAY_SECONDS)
    except Exception as e:
        return False, f"preview unavailable: {e}"

    price = preview.get("price")
    spread = preview.get("spread")
    if not price or price <= 0 or spread is None:
        return False, f"preview missing price/spread: {preview}"

    relative_spread = spread / price
    if relative_spread > config.SPREAD_TOLERANCE:
        return False, (
            f"relative spread {relative_spread:.1%} exceeds tolerance "
            f"{config.SPREAD_TOLERANCE:.0%} (price={price}, abs_spread={spread})"
        )

    if preview.get("warning"):
        return False, f"liquidity warning from preview: {preview['warning']}"

    return True, None


def get_market_prices(market_slug, outcome):
    """Fetch current prices via `bullpen polymarket price` for Trailing
    Take-Profit monitoring. Returns (best_bid, indicative_price, error).

    best_bid is what a market sell would actually receive RIGHT NOW — it is
    the only price a TTP exit may trigger on. indicative_price falls back
    through midpoint/last_trade and is used only to keep the peak
    high-water-mark fresh: last_trade in particular can be arbitrarily stale
    (dead books report a last_trade from days ago), so it must never fire a
    sell on its own.
    """
    try:
        data = run_bullpen_json(
            ["polymarket", "price", market_slug, outcome],
            retries=config.FEED_FETCH_RETRIES, retry_delay=config.FEED_FETCH_RETRY_DELAY_SECONDS,
        )
    except Exception as e:
        return None, None, f"price check failed: {e}"

    outcomes = data.get("outcomes") or []
    match = next((o for o in outcomes if str(o.get("outcome", "")).lower() == outcome.lower()), None)
    if match is None and outcomes:
        match = outcomes[0]
    if match is None:
        return None, None, f"no outcome data in price response: {data}"

    best_bid = match.get("best_bid")
    if not best_bid or best_bid <= 0:
        best_bid = None
    indicative = best_bid or match.get("midpoint") or match.get("last_trade")
    if not indicative or indicative <= 0:
        return None, None, f"no usable price in response: {match}"
    return best_bid, indicative, None


def extract_fill_price(response):
    """Best-effort read of the ACTUAL average fill price from a buy/sell
    response. No live fill has ever been captured by this bot yet (all
    history is paper), so the exact field name is unverified — we try the
    plausible candidates and return None if none is present, in which case
    the caller falls back to the source trade's price and logs the raw
    response so the first real fill documents the true shape.
    """
    for key in ("avg_price", "average_price", "fill_price", "executed_price", "price"):
        value = response.get(key)
        if isinstance(value, (int, float)) and 0 < value <= 1:
            return float(value)
    return None


def close_position_trailing_tp(key, trader, nickname, market_slug, outcome, positions,
                                current_price, peak_profit_pct, profit_pct,
                                trader_performance, muted_traders):
    """Executes the Trailing Take-Profit 'suck back' exit: a full-position
    market sell, independent of any source trade. Mirrors the SELL branch of
    process_trade (live-execute before touching the ledger, require_filled,
    circuit breaker on the realized pnl) but always closes 100% of the
    position rather than a fraction.
    """
    pos = positions[key]
    shares_closed = pos["shares"]
    cost_basis_closed = pos["cost_basis_usd"]

    base_event = {
        "timestamp": now_iso(),
        "trader_address": trader,
        "trader_nickname": nickname,
        "market_slug": market_slug,
        "outcome": outcome,
        "side": "SELL",
        "mode": "paper" if not config.LIVE_MODE else "live",
        "peak_profit_pct": peak_profit_pct,
        "profit_pct_at_trigger": profit_pct,
        "trigger_price": current_price,
    }

    effective_price = current_price
    if config.LIVE_MODE:
        spread_ok, spread_reason = check_spread_tolerance(market_slug, outcome, shares_closed, "SELL")
        if not spread_ok:
            append_log({**base_event, "event_type": "skip_wide_spread_trailing_tp", "reason": spread_reason})
            return

        min_price = round(current_price * (1 - config.SLIPPAGE_TOLERANCE), 4)
        try:
            response = require_filled(run_bullpen_json([
                "polymarket", "sell", market_slug, outcome, str(shares_closed),
                "--min-price", str(min_price), "--yes",
            ]), "live trailing-tp sell")
        except BullpenTimeoutError as e:
            append_log({**base_event, "event_type": "unknown_fill_state", "reason": str(e)})
            return
        except Exception as e:
            append_log({**base_event, "event_type": "failed_trade", "reason": str(e)})
            return

        fill_price = extract_fill_price(response)
        if fill_price:
            effective_price = fill_price
        else:
            base_event["fill_accounting"] = "fallback_best_bid"
            base_event["raw_trade_response"] = response

    proceeds_usd = shares_closed * effective_price
    pnl_usd = proceeds_usd - cost_basis_closed

    del positions[key]

    append_log({**base_event,
                "event_type": "paper_sell_trailing_tp" if not config.LIVE_MODE else "live_sell_trailing_tp",
                "our_shares_closed": shares_closed,
                "our_shares_remaining": 0.0,
                "proceeds_usd": proceeds_usd,
                "cost_basis_usd": cost_basis_closed,
                "pnl_usd": pnl_usd})

    check_circuit_breaker(trader, nickname, pnl_usd, trader_performance, muted_traders)


def check_trailing_take_profit(positions, trader_performance, muted_traders):
    """Trailing Take-Profit (TTP). For every active position: fetches a
    current price, updates the position's high-water-mark peak_profit_pct
    (persisted in state.json so it survives restarts), and once that peak
    has ever reached config.TRAILING_TP_ACTIVATION_PCT, triggers a full
    market-sell exit the moment current profit pulls back
    config.TRAILING_TP_DRAWDOWN_PCT (percentage points) off the peak.

    Time-gated to run at most once per TRAILING_TP_CHECK_INTERVAL_SECONDS
    (see main loop) -- a full sweep is one `price` subprocess per open
    position (measured >120s across 79 positions), far too slow to run
    every poll. Runs alongside (not instead of) the trade-copying logic and
    the circuit breaker -- this is what lets the bot exit a tracked-trader
    position on its own schedule, even if the trader hasn't sold yet.
    """
    for key in list(positions.keys()):
        if SHUTDOWN_REQUESTED:
            return
        pos = positions.get(key)
        if not pos or pos.get("shares", 0) <= 0:
            continue
        parts = key.split("|")
        if len(parts) != 3:
            continue
        trader, market_slug, outcome = parts
        nickname = config.TRACKED_TRADERS.get(trader, trader)

        entry_price = pos.get("avg_entry_price") or 0.0
        if entry_price <= 0:
            continue

        best_bid, indicative_price, err = get_market_prices(market_slug, outcome)
        if indicative_price is None:
            append_log({"timestamp": now_iso(), "event_type": "error",
                        "trader_address": trader, "market_slug": market_slug,
                        "outcome": outcome, "error": f"trailing_tp price check: {err}"})
            continue

        # Peak tracking may use the indicative price (midpoint/last_trade
        # fallback), but the EXIT decision below requires a live best_bid:
        # a stale last_trade must never be able to fire a sell.
        indicative_profit_pct = (indicative_price - entry_price) / entry_price
        peak_profit_pct = max(pos.get("peak_profit_pct", indicative_profit_pct), indicative_profit_pct)
        pos["peak_profit_pct"] = peak_profit_pct

        if peak_profit_pct < config.TRAILING_TP_ACTIVATION_PCT:
            continue  # trail not armed yet

        if best_bid is None:
            continue  # armed, but no live bid to evaluate (or exit into)

        profit_pct = (best_bid - entry_price) / entry_price
        drawdown = peak_profit_pct - profit_pct
        if drawdown < config.TRAILING_TP_DRAWDOWN_PCT:
            continue  # armed, but hasn't pulled back far enough yet

        print(f"Trailing TP triggered for {nickname} {market_slug} ({outcome}): "
              f"peak {peak_profit_pct:.1%} -> current {profit_pct:.1%}, selling full position.")
        close_position_trailing_tp(key, trader, nickname, market_slug, outcome, positions,
                                    best_bid, peak_profit_pct, profit_pct,
                                    trader_performance, muted_traders)


def _parse_market_resolution(market_info):
    """Returns {outcome_name_lower: final_price} for a resolved market, or
    None if the market isn't resolved yet. outcomes/outcomePrices arrive as
    real lists from the CLI today, but sibling fields (clobTokenIds) come as
    JSON-encoded strings, so tolerate both encodings.
    """
    if not market_info.get("closed") or str(market_info.get("umaResolutionStatus", "")).lower() != "resolved":
        return None
    outcomes = market_info.get("outcomes")
    prices = market_info.get("outcomePrices")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(prices, str):
        prices = json.loads(prices)
    if not outcomes or prices is None or len(outcomes) != len(prices):
        return None
    return {str(name).lower(): float(p) for name, p in zip(outcomes, prices)}


def run_closeout_sweep(positions, trader_performance, muted_traders):
    """Resolved-market sweep (hourly, see CLOSEOUT_INTERVAL_SECONDS).

    Source traders redeem resolved positions rather than selling them, and
    redemptions never appear as SELL trades in the feed — so without this
    sweep, our copy of a resolved position sits in state forever (and keeps
    getting pointlessly TTP price-checked). For every market we hold a
    position in: if it has resolved, book the final 0/1 outcome price as
    the close (position_resolved event, realized pnl fed to the circuit
    breaker like any other close) and drop the position.

    In LIVE mode, additionally runs `bullpen polymarket closeout` to
    actually redeem winners on-chain. That call moves real funds, so it is
    single-shot (never retried) and a timeout is logged as
    unknown_fill_state, matching the buy/sell doctrine.
    """
    resolution_cache = {}
    for key in list(positions.keys()):
        if SHUTDOWN_REQUESTED:
            return
        pos = positions.get(key)
        if not pos:
            continue
        parts = key.split("|")
        if len(parts) != 3:
            continue
        trader, market_slug, outcome = parts

        if market_slug not in resolution_cache:
            try:
                market_info = run_bullpen_json(
                    ["polymarket", "market", market_slug],
                    retries=config.FEED_FETCH_RETRIES,
                    retry_delay=config.FEED_FETCH_RETRY_DELAY_SECONDS,
                )
                resolution_cache[market_slug] = _parse_market_resolution(market_info)
            except Exception as e:
                append_log({"timestamp": now_iso(), "event_type": "error",
                            "market_slug": market_slug,
                            "error": f"closeout sweep market check failed: {e}"})
                resolution_cache[market_slug] = None

        final_prices = resolution_cache[market_slug]
        if final_prices is None:
            continue  # not resolved (or unreadable) — leave the position alone

        final_price = final_prices.get(outcome.lower())
        if final_price is None:
            append_log({"timestamp": now_iso(), "event_type": "error",
                        "market_slug": market_slug, "outcome": outcome,
                        "error": f"market resolved but outcome not found in {list(final_prices)}"})
            continue

        nickname = config.TRACKED_TRADERS.get(trader, trader)
        proceeds_usd = pos["shares"] * final_price
        pnl_usd = proceeds_usd - pos["cost_basis_usd"]
        del positions[key]

        append_log({"timestamp": now_iso(), "event_type": "position_resolved",
                    "trader_address": trader, "trader_nickname": nickname,
                    "market_slug": market_slug, "outcome": outcome,
                    "final_price": final_price,
                    "our_shares_closed": pos["shares"],
                    "proceeds_usd": proceeds_usd,
                    "cost_basis_usd": pos["cost_basis_usd"],
                    "pnl_usd": pnl_usd,
                    "mode": "paper" if not config.LIVE_MODE else "live"})
        check_circuit_breaker(trader, nickname, pnl_usd, trader_performance, muted_traders)

    if config.LIVE_MODE and not SHUTDOWN_REQUESTED:
        try:
            result = run_bullpen_json(["polymarket", "closeout", "--scope", "all", "--yes"])
            append_log({"timestamp": now_iso(), "event_type": "closeout_sweep",
                        "status": result.get("status"),
                        "summary": result.get("summary")})
        except BullpenTimeoutError as e:
            append_log({"timestamp": now_iso(), "event_type": "unknown_fill_state",
                        "reason": f"closeout redeem timed out; redemptions may have submitted: {e}"})
        except Exception as e:
            append_log({"timestamp": now_iso(), "event_type": "error",
                        "error": f"closeout redeem failed: {e}"})


def load_state():
    """Fail-CLOSED on a corrupt state file: state.json is the only record of
    what positions we hold, so starting fresh over a corrupt file would mean
    trading with amnesia while money may still be deployed in markets (and
    bootstrap would re-baseline, orphaning every open position). Restore
    from a state.json.bak-* backup instead.
    """
    if not os.path.exists(config.STATE_PATH):
        return {"seen_trade_ids": [], "positions": {}, "source_positions": {},
                "trader_performance": {}, "muted_traders": {}}
    try:
        with open(config.STATE_PATH) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(
            f"FATAL: {config.STATE_PATH} is unreadable ({e}).\n"
            f"Refusing to start with no position memory while positions may still be open.\n"
            f"Restore it from the most recent state.json.bak-* file (or {config.STATE_PATH}.tmp "
            f"if one exists from an interrupted write), then restart."
        )
    state.setdefault("source_positions", {})
    state.setdefault("trader_performance", {})
    state.setdefault("muted_traders", {})
    return state


def save_state(state):
    atomic_write_json(config.STATE_PATH, state)


# In-memory copy of the trade log, loaded once at startup — append_log used
# to re-read the whole (ever-growing) file from disk on every single event.
_log_cache = None


def _load_log_cache():
    global _log_cache
    if _log_cache is not None:
        return _log_cache
    _log_cache = []
    if os.path.exists(config.TRADE_LOG_PATH):
        try:
            with open(config.TRADE_LOG_PATH) as f:
                _log_cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Fail-OPEN here, unlike load_state: the log is an audit trail,
            # not trading state, and append_log gets called from exception
            # handlers — a corrupt log must not be able to crash-loop the
            # bot. Move the damaged file aside and start a fresh log that
            # records the fact.
            corrupt_path = (f"{config.TRADE_LOG_PATH}.corrupt-"
                            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}")
            os.replace(config.TRADE_LOG_PATH, corrupt_path)
            _log_cache = [{"timestamp": now_iso(), "event_type": "log_recovered",
                           "note": f"existing trade log was unreadable; moved aside to {corrupt_path}"}]
    return _log_cache


def append_log(event):
    log = _load_log_cache()
    log.append(event)
    atomic_write_json(config.TRADE_LOG_PATH, log)
    print(f"[{event['timestamp']}] {event['event_type']}: {event.get('market_slug', '')} {event.get('outcome', '')}")


def position_key(trader, market_slug, outcome):
    return f"{trader}|{market_slug}|{outcome}"


def find_cross_trader_position(positions, own_key, trader, market_slug, outcome):
    """Risk 3 (duplicate exposure) helper. Returns the address of another
    TRACKED trader that already holds an active (shares > 0) position in
    this exact market_slug+outcome, or None if no other trader currently
    holds one.

    Positions are keyed per-trader (position_key), so this has to scan all
    keys and match on the market_slug+outcome suffix rather than a direct
    lookup.
    """
    for other_key, other_pos in positions.items():
        if other_key == own_key:
            continue
        parts = other_key.split("|")
        if len(parts) != 3:
            continue
        other_trader, other_slug, other_outcome = parts
        if other_trader == trader:
            continue
        if other_slug == market_slug and other_outcome == outcome and other_pos.get("shares", 0) > 0:
            return other_trader
    return None


def check_circuit_breaker(trader, nickname, pnl_usd, trader_performance, muted_traders):
    """Circuit breaker (kill switch). Call this immediately after logging a
    realized pnl_usd on one of OUR OWN closed copy-trades (paper_sell /
    live_sell) for `trader` -- see config.MUTE_CONSECUTIVE_LOSS_STREAK etc.
    for the rules. Mutes are recorded into `muted_traders` (part of state.json)
    so they persist across restarts; already-muted traders are left alone.
    """
    perf = trader_performance.setdefault(trader, {"recent_results": [], "consecutive_losses": 0})
    is_win = pnl_usd > 0

    perf["recent_results"].append(is_win)
    perf["recent_results"] = perf["recent_results"][-config.MIN_TRADES_FOR_WIN_RATE_MUTE:]
    perf["consecutive_losses"] = 0 if is_win else perf["consecutive_losses"] + 1

    if trader in muted_traders:
        return

    reason = None
    if perf["consecutive_losses"] >= config.MUTE_CONSECUTIVE_LOSS_STREAK:
        reason = f"{perf['consecutive_losses']}-trade consecutive losing streak"
    elif len(perf["recent_results"]) >= config.MIN_TRADES_FOR_WIN_RATE_MUTE:
        win_rate = sum(perf["recent_results"]) / len(perf["recent_results"])
        if win_rate < config.MUTE_WIN_RATE_THRESHOLD:
            reason = (f"win rate {win_rate:.0%} over last {len(perf['recent_results'])} trades "
                      f"below {config.MUTE_WIN_RATE_THRESHOLD:.0%} threshold")

    if reason:
        muted_traders[trader] = {"muted_at": now_iso(), "reason": reason}
        print(f"Trader {nickname} muted: {reason} - Performance check failed.")


def process_trade(trade, positions, source_positions, trader_performance, muted_traders):
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

        # Circuit breaker (kill switch). Checked first -- blocks all new BUY
        # signals from this trader regardless of any other setting, but
        # source_positions tracking above still runs so a SELL against a
        # position we already hold from before the mute still computes the
        # right fraction_sold.
        if trader in muted_traders:
            append_log({**base_event, "event_type": "skip_muted_trader",
                        "reason": muted_traders[trader]["reason"]})
            return

        # Risk 3 (duplicate exposure) guard. Applies in BOTH paper and live
        # mode so paper runs stay representative of what live would do.
        other_trader = find_cross_trader_position(positions, key, trader, market_slug, outcome)
        if other_trader:
            other_nickname = config.TRACKED_TRADERS.get(other_trader, other_trader)
            print(f"Duplicate position detected {market_slug} ({outcome}), skipping.")
            append_log({**base_event, "event_type": "skip_duplicate_position",
                        "reason": f"active position in this outcome already held via "
                                  f"tracked trader {other_nickname} ({other_trader})"})
            return

        existing_pos = positions.get(key)
        if existing_pos and existing_pos.get("shares", 0) > 0:
            # Legacy positions recorded before this field existed have no
            # buy_count -- they exist only because of a real buy, so treat
            # them as having 1 already rather than 0.
            prior_buy_count = existing_pos.get("buy_count", 1)
            if prior_buy_count >= config.MAX_BUYS_PER_TRADER_OUTCOME:
                print(f"Duplicate position detected {market_slug} ({outcome}), skipping.")
                append_log({**base_event, "event_type": "skip_duplicate_position",
                            "reason": f"trader {nickname} already has {prior_buy_count} buy(s) "
                                      f"on this outcome (cap {config.MAX_BUYS_PER_TRADER_OUTCOME})"})
                return

        # Execute (if live) BEFORE touching our position ledger. If the live
        # order fails, is unmatched, or reverts on-chain, log it as a
        # failed_trade and bail out — we must NOT record a position we
        # never actually acquired.
        if config.LIVE_MODE:
            spread_ok, spread_reason = check_spread_tolerance(
                market_slug, outcome, config.FIXED_TRADE_USD, "BUY"
            )
            if not spread_ok:
                append_log({**base_event, "event_type": "skip_wide_spread", "reason": spread_reason})
                return

            max_price = round(price * (1 + config.SLIPPAGE_TOLERANCE), 4)
            try:
                response = require_filled(run_bullpen_json([
                    "polymarket", "buy", market_slug, outcome, str(config.FIXED_TRADE_USD),
                    "--max-price", str(max_price), "--yes",
                ]), "live buy")
            except BullpenTimeoutError as e:
                append_log({**base_event, "event_type": "unknown_fill_state", "reason": str(e)})
                return
            except Exception as e:
                append_log({**base_event, "event_type": "failed_trade", "reason": str(e)})
                return

            # Record ACTUAL fill economics when the response exposes them —
            # a fill can be up to SLIPPAGE_TOLERANCE worse than the source
            # price, and a ledger built on the source price drifts from
            # what we really hold (and skews TTP profit math).
            fill_price = extract_fill_price(response)
            if fill_price:
                our_shares = config.FIXED_TRADE_USD / fill_price
            else:
                base_event["fill_accounting"] = "fallback_source_price"
                base_event["raw_trade_response"] = response

        pos = positions.get(key) or {"shares": 0.0, "cost_basis_usd": 0.0, "avg_entry_price": 0.0,
                                      "buy_count": 0, "peak_profit_pct": 0.0}
        prior_buy_count = pos.get("buy_count", 1)  # legacy default, same assumption as the cap check above
        new_shares = pos["shares"] + our_shares
        new_cost = pos["cost_basis_usd"] + config.FIXED_TRADE_USD
        pos["avg_entry_price"] = new_cost / new_shares if new_shares else 0.0
        pos["shares"] = new_shares
        pos["cost_basis_usd"] = new_cost
        pos["buy_count"] = prior_buy_count + 1
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
        effective_price = price

        # Execute (if live) BEFORE touching our position ledger — same
        # reasoning as BUY: a failed, unmatched, or reverted sell must not
        # be recorded as closed.
        if config.LIVE_MODE:
            spread_ok, spread_reason = check_spread_tolerance(
                market_slug, outcome, shares_closed, "SELL"
            )
            if not spread_ok:
                append_log({**base_event, "event_type": "skip_wide_spread", "reason": spread_reason})
                return

            min_price = round(price * (1 - config.SLIPPAGE_TOLERANCE), 4)
            try:
                response = require_filled(run_bullpen_json([
                    "polymarket", "sell", market_slug, outcome, str(shares_closed),
                    "--min-price", str(min_price), "--yes",
                ]), "live sell")
            except BullpenTimeoutError as e:
                append_log({**base_event, "event_type": "unknown_fill_state", "reason": str(e)})
                return
            except Exception as e:
                append_log({**base_event, "event_type": "failed_trade", "reason": str(e)})
                return

            fill_price = extract_fill_price(response)
            if fill_price:
                effective_price = fill_price
            else:
                base_event["fill_accounting"] = "fallback_source_price"
                base_event["raw_trade_response"] = response

        proceeds_usd = shares_closed * effective_price
        pnl_usd = proceeds_usd - cost_basis_closed

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

        # The moment a trade result (realized pnl_usd) is logged, immediately
        # re-evaluate this trader's performance for the circuit breaker.
        check_circuit_breaker(trader, nickname, pnl_usd, trader_performance, muted_traders)

    else:
        append_log({**base_event, "event_type": "error",
                    "error": f"unrecognized side: {side}"})


def main():
    print(f"Copybot starting — mode={'LIVE' if config.LIVE_MODE else 'PAPER'}, "
          f"tracking {len(config.TRACKED_TRADERS)} trader(s), "
          f"${config.FIXED_TRADE_USD}/trade, polling every {config.POLL_INTERVAL_SECONDS}s")

    signal.signal(signal.SIGTERM, _handle_sigterm)

    state = load_state()
    seen_ids = deque(state["seen_trade_ids"], maxlen=2000)
    seen_set = set(seen_ids)
    positions = state["positions"]
    source_positions = state["source_positions"]
    trader_performance = state["trader_performance"]
    muted_traders = state["muted_traders"]

    def persist():
        save_state({"seen_trade_ids": list(seen_ids), "positions": positions,
                    "source_positions": source_positions, "trader_performance": trader_performance,
                    "muted_traders": muted_traders})

    bootstrap = not os.path.exists(config.STATE_PATH) or not state["seen_trade_ids"]
    if bootstrap:
        try:
            feed = run_bullpen_json(
                ["tracker", "feed", "--limit", str(config.FEED_LIMIT)],
                retries=config.FEED_FETCH_RETRIES,
                retry_delay=config.FEED_FETCH_RETRY_DELAY_SECONDS,
            )
            trades = feed.get("trades", [])
            for t in trades:
                tid = t.get("trade_id")
                if tid:
                    seen_ids.append(tid)
                    seen_set.add(tid)
            append_log({"timestamp": now_iso(), "event_type": "bootstrap",
                        "note": f"baseline-skipped {len(trades)} pre-existing trades; "
                                f"only trades after this point will be copied"})
            persist()
        except Exception as e:
            append_log({"timestamp": now_iso(), "event_type": "error",
                        "error": f"bootstrap failed: {e}"})

    last_ttp_sweep = 0.0
    last_closeout_sweep = 0.0

    while not SHUTDOWN_REQUESTED:
        try:
            feed = run_bullpen_json(
                ["tracker", "feed", "--limit", str(config.FEED_LIMIT)],
                retries=config.FEED_FETCH_RETRIES,
                retry_delay=config.FEED_FETCH_RETRY_DELAY_SECONDS,
            )
            trades = feed.get("trades", [])
            new_trades = [t for t in trades if t.get("trade_id") not in seen_set]
            new_trades.sort(key=lambda t: t.get("timestamp", ""))

            for trade in new_trades:
                if SHUTDOWN_REQUESTED:
                    break
                tid = trade.get("trade_id")
                if tid:
                    seen_ids.append(tid)
                    seen_set.add(tid)
                # Persist the seen-mark BEFORE the trade can execute: if we
                # crash mid-order, restart must treat this trade as already
                # handled (an unknown-outcome order gets reconciled manually
                # via the log) rather than re-firing it — a replayed live
                # buy is a double-spend of real funds.
                persist()
                try:
                    process_trade(trade, positions, source_positions, trader_performance, muted_traders)
                except Exception as e:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "source_trade_id": tid,
                                "trader_address": trade.get("user_address"),
                                "error": str(e)})
                # Persist again so a live fill is durable before we even
                # look at the next trade.
                persist()

            now = time.time()
            if not SHUTDOWN_REQUESTED and now - last_ttp_sweep >= config.TRAILING_TP_CHECK_INTERVAL_SECONDS:
                last_ttp_sweep = now
                try:
                    check_trailing_take_profit(positions, trader_performance, muted_traders)
                except Exception as e:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "error": f"trailing take-profit check failed: {e}"})
                persist()

            if not SHUTDOWN_REQUESTED and now - last_closeout_sweep >= config.CLOSEOUT_INTERVAL_SECONDS:
                last_closeout_sweep = now
                try:
                    run_closeout_sweep(positions, trader_performance, muted_traders)
                except Exception as e:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "error": f"closeout sweep failed: {e}"})
                persist()

        except Exception as e:
            append_log({"timestamp": now_iso(), "event_type": "error",
                        "error": f"poll cycle failed: {e}"})

        # Sleep in 1s slices so SIGTERM interrupts the wait promptly instead
        # of stalling shutdown for a full poll interval.
        deadline = time.time() + config.POLL_INTERVAL_SECONDS
        while not SHUTDOWN_REQUESTED and time.time() < deadline:
            time.sleep(1)

    persist()
    print("Copybot stopped cleanly (state saved).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCopybot stopped.")
        sys.exit(0)
