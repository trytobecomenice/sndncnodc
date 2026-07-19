#!/usr/bin/env python3
"""
Polymarket copytrading bot (paper mode).

Watches the active tracked-traders set (config.TRACKED_TRADERS_SOURCE — the
static config.TRACKED_TRADERS dict, or wallet_profile.status='track' rows
scored by packages/copy-trading/src/scoreWallets.ts, see db.get_tracked_traders)
via `bullpen tracker feed`, and for each new trade from a tracked address:
- BUY  -> open/add to a simulated FIXED_TRADE_USD position in the same market+outcome
- SELL -> proportional: if the trader sells N% of their (observed) position in that
          market+outcome, we sell N% of our own simulated position too

"Observed" position size is tracked from trades seen since this bot started (or since
bootstrap) — pre-existing holdings the trader had before we started watching are not
visible to us, so a sell that exceeds what we've observed is clamped to selling 100%
of our own position.

Everything (fills, skips, errors) is appended to the shared SQLite DB's
bot_event_log table (see db.py) so the Next.js dashboard (apps/dashboard)
can read it directly, alongside bot.py's own local dashboard.py.
"""

import json
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone

import config
from bullpen_client import (
    BullpenTimeoutError,
    extract_fill_price,
    require_filled,
    run_bullpen_json,
)


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

    OUTPUT: (ok, reason, executable_price) — executable_price is the fresh
    preview price this call already fetched, returned so callers (e.g.
    check_slippage_ceiling, added 2026-07-19) can reuse it instead of
    making a second preview call for the same market/outcome/amount. None
    when ok is False (no reliable price was read).
    """
    try:
        preview = run_bullpen_json([
            "polymarket", "preview", market_slug, outcome, str(amount),
            "--side", "buy" if side == "BUY" else "sell",
        ], retries=config.FEED_FETCH_RETRIES, retry_delay=config.FEED_FETCH_RETRY_DELAY_SECONDS)
    except Exception as e:
        return False, f"preview unavailable: {e}", None

    price = preview.get("price")
    spread = preview.get("spread")
    if not price or price <= 0 or spread is None:
        return False, f"preview missing price/spread: {preview}", None

    relative_spread = spread / price
    if relative_spread > config.SPREAD_TOLERANCE:
        return False, (
            f"relative spread {relative_spread:.1%} exceeds tolerance "
            f"{config.SPREAD_TOLERANCE:.0%} (price={price}, abs_spread={spread})"
        ), None

    if preview.get("warning"):
        return False, f"liquidity warning from preview: {preview['warning']}"

    return True, None, price


def check_slippage_ceiling(source_price, executable_price, side):
    """"Disciplined Taker" price ceiling, added 2026-07-19. Proactively
    ABORTS a live copy if the market has already moved past
    config.SLIPPAGE_TOLERANCE since the source trade — a genuine PRE-TRADE
    decision, distinct from the --max-price/--min-price already passed to
    the order itself in process_trade/close_position_trailing_tp (same
    SLIPPAGE_TOLERANCE constant, reused rather than duplicated — see its
    doc comment in config.py). Without this check, a moved market still
    gets an order SUBMITTED: --max-price/--min-price stops it from filling
    at a bad price, but the order can still rest unfilled on the book at
    the limit price rather than cleanly not happening, and could come back
    to fill hours later against a signal that's no longer live. This check
    means we never submit that order in the first place.

    Only called on BUYs in practice (see call site in process_trade) — a
    slippage ceiling on a SELL would block an exit, and per
    risk_manager.py's same principle, a risk layer that traps you in
    positions adds risk instead of removing it. Kept side-aware anyway so
    the direction is correct if a SELL use is ever deliberately added.

    INPUT:  source_price — the tracked trader's own fill price
            executable_price — the CURRENT price from a fresh preview
            side — "BUY" or "SELL"
    OUTPUT: (ok, reason) — reason is None when ok
    """
    if side == "BUY":
        adverse_pct = (executable_price - source_price) / source_price
    else:
        adverse_pct = (source_price - executable_price) / source_price

    if adverse_pct > config.SLIPPAGE_TOLERANCE:
        return False, (
            f"price moved {adverse_pct:.1%} against the source trade "
            f"(source={source_price}, executable={executable_price}), exceeds "
            f"{config.SLIPPAGE_TOLERANCE:.0%} slippage ceiling — aborting rather "
            f"than submit an order into an already-moved market"
        )
    return True, None


def compute_shortfall(side, source_price, executable_price, trade_usd=None, shares=None):
    """Pure implementation-shortfall math. Returns (shortfall_pct,
    shortfall_usd), both SIGNED so positive always means "our copy executes
    WORSE than the source trader's fill" regardless of side:

    - BUY:  we pay executable_price instead of source_price, so worse means
      executable > source. shortfall_usd is the exact extra cost of
      acquiring the same shares the source price would have bought with
      trade_usd: usd * (exec - source) / source.
    - SELL: we receive executable_price instead of source_price, so worse
      means executable < source. shortfall_usd = shares * (source - exec),
      the exact proceeds we'd give up on this close.

    Negative values (we'd fill BETTER than the source did — price moved our
    way in the copy delay) are possible and must be kept signed, not
    clamped: the whole point of this metric is the signed average over many
    copies, which is the strategy's structural cost of being last to every
    trade. Kept as its own function (rather than inline in the fetch
    helper) so this arithmetic is unit-testable without a bullpen call.
    """
    if side == "BUY":
        pct = (executable_price - source_price) / source_price
        usd = (trade_usd or 0.0) * pct
    else:
        pct = (source_price - executable_price) / source_price
        usd = (shares or 0.0) * (source_price - executable_price)
    return pct, usd


def measure_paper_shortfall(market_slug, outcome, side, preview_amount, source_price,
                             trade_usd=None, shares=None):
    """Implementation-shortfall measurement, PAPER MODE ONLY. Calls
    `bullpen polymarket preview` for the price this copy could ACTUALLY
    execute at right now, and returns a dict of extra fields to merge into
    the paper_buy/paper_sell event (they land in bot_event_log.payload_json
    via append_log — no schema change needed).

    MEASUREMENT ONLY, by design: the returned fields never feed back into
    the paper fill price, position ledger, or PnL — paper accounting stays
    on the source trade's price exactly as before, so historical paper
    stats remain comparable and the measurement can't perturb the thing it
    measures. (Live mode doesn't call this at all: the live path already
    previews for the spread check and records the true fill price, which IS
    the executable price.)

    Fails soft: any preview error returns a status field instead of
    raising — losing one measurement must never block or delay a copy
    beyond the preview call itself. preview_amount follows the same
    convention as check_spread_tolerance: USD for BUY, shares for SELL.
    """
    try:
        preview = run_bullpen_json([
            "polymarket", "preview", market_slug, outcome, str(preview_amount),
            "--side", "buy" if side == "BUY" else "sell",
        ], retries=config.FEED_FETCH_RETRIES, retry_delay=config.FEED_FETCH_RETRY_DELAY_SECONDS)
    except Exception as e:
        return {"shortfall_status": "preview_unavailable", "shortfall_error": str(e)}

    executable_price = preview.get("price")
    if not executable_price or executable_price <= 0:
        return {"shortfall_status": "no_executable_price", "shortfall_raw_preview": preview}

    pct, usd = compute_shortfall(side, source_price, executable_price,
                                 trade_usd=trade_usd, shares=shares)
    return {
        "shortfall_status": "ok",
        "executable_price": executable_price,
        "executable_spread": preview.get("spread"),
        "shortfall_pct": pct,
        "shortfall_usd": usd,
    }


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
        # No slippage-ceiling check here (contrast the BUY path in
        # process_trade) — this is an exit, and exits are never blocked
        # by a risk gate (see check_slippage_ceiling's docstring / risk_manager.py).
        spread_ok, spread_reason, _preview_price = check_spread_tolerance(market_slug, outcome, shares_closed, "SELL")
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


def check_trailing_take_profit(positions, trader_performance, muted_traders, tracked_by_lower):
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

    Returns {position_key: indicative_price} for every position it managed
    to price this sweep -- piggybacked on by the portfolio-equity /
    kill-switch evaluation in main() (risk_manager.compute_equity), since
    this sweep is the one place that already pays for a price fetch per
    open position. Positions closed by a TTP exit during the sweep are
    simply absent from `positions` afterwards; their realized pnl reaches
    equity via db.realized_pnl_total() instead, so nothing double-counts.
    """
    prices_by_key = {}
    for key in list(positions.keys()):
        if SHUTDOWN_REQUESTED:
            return prices_by_key
        pos = positions.get(key)
        if not pos or pos.get("shares", 0) <= 0:
            continue
        parts = key.split("|")
        if len(parts) != 3:
            continue
        trader, market_slug, outcome = parts
        nickname = nickname_for(trader, tracked_by_lower)

        entry_price = pos.get("avg_entry_price") or 0.0
        if entry_price <= 0:
            continue

        best_bid, indicative_price, err = get_market_prices(market_slug, outcome)
        if indicative_price is None:
            append_log({"timestamp": now_iso(), "event_type": "error",
                        "trader_address": trader, "market_slug": market_slug,
                        "outcome": outcome, "error": f"trailing_tp price check: {err}"})
            continue
        prices_by_key[key] = indicative_price

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
    return prices_by_key


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


# Consecutive closeout-sweep fetch failures per market_slug, used ONLY to
# throttle repeated error logging (added 2026-07-19). During a bullpen
# backend outage the hourly sweep re-logged an identical "market check
# failed" error for every held market every sweep — hundreds of rows of
# pure repetition in bot_event_log. Now: the FIRST failure per market is
# logged normally, repeats are suppressed until either the fetch succeeds
# again (counter resets silently) or every 24th consecutive failure
# (~daily at the hourly sweep rate) logs a reminder that includes the
# running count, so a market that stays unfetchable can't disappear from
# the log entirely. In-memory on purpose: a restart re-logging one error
# per still-failing market is acceptable, and persisting throttle state
# would be bookkeeping with no decision value.
_closeout_fetch_failures = {}


def run_closeout_sweep(positions, trader_performance, muted_traders, tracked_by_lower):
    """Resolved-market sweep (hourly, see CLOSEOUT_INTERVAL_SECONDS).

    Source traders redeem resolved positions rather than selling them, and
    redemptions never appear as SELL trades in the feed — so without this
    sweep, our copy of a resolved position sits in state forever (and keeps
    getting pointlessly TTP price-checked). For every market we hold a
    position in: if it has resolved, book the final 0/1 outcome price as
    the close (position_resolved event, realized pnl fed to the circuit
    breaker like any other close) and drop the position.

    A market whose lookup FAILS (vs. cleanly reporting "not resolved") is
    left alone and retried next sweep; repeated identical failures are
    throttled in the log — see _closeout_fetch_failures above.

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
                _closeout_fetch_failures.pop(market_slug, None)
            except Exception as e:
                failures = _closeout_fetch_failures.get(market_slug, 0) + 1
                _closeout_fetch_failures[market_slug] = failures
                if failures == 1 or failures % 24 == 0:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "market_slug": market_slug,
                                "consecutive_failures": failures,
                                "error": f"closeout sweep market check failed "
                                         f"({failures} consecutive sweep(s), repeats "
                                         f"throttled): {e}"})
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

        nickname = nickname_for(trader, tracked_by_lower)
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


# load_state/save_state/append_log now live in db.py, backed by the shared
# SQLite DB (see db.py's module docstring) instead of state.json/
# trades_log.json. Imported under these exact names so every call site below
# (process_trade, check_trailing_take_profit, check_circuit_breaker,
# run_closeout_sweep, main) is unchanged — only the storage backend moved.
#
# get_tracked_traders() is the switch config.TRACKED_TRADERS_SOURCE gates
# (see config.py and db.py's docstring on it): main() calls it once at
# startup and threads the result through as `tracked_by_lower` everywhere
# below that used to read config.TRACKED_TRADERS directly.
from db import (  # noqa: E402
    load_state, save_state, append_log, get_tracked_traders,
    get_risk_value, set_risk_value, load_market_events, save_market_event,
    realized_pnl_total,
)
import risk_manager  # noqa: E402


def resolve_market_event(market_slug):
    """market_slug -> parent event slug, via `bullpen polymarket market`
    (the same read-only call the closeout sweep already relies on). The
    response's `events` field is a list of event objects (verified live
    2026-07-18); sibling fields on this endpoint arrive JSON-string-encoded
    in some cases (see _parse_market_resolution), so tolerate both here too.
    Returns None on any failure — the caller fails CLOSED (skips the buy),
    it never guesses an event.
    """
    try:
        info = run_bullpen_json(
            ["polymarket", "market", market_slug],
            retries=config.FEED_FETCH_RETRIES,
            retry_delay=config.FEED_FETCH_RETRY_DELAY_SECONDS,
        )
    except Exception:
        return None
    events = info.get("events")
    if isinstance(events, str):
        try:
            events = json.loads(events)
        except json.JSONDecodeError:
            return None
    if isinstance(events, list) and events and isinstance(events[0], dict):
        event = events[0]
        slug = event.get("slug") or (str(event["id"]) if event.get("id") else None)
        return slug or None
    return None


def position_key(trader, market_slug, outcome):
    return f"{trader}|{market_slug}|{outcome}"


def nickname_for(trader, tracked_by_lower):
    """Case-insensitive nickname lookup against the active tracked-traders
    source (static config.py dict or wallet_profile, see
    config.TRACKED_TRADERS_SOURCE). `trader` is whatever casing the live
    tracker feed reported (checksummed mixed-case); tracked_by_lower's keys
    are always lowercase (built in main()) so this matches regardless of
    which casing the active source itself uses.
    """
    return tracked_by_lower.get(trader.lower(), (trader, trader))[1]


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
    for the rules. Mutes are recorded into `muted_traders` (persisted via
    db.py into wallet_profile) so they persist across restarts;
    already-muted traders are left alone.

    trader_performance/muted_traders are keyed by trader.lower() (not
    `trader` verbatim) — see db.py's module comment on why: they round-trip
    through wallet_profile.wallet_address, which is always lowercase, so
    keying them lowercase here too means no case-translation is needed on
    load, and a wallet muted under TRACKED_TRADERS_SOURCE="db" stays muted
    after a restart regardless of what casing wallet_profile happens to
    store it in.
    """
    key = trader.lower()
    perf = trader_performance.setdefault(key, {"recent_results": [], "consecutive_losses": 0})
    is_win = pnl_usd > 0

    perf["recent_results"].append(is_win)
    perf["recent_results"] = perf["recent_results"][-config.MIN_TRADES_FOR_WIN_RATE_MUTE:]
    perf["consecutive_losses"] = 0 if is_win else perf["consecutive_losses"] + 1

    if key in muted_traders:
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
        muted_traders[key] = {"muted_at": now_iso(), "reason": reason}
        print(f"Trader {nickname} muted: {reason} - Performance check failed.")


def process_trade(trade, positions, source_positions, trader_performance, muted_traders,
                  tracked_by_lower, risk_state):
    trader = trade["user_address"]
    nickname = nickname_for(trader, tracked_by_lower)
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
        if trader.lower() in muted_traders:
            append_log({**base_event, "event_type": "skip_muted_trader",
                        "reason": muted_traders[trader.lower()]["reason"]})
            return

        # Risk 3 (duplicate exposure) guard. Applies in BOTH paper and live
        # mode so paper runs stay representative of what live would do.
        other_trader = find_cross_trader_position(positions, key, trader, market_slug, outcome)
        if other_trader:
            other_nickname = nickname_for(other_trader, tracked_by_lower)
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

        # Portfolio-level risk gates (risk_manager.py) — BUYs only, both
        # paper and live, checked LAST so a risk skip in the log means "this
        # trade passed every other filter and was blocked purely on
        # portfolio limits". Event resolution fails CLOSED: if we can't
        # determine which event this market belongs to, we skip the copy
        # rather than let unattributable exposure bypass the per-event cap.
        event_slug = risk_state["market_to_event"].get(market_slug)
        if event_slug is None:
            event_slug = resolve_market_event(market_slug)
            if event_slug:
                risk_state["market_to_event"][market_slug] = event_slug
                save_market_event(market_slug, event_slug)
            else:
                append_log({**base_event, "event_type": "skip_risk_event_unresolved",
                            "reason": f"could not resolve parent event for {market_slug} — "
                                      f"failing closed rather than bypassing the per-event cap"})
                return

        risk_ok, risk_event_type, risk_reason = risk_manager.check_buy(
            positions, risk_state["market_to_event"], event_slug,
            config.FIXED_TRADE_USD, risk_state["kill_switch"],
        )
        if not risk_ok:
            append_log({**base_event, "event_type": risk_event_type, "reason": risk_reason})
            return

        # Execute (if live) BEFORE touching our position ledger. If the live
        # order fails, is unmatched, or reverts on-chain, log it as a
        # failed_trade and bail out — we must NOT record a position we
        # never actually acquired.
        if config.LIVE_MODE:
            spread_ok, spread_reason, executable_price = check_spread_tolerance(
                market_slug, outcome, config.FIXED_TRADE_USD, "BUY"
            )
            if not spread_ok:
                append_log({**base_event, "event_type": "skip_wide_spread", "reason": spread_reason})
                return

            # "Disciplined Taker" price ceiling — reuses the preview price
            # check_spread_tolerance just fetched, no extra network call.
            # BUY-only: exits are never blocked by a risk gate (see
            # check_slippage_ceiling's docstring).
            slippage_ok, slippage_reason = check_slippage_ceiling(price, executable_price, "BUY")
            if not slippage_ok:
                append_log({**base_event, "event_type": "skip_slippage_ceiling", "reason": slippage_reason})
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
        else:
            # Paper mode: record what this copy would ACTUALLY fill at right
            # now, next to the source price the paper ledger books it at.
            # Measurement only — our_shares below stays priced off the
            # source trade (see measure_paper_shortfall's docstring).
            base_event.update(measure_paper_shortfall(
                market_slug, outcome, "BUY", config.FIXED_TRADE_USD, price,
                trade_usd=config.FIXED_TRADE_USD,
            ))

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
            # No slippage-ceiling check here — this is an exit (see
            # check_slippage_ceiling's docstring / risk_manager.py's same
            # principle: never block a SELL, only BUYs).
            spread_ok, spread_reason, _preview_price = check_spread_tolerance(
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
        else:
            # Paper mode: same measurement as the BUY side — what would this
            # close actually receive right now vs the source price the paper
            # ledger books it at. effective_price below stays the source
            # price (see measure_paper_shortfall's docstring).
            base_event.update(measure_paper_shortfall(
                market_slug, outcome, "SELL", shares_closed, price,
                shares=shares_closed,
            ))

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
    # get_tracked_traders() is where config.TRACKED_TRADERS_SOURCE actually
    # takes effect (see config.py and db.py's docstring on it). Fetched once
    # here, not per-poll: picking up a new scan:wallets run requires a
    # restart, matching MIN_TRACKED_TRADERS' "fail loudly at startup" design
    # rather than a tracked list that can silently change mid-session.
    tracked_traders = get_tracked_traders()
    tracked_by_lower = {addr.lower(): (addr, nick) for addr, nick in tracked_traders.items()}

    # Portfolio-risk state (risk_manager.py): the latched kill switch and
    # equity high-water mark survive restarts via bot_risk_state, and the
    # market->event memo via bot_market_event. Mutated in place by
    # process_trade (new event resolutions) and the post-sweep equity
    # evaluation below; every mutation is persisted through db.py the
    # moment it happens.
    risk_state = {
        "kill_switch": get_risk_value("kill_switch"),
        "equity_hwm": get_risk_value("equity_hwm"),
        "market_to_event": load_market_events(),
    }

    print(f"Copybot starting — mode={'LIVE' if config.LIVE_MODE else 'PAPER'}, "
          f"source={config.TRACKED_TRADERS_SOURCE}, "
          f"tracking {len(tracked_traders)} trader(s), "
          f"${config.FIXED_TRADE_USD}/trade, polling every {config.POLL_INTERVAL_SECONDS}s")
    if risk_state["kill_switch"]:
        ks = risk_state["kill_switch"]
        print(f"WARNING: drawdown kill switch is LATCHED (since {ks.get('triggered_at')}: "
              f"{'; '.join(ks.get('reasons', []))}) — all new BUYs are halted. "
              f"Run reset_kill_switch.py after review to resume buying.")

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

    # load_state() (db.py) is now the sole source of truth regardless of
    # whether state.json exists on disk — bootstrap purely off whether it
    # returned any seen_trade_ids, not file presence (checking
    # os.path.exists(config.STATE_PATH) here would incorrectly re-bootstrap
    # on every restart once state.json is retired).
    bootstrap = not state["seen_trade_ids"]
    if bootstrap:
        try:
            feed = run_bullpen_json(
                ["tracker", "feed", "--limit", str(config.FEED_LIMIT)],
                retries=config.FEED_FETCH_RETRIES,
                retry_delay=config.FEED_FETCH_RETRY_DELAY_SECONDS,
                timeout=config.FEED_POLL_TIMEOUT_SECONDS,
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
                timeout=config.FEED_POLL_TIMEOUT_SECONDS,
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

                # `bullpen tracker feed` returns trades from whatever address
                # set is registered with bullpen's OWN tracker (external to
                # this repo, kept in sync out-of-band) — it is not itself
                # gated by config.TRACKED_TRADERS_SOURCE. This is the actual
                # enforcement point for that switch: bot.py, not bullpen,
                # decides whether a given trader is currently worth copying
                # (see docs/copy-trading/SAFETY.md's ownership-boundary section).
                trader_addr = trade.get("user_address") or ""
                if trader_addr.lower() not in tracked_by_lower:
                    append_log({"timestamp": now_iso(), "event_type": "skip_untracked_trader",
                                "source_trade_id": tid, "trader_address": trader_addr,
                                "reason": f"trader not in active tracked-traders source "
                                          f"(TRACKED_TRADERS_SOURCE={config.TRACKED_TRADERS_SOURCE!r})"})
                    persist()
                    continue

                try:
                    process_trade(trade, positions, source_positions, trader_performance,
                                  muted_traders, tracked_by_lower, risk_state)
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
                    prices_by_key = check_trailing_take_profit(
                        positions, trader_performance, muted_traders, tracked_by_lower)
                except Exception as e:
                    prices_by_key = None
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "error": f"trailing take-profit check failed: {e}"})
                persist()

                # Kill-switch evaluation, piggybacked on the sweep's price
                # fetches (see risk_manager module docstring for the equity
                # model and the ~5-min reaction-time tradeoff). Skipped when
                # the sweep itself failed — a broken price fetch must not
                # manufacture a phantom drawdown.
                if prices_by_key is not None and not SHUTDOWN_REQUESTED:
                    try:
                        equity = risk_manager.compute_equity(
                            positions, prices_by_key, realized_pnl_total())
                        new_hwm, triggers = risk_manager.evaluate_equity(
                            equity, risk_state["equity_hwm"])
                        if new_hwm != risk_state["equity_hwm"]:
                            risk_state["equity_hwm"] = new_hwm
                            set_risk_value("equity_hwm", new_hwm)
                        if triggers and not risk_state["kill_switch"]:
                            kill_switch = {"triggered_at": now_iso(), "reasons": triggers,
                                           "equity": equity, "hwm": new_hwm}
                            risk_state["kill_switch"] = kill_switch
                            set_risk_value("kill_switch", kill_switch)
                            append_log({"timestamp": now_iso(),
                                        "event_type": "risk_kill_switch_triggered",
                                        **kill_switch})
                            print(f"RISK KILL SWITCH TRIGGERED — halting all new BUYs: "
                                  f"{'; '.join(triggers)}. Run reset_kill_switch.py "
                                  f"after review to resume.")
                    except Exception as e:
                        append_log({"timestamp": now_iso(), "event_type": "error",
                                    "error": f"risk equity evaluation failed: {e}"})

            if not SHUTDOWN_REQUESTED and now - last_closeout_sweep >= config.CLOSEOUT_INTERVAL_SECONDS:
                last_closeout_sweep = now
                try:
                    run_closeout_sweep(positions, trader_performance, muted_traders, tracked_by_lower)
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
