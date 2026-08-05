#!/usr/bin/env python3
"""
Polymarket copytrading bot (paper mode).

Watches the active tracked-traders set (config.TRACKED_TRADERS_SOURCE — the
static config.TRACKED_TRADERS dict, or wallet_profile.status='track' rows
scored by packages/copy-trading/src/scoreWallets.ts, see db.get_tracked_traders)
via Polymarket's own public Data API, polled directly per wallet
(polymarket_data_api.py — see docs/copy-trading/RISK_MANAGEMENT.md Rule 14),
and for each new trade from a tracked address:
- BUY  -> open/add to a simulated confidence-weighted-size position in the same market+outcome
          (compute_trade_size_usd() — see config.py's BASE/MIN/MAX_TRADE_USD)
- SELL -> proportional: if the trader sells N% of their (observed) position in that
          market+outcome, we sell N% of our own simulated position too

CUTOVER 2026-07-22: this bot used to watch `bullpen tracker feed` instead. Replaced
entirely for tracking after finding, live, that bullpen's own tracker only sees
wallets registered on ITS OWN separate list — 10 of our 20 configured wallets had
silently never been added there, a structural 50% blind spot. The direct API reads
straight from config.py's address list, so that whole failure mode cannot recur.
Trade EXECUTION (live buy/sell) is UNCHANGED and still goes through `bullpen` —
this only ever replaced how the bot DETECTS a trade to copy, never how it places
one; see docs/copy-trading/SAFETY.md §6 for why private keys/execution stay on
bullpen regardless.

"Observed" position size is tracked from trades seen since this bot started (or since
bootstrap) — pre-existing holdings the trader had before we started watching are not
visible to us, so a sell that exceeds what we've observed is clamped to selling 100%
of our own position.

Everything (fills, skips, errors) is appended to the shared SQLite DB's
bot_event_log table (see db.py) so the Next.js dashboard (apps/dashboard)
can read it directly, alongside bot.py's own local dashboard.py.
"""

import json
import logging
import signal
import sys
import time
from collections import deque
from concurrent.futures import as_completed
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import config
from bullpen_client import (
    BullpenAuthError,
    BullpenTimeoutError,
    extract_fill_price,
    extract_filled_shares,
    extract_order_id,
    extract_order_status,
    require_filled,
    run_bullpen_json,
)
from polymarket_data_api import fetch_all_wallets_concurrent, make_persistent_executor
import polymarket_simulator
import oms_client
import telegram_alerts
from prometheus_client import Gauge, start_http_server


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# Console/status logging (2026-07-22, disk-exhaustion hardening) — replaces
# bare print() for the handful of operational status lines (startup banner,
# SIGTERM/shutdown notices, kill-switch alerts). RotatingFileHandler caps
# bot.out.log at config.LOG_MAX_BYTES, keeping config.LOG_BACKUP_COUNT old
# copies (bot.out.log.1 .. .5) instead of growing forever. StreamHandler is
# kept alongside it so running bot.py in a foreground terminal still shows
# output live — this doesn't change that, only what happens to the file.
# NOTE: bot_event_log (the SQLite table) is a COMPLETELY SEPARATE thing —
# written via direct SQL INSERT in db.append_log(), never through this
# logger — see config.py's EVENT_LOG_RETENTION_DAYS/prune_event_log() for
# that table's own (necessarily different) growth-bounding mechanism.
logger = logging.getLogger("copybot")
logger.setLevel(logging.INFO)
_file_handler = RotatingFileHandler(
    config.BOT_LOG_PATH, maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_BACKUP_COUNT,
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_file_handler)
logger.addHandler(logging.StreamHandler())

# Phase 1 observability (2026-07-31) — copybot_events_total (per event_type)
# lives in db.py, the one choke point every event already flows through;
# these three are portfolio-level snapshots only main()'s kill-switch
# evaluation block has the inputs for, so they're set there instead. See
# docs/copy-trading/SAFETY.md Sec.54.
METRIC_EQUITY_USD = Gauge("copybot_equity_usd", "Current portfolio equity (bankroll + realized + unrealized PnL)")
METRIC_KILL_SWITCH_ACTIVE = Gauge("copybot_kill_switch_active", "1 if the drawdown kill switch is latched, else 0")
METRIC_ENTRY_INTERLOCK_ACTIVE = Gauge(
    "copybot_entry_interlock_active",
    "1 if the recoverable execution-integrity BUY interlock is active, else 0",
)
METRIC_OPEN_POSITIONS = Gauge("copybot_open_positions", "Count of currently open bot_filtered positions")


# Set by the SIGTERM handler (dashboard.py's stop button sends SIGTERM).
# The main loop checks it between trades and inside long sweeps so we always
# finish the trade in flight, persist state, and exit cleanly — never die
# between a live fill and its save.
SHUTDOWN_REQUESTED = False


def _handle_sigterm(signum, frame):
    global SHUTDOWN_REQUESTED
    SHUTDOWN_REQUESTED = True
    logger.info("SIGTERM received — finishing current work, saving state, then exiting.")


def _evaluate_spread_gate(price, spread, insufficient_liquidity):
    """Shared Risk 1 (spread/liquidity) verdict logic, factored out
    2026-07-31 so the LIVE gate (check_spread_tolerance) and the PAPER
    retroactive flag (measure_paper_shortfall's would_have_passed_spread_gate)
    can never silently drift apart — both must agree on exactly what a live
    order would have been allowed to do against the same book read.

    NOTE: `spread` is an ABSOLUTE price-tick spread (e.g. 0.01), not a
    fraction of price -- dividing by price is required to get a comparable
    relative number across outcomes trading near $0.05 vs near $0.95.
    Verified empirically: a thin long-shot market and a liquid ~50/50 market
    can report the identical absolute spread while differing by 10x+ in
    relative terms.

    Returns (ok, reason) — reason is None when ok is True.
    """
    if not price or price <= 0 or spread is None:
        return False, f"preview missing price/spread (price={price}, spread={spread})"

    relative_spread = spread / price
    if relative_spread > config.SPREAD_TOLERANCE:
        return False, (
            f"relative spread {relative_spread:.1%} exceeds tolerance "
            f"{config.SPREAD_TOLERANCE:.0%} (price={price}, abs_spread={spread})"
        )

    if insufficient_liquidity:
        return False, "insufficient book depth to fill the requested amount"

    return True, None


def check_spread_tolerance(market_slug, outcome, amount, side):
    """Risk 1 (spread/liquidity) pre-trade check. Reads a fresh CLOB order
    book (independent of the possibly-stale price the tracker feed reported
    for the source trade) via polymarket_simulator.simulate_fill() and
    rejects the copy if the relative spread (spread / price) exceeds
    config.SPREAD_TOLERANCE, or if the visible book couldn't fill the
    requested size — see _evaluate_spread_gate() for the shared verdict
    logic.

    Fails safe: if the book read itself errors (network/timeout/parse), this
    returns not-ok rather than skipping the check -- we'd rather miss a copy
    than fire one blind into an unknown book.

    OUTPUT: (ok, reason, executable_price) — executable_price is the fresh
    price this call already fetched, returned so callers (e.g.
    check_slippage_ceiling, added 2026-07-19) can reuse it instead of
    making a second book read for the same market/outcome/amount. None
    when ok is False (no reliable price was read).

    CUTOVER 2026-07-31: replaced `bullpen polymarket preview` with a direct
    CLOB order-book read (polymarket_simulator.simulate_fill) — the same
    swap already made for paper-mode shortfall measurement on 2026-07-22,
    now applied to this live pre-trade gate too. Execution (buy/sell) is
    unaffected — still goes through bullpen (SAFETY.md §6): Gamma/CLOB are
    read-only public data, neither can place or sign an order.
    """
    try:
        preview = polymarket_simulator.simulate_fill(market_slug, outcome, side, amount)
    except Exception as e:
        return False, f"preview unavailable: {e}", None

    price = preview.get("price")
    ok, reason = _evaluate_spread_gate(price, preview.get("spread"), preview.get("insufficient_liquidity"))
    return ok, reason, (price if ok else None)


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


def compute_entry_slippage_ceiling_pct(live_edge_pct=None, protection_fraction=None):
    """Entry-side (BUY) Marketable Limit Order ceiling, 2026-07-26 -- same
    construction as compute_slippage_floor_price's exit-side floor, mirrored
    for symmetry: paying more in entry slippage than the strategy's own live
    edge is a guaranteed loser regardless of resolution outcome, so the
    ceiling is expressed as protection_fraction of the LIVE measured edge
    (db.compute_live_edge_pct()), not a constant pulled off the raw
    shortfall distribution alone (that distribution mixes wildly different
    liquidity regimes -- see RISK_MANAGEMENT.md Rule 33).

    live_edge_pct=None (too little closed-trade history to trust) falls
    back to config.ORDER_PEG_FALLBACK_EDGE_PCT, same conservative stand-in
    as the exit side. A negative live edge floors at 0.0 before scaling
    (never inverts into a negative allowance) -- also matching the exit
    side's compute_slippage_floor_price.

    Clamped to [config.SLIPPAGE_TOLERANCE, config.ENTRY_SLIPPAGE_CEILING_CAP_PCT]:
    never less protective than the pre-existing static tolerance, never so
    wide that a strong live edge licenses an absurd ceiling.
    """
    protection_fraction = (protection_fraction if protection_fraction is not None
                            else config.SLIPPAGE_PROTECTION_FRACTION)
    edge = live_edge_pct if live_edge_pct is not None else config.ORDER_PEG_FALLBACK_EDGE_PCT
    edge = max(edge, 0.0)
    raw = protection_fraction * edge
    return max(config.SLIPPAGE_TOLERANCE, min(config.ENTRY_SLIPPAGE_CEILING_CAP_PCT, raw))


def should_skip_category(wallet_score_entry, category):
    """Hard-skip check (2026-07-23) — a stricter, separate decision from
    compute_trade_size_usd()'s floor/ceiling sizing below. Returns True
    only when there is STATISTICALLY SIGNIFICANT evidence (not just a low
    raw score) that this wallet's mean realized PnL in `category` is
    negative: a one-sample t-test (pnl_t_stat, computed by
    scoreWalletCategories.ts) more extreme than -config.CATEGORY_SKIP_Z_CRITICAL
    (the standard one-tailed 95%-confidence critical value — see that
    constant's own comment for why this is the right test, not an invented
    threshold). A category with a low score but a WEAK/noisy statistical
    signal (small sample, high variance) does NOT get hard-skipped here —
    it still gets sized down via compute_trade_size_usd()'s normal
    floor, since "probably bad" and "confidently bad" call for different
    responses.
    """
    if not wallet_score_entry or not category:
        return False
    category_detail = wallet_score_entry.get("categories", {}).get(category)
    if not category_detail:
        return False
    t_stat = category_detail.get("pnl_t_stat")
    if t_stat is None:
        return False
    return t_stat <= -config.CATEGORY_SKIP_Z_CRITICAL


def compute_shrunk_win_rate(observed_win_rate, trade_count, market_price,
                             pseudo_count=None):
    """Empirical-Bayes/Beta-Binomial shrinkage: pulls `observed_win_rate`
    toward the market's OWN current price (its own implied probability),
    weighted by sample size — p_shrunk = (n*win_rate + k*price) / (n+k).
    See config.KELLY_SHRINKAGE_PSEUDO_COUNT's own comment for how k=25 was
    solved (not guessed), and why the market price — not a flat 0.5 — is
    the right shrinkage target: at n=0 (zero track record), p_shrunk =
    market_price exactly, which is what makes compute_kelly_fraction()
    correctly come out to a 0 edge for a wallet we know nothing about,
    rather than needing a special-cased fallback.

    Pure function, unit-testable without a DB call. trade_count<=0 degrades
    to "no evidence at all" (p_shrunk = market_price), same as n=0.
    """
    if pseudo_count is None:
        pseudo_count = config.KELLY_SHRINKAGE_PSEUDO_COUNT
    n = max(0, trade_count)
    return (n * observed_win_rate + pseudo_count * market_price) / (n + pseudo_count)


def compute_kelly_fraction(win_rate, market_price):
    """Kelly criterion fraction for a share bought at `market_price` that
    pays 1.0 if `win_rate`-probability-correct, 0 otherwise: net odds
    b = (1-price)/price, f* = win_rate - (1-win_rate)/b. Algebraically
    equivalent, divide-by-zero-safe form used here: f* = win_rate -
    (1-win_rate)*price/(1-price).

    A degenerate market_price (<=0 or >=1 — shouldn't reach this function
    given process_trade()'s own `price <= 0` guard upstream, but defended
    anyway rather than crashing on a market that's already resolved/
    invalid) returns 0.0 — no meaningful odds, no assumed edge, never
    guessed at. Pure function, unit-testable without a DB call.
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
    return win_rate - (1 - win_rate) * market_price / (1 - market_price)


def compute_trade_size_usd(wallet_score_entry, market_price, category=None):
    """Half-Kelly BUY size (2026-07-24, replacing the 2026-07-22 linear
    confidence ramp — see config.py's sizing comment for the full formula
    this implements).

    wallet_score_entry is one value from db.get_wallet_composite_scores():
    {"composite": float_or_None, "composite_win_rate": float_or_None,
    "composite_trade_count": int_or_None, "categories": {category:
    {"score":..., "win_rate": float_or_None, "trade_count": int_or_None,
    "pnl_t_stat":...}}}, or None if the wallet has no wallet_profile row at
    all (defended against though not expected in practice, since that
    function returns every row). `market_price` is the source trade's
    price — the same value process_trade() already validated is > 0 before
    reaching this function.

    Two-tier fallback, in order, picking a (win_rate, trade_count) PAIR
    rather than a single blended score:
      1. `category` is truthy AND wallet_score_entry["categories"][category]
         ["win_rate"] is not None -> use that category-specific win rate +
         trade count — the wallet's OWN reconstructed edge in THIS category
         specifically (see scoreWalletCategories.ts).
      2. Otherwise, fall back to the wallet's LIFETIME rolling win rate +
         trade count (composite_win_rate/composite_trade_count). A real,
         intentional consequence worth stating: this is usually a MUCH
         larger sample (often thousands of trades) than any single
         category, so composite-tier sizing gets barely shrunk by
         compute_shrunk_win_rate() at all — appropriate, since relying on
         a much deeper sample is the right call when there's no category-
         specific evidence, not a bug.
    Neither tier available (no win_rate anywhere) -> unchanged
    config.BASE_TRADE_USD, exactly the original no-evidence behavior. (A
    STRONGER statistical bar — should_skip_category() above — decides
    whether to skip the copy entirely before this function is even called;
    by the time this runs, that decision has already been made, and is
    UNCHANGED by this rewrite — it never depended on the sizing formula.)

    A non-positive half-Kelly fraction (win_rate <= market_price once
    shrunk, i.e. the model itself sees zero or negative edge) now returns
    0.0 — a skip signal for process_trade() to act on — rather than
    flooring at MIN_TRADE_USD (2026-07-28, found live: a wallet with a
    weak-but-not-yet-statistically-significant category track record was
    still trading a floor-sized copy on every signal despite Kelly reading
    negative on 8 of 9 same-day trades, since should_skip_category()'s bar
    is deliberately much stricter — "confidently bad", not "probably bad".
    This closes that gap at the sizing layer instead: "no track record, no
    assumed edge" already fell out of the shrinkage math at n=0 (Rule 25);
    this extends the same principle to "negative assumed edge, no trade"
    for n>0 too, rather than trading a nonzero amount on a signal the
    model's own math disagrees with).

    Otherwise, a positive half-Kelly fraction is clamped to (0, 1] and
    mapped into config.MIN_TRADE_USD..MAX_TRADE_USD (a deliberate scope
    choice — true bankroll-fraction Kelly would need total capital and
    would reshape the portfolio risk manager's exposure ceiling
    interaction; not done here) — stretched by
    wallet_score_entry["capital_multiplier"] (2026-07-28, rule_set v7,
    scoreWallets.ts's computeCapitalMultiplier, >= 1.0, defaults to 1.0/no
    change when missing or None) BEFORE the clamped fraction is mapped in.
    This is NOT a second sizing formula — it widens or narrows the RANGE
    this same Kelly fraction lands in, so a wallet with an exceptional
    risk-adjusted track record gets a bigger band to work with while the
    underlying Kelly math is completely unchanged. Deliberately does NOT
    scale config.BASE_TRADE_USD (the no-evidence fallback below) — a
    capital multiplier rewards PROVEN edge; it must never inflate the
    default used when there's no win-rate evidence at all, and it must
    never rescue a non-positive edge into a trade either. Pure function,
    unit-testable without a DB call.
    """
    if wallet_score_entry is None:
        return config.BASE_TRADE_USD

    win_rate = None
    trade_count = None
    if category:
        category_detail = wallet_score_entry.get("categories", {}).get(category)
        if category_detail and category_detail.get("win_rate") is not None:
            win_rate = category_detail["win_rate"]
            trade_count = category_detail.get("trade_count") or 0
    if win_rate is None and wallet_score_entry.get("composite_win_rate") is not None:
        win_rate = wallet_score_entry["composite_win_rate"]
        trade_count = wallet_score_entry.get("composite_trade_count") or 0

    if win_rate is None:
        return config.BASE_TRADE_USD

    shrunk_win_rate = compute_shrunk_win_rate(win_rate, trade_count, market_price)
    kelly_fraction = compute_kelly_fraction(shrunk_win_rate, market_price)
    half_kelly_fraction = kelly_fraction * config.KELLY_FRACTION_MULTIPLIER

    if half_kelly_fraction <= 0:
        return 0.0

    capital_multiplier = wallet_score_entry.get("capital_multiplier") or 1.0
    min_trade_usd = config.MIN_TRADE_USD * capital_multiplier
    max_trade_usd = config.MAX_TRADE_USD * capital_multiplier

    clamped = min(1.0, half_kelly_fraction)
    return min_trade_usd + (max_trade_usd - min_trade_usd) * clamped


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
    """Implementation-shortfall measurement, PAPER MODE ONLY. Simulates the
    fill this copy could ACTUALLY execute at right now by walking
    Polymarket's own public order book directly (polymarket_simulator.py,
    2026-07-22 — replaced `bullpen polymarket preview` here specifically so
    paper-mode simulation no longer depends on bullpen at all; execution
    itself is untouched, see that module's docstring), and returns a dict
    of extra fields to merge into the paper_buy/paper_sell event (they land
    in bot_event_log.payload_json via append_log — no schema change needed).

    MEASUREMENT ONLY, by design: the returned fields never feed back into
    the paper fill price, position ledger, or PnL — paper accounting stays
    on the source trade's price exactly as before, so historical paper
    stats remain comparable and the measurement can't perturb the thing it
    measures. (Live mode doesn't call this at all: the live path already
    previews for the spread check and records the true fill price, which IS
    the executable price.)

    `would_have_passed_spread_gate` (added 2026-07-31, on every return path
    below): the SAME verdict check_spread_tolerance() would have made
    against this exact book read (via the shared _evaluate_spread_gate()),
    computed but NOT enforced — a paper trade is never skipped or altered
    because of it. Lets paper stats be filtered/segmented after the fact
    into "would live have taken this copy" without narrowing what gets
    recorded now, which would break comparability with pre-2026-07-31 paper
    history the same way actually gating paper trades would have.

    Fails soft: any simulation error returns a status field instead of
    raising — losing one measurement must never block or delay a copy
    beyond the simulation call itself. preview_amount follows the same
    convention as check_spread_tolerance: USD for BUY, shares for SELL.
    """
    try:
        preview = polymarket_simulator.simulate_fill(market_slug, outcome, side, preview_amount)
    except Exception as e:
        return {"shortfall_status": "preview_unavailable", "shortfall_error": str(e),
                "would_have_passed_spread_gate": False,
                "spread_gate_reason": f"preview unavailable: {e}"}

    executable_price = preview.get("price")
    would_pass, gate_reason = _evaluate_spread_gate(
        executable_price, preview.get("spread"), preview.get("insufficient_liquidity"))

    if not executable_price or executable_price <= 0:
        result = {"shortfall_status": "no_executable_price", "shortfall_raw_preview": preview,
                  "would_have_passed_spread_gate": would_pass}
        if not would_pass:
            result["spread_gate_reason"] = gate_reason
        return result

    pct, usd = compute_shortfall(side, source_price, executable_price,
                                 trade_usd=trade_usd, shares=shares)

    # Added 2026-07-22 (docs/copy-trading/RISK_MANAGEMENT.md Rule 10 addendum): `preview` already
    # reports these two costs explicitly (confirmed live: a real preview showed trading_fee=1.2 on
    # a $100 trade) — shortfall_pct/shortfall_usd above measure ONLY price slippage and are left
    # exactly as before (their existing meaning isn't being redefined, in case anything downstream
    # already depends on it). These are NEW, separate fields capturing the other real cost
    # component that was always available in the response but never logged. total_cost_usd is the
    # genuine all-in figure: slippage + trading fee + network fee.
    trading_fee_usd = preview.get("trading_fee") or 0.0
    network_fee_usd = preview.get("network_fee") or 0.0
    total_cost_usd = usd + trading_fee_usd + network_fee_usd

    result = {
        "shortfall_status": "ok",
        "executable_price": executable_price,
        "executable_spread": preview.get("spread"),
        "shortfall_pct": pct,
        "shortfall_usd": usd,
        "trading_fee_usd": trading_fee_usd,
        "network_fee_usd": network_fee_usd,
        "total_cost_usd": total_cost_usd,
        "would_have_passed_spread_gate": would_pass,
    }
    if not would_pass:
        result["spread_gate_reason"] = gate_reason
    # Added 2026-07-22 (order-book simulator cutover): the book didn't have enough visible depth
    # to fill preview_amount in full — executable_price/fees above are still real, just for a
    # smaller fill than requested. Surfaced so this isn't silently indistinguishable from a
    # fully-filled measurement.
    if preview.get("insufficient_liquidity"):
        result["insufficient_liquidity"] = True
        result["shares_filled"] = preview.get("shares_filled")
    return result


def get_market_prices(market_slug, outcome, ignore_staleness=False):
    """Fetch current prices directly from Polymarket's own public order book
    (polymarket_simulator.py, 2026-07-22 — migrated off `bullpen polymarket
    price` for the same reason tracking and paper simulation already moved:
    a read-only price check needs no custody/signing, so it runs through
    our own reused connections instead of bullpen) for Trailing Take-Profit
    monitoring. Returns (best_bid, indicative_price, error).

    best_bid is what a market sell would actually receive RIGHT NOW — it is
    the only price a TTP exit may trigger on. indicative_price falls back
    through midpoint/last_trade and is used only to keep the peak
    high-water-mark fresh: last_trade in particular can be arbitrarily stale
    (dead books report a last_trade from days ago, a real market condition,
    not a quirk of any particular data source), so it must never fire a
    sell on its own.

    ignore_staleness (default False) threads straight through to
    fetch_order_book_for_outcome/fetch_order_book — see those docstrings.
    Only sweep_zombie_positions/close_position_zombie_dump should ever pass
    True; the normal TTP sweep must keep refusing stale books.

    Stale-tolerant peak-tracking fallback (2026-07-31): a chronically thin
    market's book can genuinely stay >MAX_BOOK_AGE_SECONDS old for hours
    (confirmed live: 74 distinct positions, 100+ failures/day each) — before
    this fix, EVERY such check returned (None, None, err), which meant
    peak_profit_pct froze forever (pos["last_priced_at"] is only updated on
    a successful read) and these positions could never be actively managed
    by TTP at all, structurally forcing them into the held-to-resolution
    bucket the 2026-07-25 sizing report found is net-NEGATIVE EV. Now: a
    fresh-required call (ignore_staleness=False) that fails ONLY because
    the book is stale (StaleOrderBookError, not a broken/delisted market)
    retries once with ignore_staleness=True and returns an indicative price
    from that stale book for peak-tracking — but ALWAYS with best_bid=None,
    so check_trailing_take_profit()'s existing `if best_bid is None:
    continue` (an exit can only fire on a live bid) needs no changes at all
    to keep this safe. midpoint is preferred over last_trade_price here for
    the same reason the fresh path prefers it — a single old trade is less
    trustworthy than the book's current two-sided quote, stale or not.
    """
    try:
        _, book = polymarket_simulator.fetch_order_book_for_outcome(
            market_slug, outcome, ignore_staleness=ignore_staleness
        )
    except polymarket_simulator.StaleOrderBookError as e:
        if ignore_staleness:
            return None, None, f"price check failed: {e}"  # should be unreachable
        try:
            _, book = polymarket_simulator.fetch_order_book_for_outcome(
                market_slug, outcome, ignore_staleness=True
            )
        except Exception as e2:
            return None, None, f"price check failed: {e2}"
        midpoint = None
        if book["bids"] and book["asks"]:
            midpoint = (book["bids"][0][0] + book["asks"][0][0]) / 2
        indicative = midpoint or book.get("last_trade_price")
        if not indicative or indicative <= 0:
            return None, None, f"no usable stale-fallback price for {market_slug}/{outcome}: {book}"
        return None, indicative, None
    except Exception as e:
        return None, None, f"price check failed: {e}"

    best_bid = book["bids"][0][0] if book["bids"] else None
    if not best_bid or best_bid <= 0:
        best_bid = None

    midpoint = None
    if book["bids"] and book["asks"]:
        midpoint = (book["bids"][0][0] + book["asks"][0][0]) / 2

    indicative = best_bid or midpoint or book.get("last_trade_price")
    if not indicative or indicative <= 0:
        return None, None, f"no usable price for {market_slug}/{outcome}: {book}"
    return best_bid, indicative, None


def get_market_ask_price(market_slug, outcome):
    """Buy-side counterpart to get_market_prices() (which returns bid/
    indicative — the right pair for a SELL-side TTP check). sweep_pending_
    executions() needs the price a BUY would actually pay right now, and
    reuses the same no-custody, no-bullpen-call order-book read rather than
    a live `bullpen preview` call every sweep cycle — that heavier,
    auth-bearing check still runs (inside _execute_buy, when config.LIVE_MODE
    is on) at the one moment it actually matters: the instant an order is
    about to fire, not on every monitoring pass while it's still resting.
    Returns (best_ask, error).
    """
    try:
        _, book = polymarket_simulator.fetch_order_book_for_outcome(market_slug, outcome)
    except Exception as e:
        return None, f"price check failed: {e}"

    best_ask = book["asks"][0][0] if book["asks"] else None
    if not best_ask or best_ask <= 0:
        return None, f"no usable ask for {market_slug}/{outcome}: {book}"
    return best_ask, None


def get_market_bid_ask(market_slug, outcome):
    """One order-book read returning (best_bid, best_ask, error) — the pair
    sweep_pending_exit_orders() needs to compute mid/spread_ratio for
    Priority 3's liquidity-regime check, without the two separate calls
    get_market_prices()+get_market_ask_price() would cost."""
    try:
        _, book = polymarket_simulator.fetch_order_book_for_outcome(market_slug, outcome)
    except Exception as e:
        return None, None, f"price check failed: {e}"

    best_bid = book["bids"][0][0] if book["bids"] else None
    best_ask = book["asks"][0][0] if book["asks"] else None
    if not best_bid or best_bid <= 0 or not best_ask or best_ask <= 0:
        return None, None, f"no usable bid/ask for {market_slug}/{outcome}: {book}"
    return best_bid, best_ask, None


def fetch_book_depth_usd(market_slug, outcome):
    """Visible ASK-side depth in USD (sum of price*size across every level
    the book returns) for risk_manager.depth_capped_trade_size_usd() —
    the side a BUY consumes. Added 2026-07-28 (Depth-Aware Trade Sizing).

    Returns None on ANY fetch failure (network/timeout/parse/stale-book —
    same failure modes fetch_order_book already raises on), never a guessed
    or zero value — the caller (process_trade) treats None as "skip the
    depth clamp for this trade," same fail-open posture as every other
    price-fetch helper in this file (get_market_ask_price/get_market_bid_ask
    above).
    """
    try:
        _, book = polymarket_simulator.fetch_order_book_for_outcome(market_slug, outcome)
    except Exception:
        return None

    asks = book.get("asks") or []
    if not asks:
        return None
    return sum(price * size for price, size in asks)


def compute_slippage_floor_price(mid_price, live_edge_pct, protection_fraction=None):
    """Priority 3's slippage floor for a patient SELL: the worst (lowest)
    price we'll accept, expressed as protection_fraction of the LIVE
    measured edge (db.compute_live_edge_pct()) below mid — not a fixed
    formula with unexplained constants. live_edge_pct=None (too little
    closed-trade history to trust yet) falls back to
    config.ORDER_PEG_FALLBACK_EDGE_PCT, a deliberately conservative
    stand-in rather than assuming a favorable edge that hasn't been earned.
    """
    protection_fraction = (protection_fraction if protection_fraction is not None
                            else config.SLIPPAGE_PROTECTION_FRACTION)
    edge = live_edge_pct if live_edge_pct is not None else config.ORDER_PEG_FALLBACK_EDGE_PCT
    edge = max(edge, 0.0)  # a currently-negative measured edge still floors at mid, never inverts
    max_slippage_pct = protection_fraction * edge
    return mid_price * (1 - max_slippage_pct)


def compute_reprice_interval_seconds(spread_ratio):
    """Bifurcated liquidity-regime check. spread_ratio=None (a price check
    failed) fails toward the SLOWER, more patient interval — the safer
    default when the liquidity regime itself is unknown, rather than
    assuming a tight, high-liquidity book."""
    if spread_ratio is None or spread_ratio > config.ORDER_PEG_LOW_LIQUIDITY_SPREAD_RATIO_THRESHOLD:
        return config.ORDER_PEG_LOW_LIQUIDITY_INTERVAL_SECONDS
    return config.ORDER_PEG_HIGH_LIQUIDITY_INTERVAL_SECONDS


def compute_pegged_price(init_price, floor_price, elapsed_seconds, reprice_interval_seconds,
                          tick_decrement=None):
    """P(t) = max(P_init - floor(t/Δt)*δ, P_floor) — ticks_elapsed is a
    whole-number count of completed reprice intervals, so the price only
    steps down at each Δt boundary, not continuously."""
    tick_decrement = tick_decrement if tick_decrement is not None else config.ORDER_PEG_TICK_DECREMENT
    ticks_elapsed = int(elapsed_seconds // reprice_interval_seconds)
    decayed = init_price - ticks_elapsed * tick_decrement
    return max(decayed, floor_price)


def compute_anchor_price(existing_anchor, observed_price):
    """The 'no high-chasing' rule as one line of pure math: a tracked
    wallet's own price never RAISES our anchor once set — only a strictly
    lower observed price can move it. Applied both when a pending_execution
    is first created (existing_anchor=None) and when a later BUY from the
    same wallet in the same market+outcome arrives while one is already
    resting (ratchets the anchor down if the new VWAP is lower, otherwise
    leaves it untouched).
    """
    if existing_anchor is None:
        return observed_price
    return min(existing_anchor, observed_price)


def compute_rebound_threshold(lowest_seen_price, tick_floor=None, pct=None, tick_size=None):
    """How far price must climb off lowest_seen_price before a dip counts as
    'confirmed reversal, not still falling.' Hybrid of a fixed tick floor
    and a percentage of the local low — see config.LIMIT_ORDER_REBOUND_PCT's
    docstring for the reasoning: a pure percentage is meaningless noise on a
    $0.05 longshot (5% is a quarter of one tick) and a pure tick count is
    inconsistently strict across Polymarket's $0.01-$0.99 range (2 ticks is
    a ~4% move at $0.50 but a ~100% move at $0.02). Taking the max of both
    components means whichever one is more meaningful at the current price
    level is the one that actually governs, with no branching needed at the
    call site.
    """
    tick_floor = tick_floor if tick_floor is not None else config.LIMIT_ORDER_REBOUND_TICK_FLOOR
    pct = pct if pct is not None else config.LIMIT_ORDER_REBOUND_PCT
    tick_size = tick_size if tick_size is not None else config.LIMIT_ORDER_TICK_SIZE
    return max(tick_floor * tick_size, pct * lowest_seen_price)


def has_rebounded(current_price, lowest_seen_price):
    """True once current_price has climbed at least compute_rebound_threshold()
    above the local minimum seen so far. Never true before a dip has even
    been recorded (lowest_seen_price is None) — callers must check that
    separately, this function assumes a real local low already exists."""
    return current_price >= lowest_seen_price + compute_rebound_threshold(lowest_seen_price)


def whale_still_holding(current_whale_shares, whale_shares_at_creation, min_fraction=None):
    """Guard 4 ('is the whale still holding'): the tracked wallet must still
    hold at least min_fraction of the shares it held when this
    pending_execution was created. whale_shares_at_creation is None or <= 0
    on a wallet's very first observed buy in this market+outcome (no prior
    baseline exists to compare against) — trivially passes, since there's
    nothing to have sold FROM yet. A real reduction below the baseline
    (rather than a small trim) is what actually blocks the fire — see
    config.LIMIT_ORDER_WHALE_HOLD_MIN_FRACTION's docstring for why 0.5, not
    'any' reduction at all.
    """
    min_fraction = (min_fraction if min_fraction is not None
                     else config.LIMIT_ORDER_WHALE_HOLD_MIN_FRACTION)
    if whale_shares_at_creation is None or whale_shares_at_creation <= 0:
        return True
    return current_whale_shares >= whale_shares_at_creation * min_fraction


def close_position_trailing_tp(key, trader, nickname, market_slug, outcome, positions,
                                current_price, peak_profit_pct, profit_pct,
                                trader_performance, muted_traders, close_reason="trailing_tp"):
    """Executes a full-position market sell exit, independent of any source
    trade -- originally built for the Trailing Take-Profit 'suck back' exit
    (the default close_reason), now shared by every "independently decided
    to exit this position" mechanism (2026-08-01: also Time-Decay Loss Cut)
    since the actual execution/ledger/circuit-breaker logic is identical,
    only the LABEL differs. Mirrors the SELL branch of process_trade
    (live-execute before touching the ledger, require_filled, circuit
    breaker on the realized pnl) but always closes 100% of the position
    rather than a fraction.

    close_reason drives the event_type ("paper_sell_{close_reason}"/
    "live_sell_{close_reason}") and is threaded into the patient-exit-peg
    and shadow-patient-exit calls below too -- deliberately kept distinct
    per mechanism (not folded into a generic "we_exited" label) so future
    close-reason-mix analysis (the exact query that found the Time-Decay
    Loss Cut problem in the first place) can still tell them apart.
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
        # Priority 3 opt-in (2026-07-26, config.ENABLE_PATIENT_EXIT_PEGGING,
        # default False): try a patient, decaying limit-sell before falling
        # back to the immediate market sell below. If start_patient_exit()
        # places a real resting order, `positions[key]` is deliberately
        # left UNTOUCHED here — sweep_pending_exit_orders() owns closing it
        # out later, on either a real fill or the guaranteed market-sell
        # fallback (never left open indefinitely). If placement fails for
        # any reason, it returns None and this falls straight through to
        # the existing immediate-sell path, unchanged — the opt-in can
        # never make an exit LESS likely to happen than it already was.
        if config.ENABLE_PATIENT_EXIT_PEGGING:
            pending_id = start_patient_exit(key, trader, market_slug, outcome, shares_closed,
                                             close_reason=close_reason)
            if pending_id is not None:
                return

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

    # Paper-only comparison measurement (2026-08-01) -- fires alongside
    # every real trailing-TP exit above, regardless of paper/live or
    # whether the LIVE_MODE patient-peg path was taken, to measure what a
    # resting peg would have captured against effective_price (the price
    # the real exit actually got). Never touches positions/PnL; wrapped in
    # try/except deliberately -- this is a best-effort measurement riding
    # alongside the real, safety-critical exit below, and must never be
    # able to block or crash it.
    try:
        start_shadow_patient_exit(key, trader, market_slug, outcome, shares_closed,
                                   immediate_exit_price=effective_price, close_reason=close_reason)
    except Exception as e:
        logger.warning(f"start_shadow_patient_exit failed for {market_slug}/{outcome}: {e}")

    del positions[key]

    append_log({**base_event,
                "event_type": f"paper_sell_{close_reason}" if not config.LIVE_MODE else f"live_sell_{close_reason}",
                "our_shares_closed": shares_closed,
                "our_shares_remaining": 0.0,
                "proceeds_usd": proceeds_usd,
                "cost_basis_usd": cost_basis_closed,
                "pnl_usd": pnl_usd})

    check_circuit_breaker(trader, nickname, pnl_usd, cost_basis_closed, trader_performance, muted_traders)


# Consecutive "market lookup itself is broken" failures per position key,
# used ONLY to throttle repeated zombie-unresolvable log lines (added
# 2026-07-27) — exact same reasoning/shape as _closeout_fetch_failures
# above run_closeout_sweep. In-memory only: this sweep runs every
# ZOMBIE_SWEEP_INTERVAL_SECONDS (hours), so re-logging once per still-broken
# position after a restart is a non-issue.
_zombie_unresolvable_failures = {}


def close_position_zombie_dump(key, trader, nickname, market_slug, outcome, positions,
                                indicative_price, trader_performance, muted_traders, age_hours):
    """Forced full-position exit for a "zombie" position — see
    sweep_zombie_positions' docstring for what qualifies. Deliberately a
    SEPARATE function from close_position_trailing_tp rather than a shared
    one with a flag: reusing it would mean sharing check_spread_tolerance
    too, which would very plausibly REJECT exactly the trade this function
    exists to force through (a wide spread is likely why the market went
    stale in the first place — gating the escape hatch on the same check
    that let it get stuck defeats the point). No patient-exit-pegging
    either: a zombie dump needs to leave now, not rest a limit order.

    Still not reckless: config.ZOMBIE_EXIT_MAX_SLIPPAGE (25%, vs. the
    normal 5% SLIPPAGE_TOLERANCE) is a real price floor, just a much looser
    one than ordinary exits get — "aggressive," never "sell at any price."
    On a failed/timed-out sell, the position is deliberately left in
    `positions` untouched (mirrors close_position_trailing_tp) so the next
    zombie sweep just tries again.
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
        "age_hours": age_hours,
        "trigger_price": indicative_price,
    }

    effective_price = indicative_price
    if config.LIVE_MODE:
        min_price = round(indicative_price * (1 - config.ZOMBIE_EXIT_MAX_SLIPPAGE), 4)
        try:
            response = require_filled(run_bullpen_json([
                "polymarket", "sell", market_slug, outcome, str(shares_closed),
                "--min-price", str(min_price), "--yes",
            ]), "live zombie-dump sell")
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
    _zombie_unresolvable_failures.pop(key, None)

    append_log({**base_event,
                "event_type": "paper_sell_zombie_dump" if not config.LIVE_MODE else "live_sell_zombie_dump",
                "our_shares_closed": shares_closed,
                "our_shares_remaining": 0.0,
                "proceeds_usd": proceeds_usd,
                "cost_basis_usd": cost_basis_closed,
                "pnl_usd": pnl_usd})

    check_circuit_breaker(trader, nickname, pnl_usd, cost_basis_closed, trader_performance, muted_traders)


def sweep_zombie_positions(positions, trader_performance, muted_traders, tracked_by_lower):
    """"Zombie position" dump-exit fallback (2026-07-27). Found live: a
    small subset of held markets never pass check_trailing_take_profit's
    staleness check — either genuinely dead (near-zero real order flow, so
    Polymarket's own book timestamp never refreshes inside MAX_BOOK_AGE_
    SECONDS) or outright delisted/renamed ("no market found for slug").
    Waiting doesn't fix either case, so a position stuck long enough just
    traps capital indefinitely. This sweep is the deliberately-separate
    escape hatch — see close_position_zombie_dump's docstring for why it's
    not folded into the normal TTP closer, and config.py's "Zombie
    position" comment block for why the thresholds below are NOT reused
    from elsewhere (MAX_BOOK_AGE_SECONDS/SLIPPAGE_TOLERANCE stay tight for
    everything else; only this narrow, rare path is loosened).

    Runs on its own interval (config.ZOMBIE_SWEEP_INTERVAL_SECONDS, hours —
    see main()), separate from and much less frequent than the 5-minute TTP
    sweep. Detection itself is free (reads pos["last_priced_at"], already
    updated by check_trailing_take_profit's own successful reads — no new
    network call); only an ALREADY-eligible position triggers one.

    config.ENABLE_ZOMBIE_POSITION_DUMP gates ONLY the actual forced sell —
    detection and both log paths below (the throttled "unresolvable" alert,
    and a "would dump" dry-run line while the flag is off) always run, so
    the very first rollout is pure observability before any real position
    gets force-closed this way.
    """
    now = time.time()
    for key in list(positions.keys()):
        if SHUTDOWN_REQUESTED:
            return
        pos = positions.get(key)
        if not pos:
            continue
        last_priced = pos.get("last_priced_at")
        if last_priced is None:
            continue  # no reference point to judge age from — skip, don't guess
        age = now - last_priced
        if age < config.ZOMBIE_POSITION_THRESHOLD_SECONDS:
            continue

        parts = key.split("|")
        if len(parts) != 3:
            continue
        trader, market_slug, outcome = parts
        nickname = nickname_for(trader, tracked_by_lower)
        age_hours = round(age / 3600, 1)

        best_bid, indicative_price, err = get_market_prices(market_slug, outcome, ignore_staleness=True)
        if indicative_price is None:
            # Market lookup itself is broken (delisted/renamed/etc), not
            # just stale — a forced sell can't fix a market that doesn't
            # resolve at all. Throttled, same pattern as
            # _closeout_fetch_failures, rather than retrying a doomed
            # operation every sweep.
            failures = _zombie_unresolvable_failures.get(key, 0) + 1
            _zombie_unresolvable_failures[key] = failures
            if failures == 1 or failures % config.ZOMBIE_UNRESOLVABLE_LOG_EVERY == 0:
                append_log({"timestamp": now_iso(), "event_type": "error",
                            "trader_address": trader, "market_slug": market_slug,
                            "outcome": outcome, "age_hours": age_hours,
                            "consecutive_failures": failures,
                            "error": f"zombie position unresolvable ({age_hours}h unpriced, "
                                     f"{failures} consecutive sweep(s), repeats throttled): {err} "
                                     f"— needs manual review, no automated exit is possible"})
            continue

        _zombie_unresolvable_failures.pop(key, None)

        if not config.ENABLE_ZOMBIE_POSITION_DUMP:
            append_log({"timestamp": now_iso(), "event_type": "zombie_position_would_dump",
                        "trader_address": trader, "market_slug": market_slug, "outcome": outcome,
                        "age_hours": age_hours, "trigger_price": indicative_price,
                        "reason": f"no successful price check in {age_hours}h — would force-close "
                                  f"at ~{indicative_price} if ENABLE_ZOMBIE_POSITION_DUMP were on"})
            continue

        close_position_zombie_dump(key, trader, nickname, market_slug, outcome, positions,
                                    indicative_price, trader_performance, muted_traders, age_hours)


def check_trailing_take_profit(positions, trader_performance, muted_traders, tracked_by_lower,
                                market_to_end_date=None, executor=None):
    """Trailing Take-Profit (TTP). For every active position: fetches a
    current price, updates the position's high-water-mark peak_profit_pct
    (persisted in state.json so it survives restarts), and once that peak
    has ever reached the activation threshold, triggers a full market-sell
    exit the moment current profit pulls back config.TRAILING_TP_DRAWDOWN_PCT
    (percentage points) off the peak.

    Time-gated to run at most once per TRAILING_TP_CHECK_INTERVAL_SECONDS
    (see main loop) -- historically one price fetch per open position, done
    SEQUENTIALLY (measured >120s across 79 positions back when this used a
    bullpen subprocess per position), far too slow to run every poll.

    Parallel price fetch (2026-07-31): found live investigating why fast-
    resolving markets (esports matches that can go from a large peak to
    resolved within minutes) kept missing their exit entirely -- the fix for
    THAT specific finding was theta-decay TP (Rule 31 addendum), but the
    5-minute SWEEP INTERVAL itself was also identified as a real, separate
    bottleneck, and it was dominated by this function's own positions being
    priced one at a time. `executor` (pass bot.py's ONE long-lived
    ThreadPoolExecutor, same pattern already used for wallet-trade fetching
    -- see polymarket_data_api.make_persistent_executor()) parallelizes the
    slow network I/O (get_market_prices()/resolve_market_end_date()) across
    all eligible positions at once. The DECISION + MUTATION phase (updating
    peak_profit_pct, arming/firing an exit, calling close_position_
    trailing_tp() which can move real money in LIVE_MODE) stays strictly
    SEQUENTIAL in the main thread afterward, using the already-fetched
    results -- concurrency only ever touches the read-only network fetch,
    never `positions`/`trader_performance`/`muted_traders` or an actual
    sell decision. executor=None (default) falls back to the original
    one-at-a-time behavior, byte-for-byte -- no caller is forced to change.

    Activation threshold ("Priority 4", 2026-07-26): config.
    ENABLE_THETA_DECAY_TP_ACTIVATION=False (default) keeps the original
    static config.TRAILING_TP_ACTIVATION_PCT (50%) unchanged. Enabled, the
    threshold scales down (compute_theta_decay_activation_pct()) as a
    position's market approaches its own resolution date, resolved once
    per market via resolve_market_end_date() and cached in
    market_to_end_date (in-memory, persisted to bot_market_event.
    end_date_iso) -- a market whose end date can't be resolved falls back
    to the static threshold, never guesses. When parallelized, this fetch
    is deduplicated by market_slug first (several positions can share one
    market) so two positions on the same market never trigger two redundant
    concurrent lookups of the same end date.

    Returns {position_key: indicative_price} for every position it managed
    to price this sweep -- piggybacked on by the portfolio-equity /
    kill-switch evaluation in main() (risk_manager.compute_equity), since
    this sweep is the one place that already pays for a price fetch per
    open position. Positions closed by a TTP exit during the sweep are
    simply absent from `positions` afterwards; their realized pnl reaches
    equity via db.realized_pnl_total() instead, so nothing double-counts.
    """
    market_to_end_date = market_to_end_date if market_to_end_date is not None else {}
    prices_by_key = {}

    # Phase 1 (cheap, no I/O): collect every position actually eligible for
    # a price check this sweep -- unchanged filtering logic, just separated
    # out so phase 2 knows exactly what to fetch before fetching anything.
    # NOTE: SHUTDOWN_REQUESTED is now only re-checked in phase 3, not
    # between individual fetches like the old one-at-a-time loop did -- a
    # shutdown mid-sweep now lets the (read-only, side-effect-free) fetch
    # phase finish before stopping, rather than truncating it. In practice
    # this should make graceful shutdown FASTER, not slower: parallel
    # fetches finish well before the old sequential loop would have.
    eligible = []
    for key in list(positions.keys()):
        pos = positions.get(key)
        if not pos or pos.get("shares", 0) <= 0:
            continue
        parts = key.split("|")
        if len(parts) != 3:
            continue
        trader, market_slug, outcome = parts
        entry_price = pos.get("avg_entry_price") or 0.0
        if entry_price <= 0:
            continue
        eligible.append((key, trader, market_slug, outcome, entry_price))

    if not eligible:
        return prices_by_key

    # Phase 2 (the slow part, now optionally parallel): one get_market_prices()
    # call per eligible position.
    def _fetch_price(item):
        _, _, market_slug, outcome, _ = item
        return get_market_prices(market_slug, outcome)

    price_results = {}
    if executor is not None:
        futures = {executor.submit(_fetch_price, item): item for item in eligible}
        for future in as_completed(futures):
            key = futures[future][0]
            try:
                price_results[key] = future.result()
            except Exception as e:
                price_results[key] = (None, None, str(e))
    else:
        for item in eligible:
            price_results[item[0]] = _fetch_price(item)

    # Phase 2b: end-date resolution, deduplicated by market_slug (several
    # positions can share one market) -- same optional-parallel shape as
    # phase 2, but keyed on market_slug instead of position key. Shared by
    # theta-decay TP activation AND Time-Decay Loss Cut (2026-08-01) -- both
    # need days-remaining, so either flag alone is enough to trigger this.
    if config.ENABLE_THETA_DECAY_TP_ACTIVATION or config.ENABLE_TIME_DECAY_LOSS_CUT:
        missing_market_slugs = {
            market_slug for _, _, market_slug, _, _ in eligible
            if market_slug not in market_to_end_date
        }
        if missing_market_slugs:
            if executor is not None:
                futures = {executor.submit(resolve_market_end_date, ms): ms
                           for ms in missing_market_slugs}
                for future in as_completed(futures):
                    market_slug = futures[future]
                    try:
                        end_date_iso = future.result()
                    except Exception:
                        end_date_iso = None
                    if end_date_iso:
                        market_to_end_date[market_slug] = end_date_iso
                        save_market_end_date(market_slug, end_date_iso)
            else:
                for market_slug in missing_market_slugs:
                    end_date_iso = resolve_market_end_date(market_slug)
                    if end_date_iso:
                        market_to_end_date[market_slug] = end_date_iso
                        save_market_end_date(market_slug, end_date_iso)

    # Phase 3 (sequential, in the main thread only): decide + act, using the
    # results already gathered above -- identical logic/order to the
    # original one-at-a-time loop, just reading from price_results instead
    # of fetching inline.
    for key, trader, market_slug, outcome, entry_price in eligible:
        if SHUTDOWN_REQUESTED:
            return prices_by_key
        pos = positions.get(key)
        if not pos:
            continue  # closed by an earlier iteration this same sweep
        nickname = nickname_for(trader, tracked_by_lower)

        best_bid, indicative_price, err = price_results.get(key, (None, None, "no price result"))
        if indicative_price is None:
            append_log({"timestamp": now_iso(), "event_type": "error",
                        "trader_address": trader, "market_slug": market_slug,
                        "outcome": outcome, "error": f"trailing_tp price check: {err}"})
            continue
        prices_by_key[key] = indicative_price
        # Zombie-position detection (2026-07-27) piggybacks on this existing
        # sweep's successful price reads rather than its own network call —
        # see sweep_zombie_positions' docstring. Any successful read resets
        # the clock; only a position that NEVER gets here for
        # ZOMBIE_POSITION_THRESHOLD_SECONDS becomes eligible.
        pos["last_priced_at"] = time.time()

        # Peak tracking may use the indicative price (midpoint/last_trade
        # fallback), but the EXIT decision below requires a live best_bid:
        # a stale last_trade must never be able to fire a sell.
        indicative_profit_pct = (indicative_price - entry_price) / entry_price
        peak_profit_pct = max(pos.get("peak_profit_pct", indicative_profit_pct), indicative_profit_pct)
        pos["peak_profit_pct"] = peak_profit_pct

        days_remaining = None
        if config.ENABLE_THETA_DECAY_TP_ACTIVATION or config.ENABLE_TIME_DECAY_LOSS_CUT:
            end_date_iso = market_to_end_date.get(market_slug)
            days_remaining = compute_days_remaining(end_date_iso)

        # Time-Decay Loss Cut (2026-08-01) — evaluated BEFORE the TTP
        # activation gate below, deliberately: it targets exactly the
        # positions that gate will never let through (peak_profit_pct below
        # any reasonable activation threshold), so the two are naturally
        # mutually exclusive, not competing for the same trigger. Requires
        # a live best_bid to exit into, same discipline as the TTP branch.
        if config.ENABLE_TIME_DECAY_LOSS_CUT and best_bid is not None:
            lifespan_fraction = compute_lifespan_fraction_remaining(pos.get("opened_at"), days_remaining)
            if is_time_decay_loss_cut_eligible(peak_profit_pct, lifespan_fraction):
                logger.info(f"Time-Decay Loss Cut triggered for {nickname} {market_slug} ({outcome}): "
                            f"peak {peak_profit_pct:.1%} never armed, "
                            f"{lifespan_fraction:.1%} of lifespan remaining, cutting now.")
                loss_cut_profit_pct = (best_bid - entry_price) / entry_price
                close_position_trailing_tp(key, trader, nickname, market_slug, outcome, positions,
                                            best_bid, peak_profit_pct, loss_cut_profit_pct,
                                            trader_performance, muted_traders,
                                            close_reason="time_decay_loss_cut")
                continue

        activation_pct = config.TRAILING_TP_ACTIVATION_PCT
        if config.ENABLE_THETA_DECAY_TP_ACTIVATION:
            activation_pct = compute_theta_decay_activation_pct(days_remaining)

        if peak_profit_pct < activation_pct:
            continue  # trail not armed yet

        if best_bid is None:
            continue  # armed, but no live bid to evaluate (or exit into)

        profit_pct = (best_bid - entry_price) / entry_price
        drawdown = peak_profit_pct - profit_pct
        if drawdown < config.TRAILING_TP_DRAWDOWN_PCT:
            continue  # armed, but hasn't pulled back far enough yet

        logger.info(f"Trailing TP triggered for {nickname} {market_slug} ({outcome}): "
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


def _trade_age_seconds(trade):
    """Seconds between now and trade["timestamp"] (the
    "%Y-%m-%d %H:%M:%S UTC" string polymarket_data_api.py formats — see
    format_trade()), or None if missing/unparseable. Used by process_trade()
    to decide whether a BUY signal is old enough to warrant an extra
    resolved-market sanity check (config.STALE_TRADE_RESOLUTION_CHECK_SECONDS)
    before opening a position.
    """
    ts = trade.get("timestamp")
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _market_already_resolved(market_slug, risk_state):
    """True if market_slug has already settled — checks risk_state's
    in-memory "resolved_markets" cache first (free after the first hit for
    a given market this process), only falling back to a live metadata
    fetch + _parse_market_resolution() the first time a market is asked
    about. A fetch failure is NOT treated as "resolved" (fails open here,
    deliberately the opposite of resolve_market_event()'s fail-closed
    doctrine): this is a belt-and-suspenders sanity check on an otherwise-
    legitimate-looking signal, not itself a primary risk gate, and a
    transient metadata-fetch error must not block a genuinely fresh buy.
    """
    resolved_markets = risk_state.setdefault("resolved_markets", set())
    if market_slug in resolved_markets:
        return True
    try:
        market_info = polymarket_simulator.fetch_market_metadata(market_slug)
    except Exception:
        return False
    if _parse_market_resolution(market_info) is not None:
        resolved_markets.add(market_slug)
        return True
    return False


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
                market_info = polymarket_simulator.fetch_market_metadata(market_slug)
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
        check_circuit_breaker(trader, nickname, pnl_usd, pos["cost_basis_usd"], trader_performance, muted_traders)

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


def run_shadow_closeout_sweep(shadow_positions, shadow_kind):
    """Resolve shadow ledgers when source redemptions never emit a SELL."""
    if shadow_kind not in ("rehab", "challenger"):
        raise ValueError(f"unsupported shadow kind: {shadow_kind!r}")
    resolution_cache = {}
    for key in list(shadow_positions):
        if SHUTDOWN_REQUESTED:
            return
        pos = shadow_positions.get(key)
        parts = key.split("|")
        if not pos or len(parts) != 3:
            continue
        trader, market_slug, outcome = parts
        if market_slug not in resolution_cache:
            try:
                resolution_cache[market_slug] = _parse_market_resolution(
                    polymarket_simulator.fetch_market_metadata(market_slug)
                )
            except Exception:
                resolution_cache[market_slug] = None
        final_prices = resolution_cache[market_slug]
        if final_prices is None or outcome.lower() not in final_prices:
            continue
        final_price = final_prices[outcome.lower()]
        proceeds_usd = pos["shares"] * final_price
        pnl_usd = proceeds_usd - pos["cost_basis_usd"]
        del shadow_positions[key]
        append_log({"timestamp": now_iso(),
                    "event_type": f"shadow_{shadow_kind}_resolved",
                    "trader_address": trader, "market_slug": market_slug,
                    "outcome": outcome, "final_price": final_price,
                    "our_shares_closed": pos["shares"],
                    "our_shares_remaining": 0.0,
                    "proceeds_usd": proceeds_usd,
                    "cost_basis_usd": pos["cost_basis_usd"],
                    "pnl_usd": pnl_usd, "mode": "paper"})


def sweep_pending_executions(positions, source_positions, source_cost_basis,
                              tracked_by_lower, risk_state, wallet_scores, wallet_ev_stats=None):
    """Rule 29's TTL/rebound/whale-hold sweep (2026-07-24) — runs every poll
    cycle (see main()), one 'pending' row at a time, oldest first. For each:

      1. Guard 4 ('is the whale still holding') — checked FIRST, every
         cycle, not only right before a fire: whale_still_holding() catches
         a dump as soon as it's observed rather than waiting for a rebound
         to even be in progress.
      2. Track the local minimum (lowest_seen_price) once price has dipped
         below anchor_price — the whale's own VWAP cost basis, which this
         order will never buy above (no-high-chasing).
      3. Fire _execute_buy() the moment has_rebounded() confirms a reversal
         off that minimum — UNLESS the rebound has already carried price
         past anchor_price, in which case firing would itself violate the
         no-chase rule, so the order is invalidated instead of chased.
      4. Expire on TTL if none of the above resolved it in time.

    Price is read via get_market_ask_price() — a plain order-book read, no
    bullpen/auth call — rather than the heavier check_spread_tolerance()
    preview; that execution-grade check still runs inside _execute_buy when
    config.LIVE_MODE is on, at the one moment it actually matters, not on
    every monitoring pass while an order is still resting.

    Deliberately reads directly off the DB each cycle (get_pending_executions)
    rather than threading an in-memory list through persist() the way
    positions/source_positions do: these rows have their own lifecycle
    (pending -> filled/expired/invalidated) and don't need round-tripping
    through load_state()'s dict shape. This also means the table is already
    shaped correctly for a future swap of the polling sweep for a real-time
    Polygon RPC websocket feed (see config.LIMIT_ORDER_TRACKED_WALLETS's
    roadmap note) — only WHERE rows get created/updated from would change,
    not what they record or how they're evaluated.
    """
    now = time.time()
    for order in get_pending_executions(status="pending"):
        if SHUTDOWN_REQUESTED:
            return
        wallet_address = order["wallet_address"]
        market_slug = order["market_slug"]
        outcome = order["outcome"]
        key = position_key(wallet_address, market_slug, outcome)
        nickname = nickname_for(wallet_address, tracked_by_lower)

        current_whale_shares = source_positions.get(key, 0.0)
        if not whale_still_holding(current_whale_shares, order["whale_shares_at_creation"]):
            close_pending_execution(order["id"], "invalidated", invalidated_reason="whale_sold")
            append_log({"timestamp": now_iso(), "event_type": "limit_order_invalidated",
                        "trader_address": wallet_address, "trader_nickname": nickname,
                        "market_slug": market_slug, "outcome": outcome,
                        "pending_execution_id": order["id"], "reason": "whale_sold",
                        "whale_shares_at_creation": order["whale_shares_at_creation"],
                        "current_whale_shares": current_whale_shares,
                        "detail": f"tracked wallet's remaining shares ({current_whale_shares:.4f}) "
                                  f"dropped below {config.LIMIT_ORDER_WHALE_HOLD_MIN_FRACTION:.0%} of "
                                  f"its {order['whale_shares_at_creation']:.4f}-share baseline at "
                                  f"order creation — smart money no longer holding, not buying this dip"})
            continue

        current_price, price_error = get_market_ask_price(market_slug, outcome)
        if current_price is None:
            if now >= order["expires_at"]:
                close_pending_execution(order["id"], "expired")
                append_log({"timestamp": now_iso(), "event_type": "limit_order_abandoned",
                            "trader_address": wallet_address, "trader_nickname": nickname,
                            "market_slug": market_slug, "outcome": outcome,
                            "pending_execution_id": order["id"],
                            "reason": f"TTL expired; last price check failed: {price_error}"})
            continue

        lowest_seen_price = order["lowest_seen_price"]
        if current_price < order["anchor_price"]:
            new_low = current_price if lowest_seen_price is None else min(lowest_seen_price, current_price)
            if new_low != lowest_seen_price:
                update_pending_execution_lowest_seen(order["id"], new_low)
                lowest_seen_price = new_low

        if lowest_seen_price is not None and has_rebounded(current_price, lowest_seen_price):
            if current_price > order["anchor_price"]:
                close_pending_execution(order["id"], "invalidated",
                                         invalidated_reason="rebound_exceeded_anchor")
                append_log({"timestamp": now_iso(), "event_type": "limit_order_invalidated",
                            "trader_address": wallet_address, "trader_nickname": nickname,
                            "market_slug": market_slug, "outcome": outcome,
                            "pending_execution_id": order["id"], "reason": "rebound_exceeded_anchor",
                            "anchor_price": order["anchor_price"], "lowest_seen_price": lowest_seen_price,
                            "current_price": current_price,
                            "detail": "rebound confirmed but price already exceeds anchor_price — "
                                      "firing here would itself violate the no-high-chasing rule"})
                continue

            event_slug = risk_state["market_to_event"].get(market_slug)
            if event_slug is None:
                event_slug, holding_rewards_enabled = resolve_market_event(market_slug)
                if event_slug:
                    risk_state["market_to_event"][market_slug] = event_slug
                    save_market_event(market_slug, event_slug, holding_rewards_enabled)
                else:
                    continue  # can't risk-gate without an event; retry next sweep, or eventually TTL out

            wallet_score_entry = wallet_scores.get(wallet_address.lower())
            category_score_detail = None
            if order["category"] and wallet_score_entry:
                category_score_detail = wallet_score_entry.get("categories", {}).get(order["category"])
            score_breakdown = {
                "category": order["category"], "category_score_detail": category_score_detail,
                "composite_score": wallet_score_entry.get("composite") if wallet_score_entry else None,
                # target_usd was frozen at signal time off the sizing formula active then (see Rule
                # 29's docstring) — NOT re-derived from current_price here, so there's no fresh
                # shrunk_win_rate/kelly_fraction to report the way the immediate-copy path has.
                "trade_size_usd": order["target_usd"], "sizing_tier": "limit_order_frozen_at_signal_time",
            }
            base_event = {
                "timestamp": now_iso(), "source_trade_id": order["source_trade_id"],
                "trader_address": wallet_address, "trader_nickname": nickname,
                "market_slug": market_slug, "outcome": outcome, "side": "BUY",
                "source_price": order["anchor_price"], "mode": "paper" if not config.LIVE_MODE else "live",
                "rule_set_version": risk_state.get("active_rule_set_version"),
                "category": order["category"],
                "limit_order": {"pending_execution_id": order["id"], "anchor_price": order["anchor_price"],
                                 "lowest_seen_price": lowest_seen_price, "rebound_fill_price": current_price},
            }
            decision_journal_id = _execute_buy(
                base_event, key, wallet_address, market_slug, outcome, current_price,
                order["target_usd"], event_slug, score_breakdown, positions, risk_state,
                wallet_ev_stats=wallet_ev_stats,
            )
            if decision_journal_id:
                close_pending_execution(order["id"], "filled", filled_at=int(now))
            # A None return means a risk gate blocked it (already logged by
            # _execute_buy) — leave the order pending rather than expiring
            # it early; a portfolio gate that's full now may free up before
            # TTL runs out, and the rebound has already been confirmed once.
            continue

        if now >= order["expires_at"]:
            close_pending_execution(order["id"], "expired")
            append_log({"timestamp": now_iso(), "event_type": "limit_order_abandoned",
                        "trader_address": wallet_address, "trader_nickname": nickname,
                        "market_slug": market_slug, "outcome": outcome,
                        "pending_execution_id": order["id"], "anchor_price": order["anchor_price"],
                        "lowest_seen_price": lowest_seen_price, "last_seen_price": current_price,
                        "reason": "TTL expired without a confirmed rebound"})


def sweep_live_whale_events(positions, source_positions, source_cost_basis, trader_performance,
                             muted_traders, tracked_by_lower, risk_state, wallet_scores,
                             shadow_positions=None, wallet_ev_stats=None):
    """Consumer sweep (2026-07-24) for wss_listener.py's/token_sync_worker.py's
    producer tables (live_whale_event / token_registry) — run every poll
    cycle, same "zero-latency, don't gate on an interval" reasoning as
    sweep_pending_executions().

    DOES NOT call _execute_buy() directly, despite that being the original
    ask. _execute_buy() is only the FINAL execution step (risk gates + the
    ledger write) — it does not contain the Dip & Rebound / VWAP-ratchet
    logic, which lives in process_trade()'s own branching plus
    sweep_pending_executions(). Calling _execute_buy() straight from here
    would bypass Dip & Rebound entirely for any wallet in
    config.LIMIT_ORDER_TRACKED_WALLETS — for strict-4, the pilot wallet
    that whole feature exists for, that reintroduces the exact adverse-
    selection risk Rule 29 was built to prevent, on the one wallet where it
    matters most. It would ALSO skip process_trade()'s other existing
    gates (muted trader, duplicate position, MAX_BUYS_PER_TRADER_OUTCOME,
    the category hard-skip) that apply to every trade regardless of how it
    was detected.

    Instead, each unconsumed event is reshaped into the same `trade` dict
    shape the normal polling feed already produces and handed to
    process_trade() itself — the SAME pipeline, every gate above plus Dip &
    Rebound branching plus _execute_buy at the very end when appropriate,
    not a second copy of that decision logic maintained separately (and
    liable to drift from the first).

    `usdc_amount` becomes `size_usd` — wss_listener.py already derived this
    in human USD terms from the paired collateral transfer, no decimals
    math needed here. Events with a NULL price/usdc_amount (no collateral
    leg was found on-chain) or a non-'buy' direction are still marked
    consumed (never silently left to retry forever) but not turned into a
    trade — logged instead, since neither is a case process_trade() itself
    knows how to handle.

    The `tracked_by_lower` membership check below mirrors main()'s own
    polling-loop gate (an untracked wallet is skipped BEFORE process_trade()
    is even called there) — process_trade() itself does not enforce that
    boundary, so this sweep must, or a wss_listener.py operator watching a
    wallet outside config.TRACKED_TRADERS could get silently copied.

    Idempotency: consumed_at is stamped in a try/finally, unconditionally —
    success, a risk gate blocked it, process_trade() deferred it to a
    pending_execution, or it raised — via mark_whale_event_consumed()'s own
    dedicated connection+commit. An event is never reprocessed once fetched
    here, whatever happened to it.

    NOT solved here, flagged rather than silently accepted: if the SAME
    wallet is watched by BOTH this WSS-fed path and the normal polling loop
    at once, the same real-world trade can be detected twice through two
    entirely separate identifier spaces (on-chain tx_hash+log_index here vs.
    the polling feed's own trade_id) — there is no shared dedup key between
    them. process_trade()'s duplicate-position/MAX_BUYS_PER_TRADER_OUTCOME
    checks are a partial safety net (a clear duplicate buy on the same key
    gets blocked), not a complete fix. The real fix — excluding any
    wss_listener.py-watched wallet from main()'s polling wallet_addresses
    list, so there's exactly one detector per wallet — is a deliberate
    choice for a human to make, not applied automatically here.

    'UNKNOWN TOKEN' ON-DEMAND FALLBACK (2026-07-25 addendum): after the
    INNER-JOIN-matched events above, a second pass handles events whose
    token_id has NO token_registry match yet (get_unconsumed_whale_events_
    without_registry_match()) — a market wss_listener.py detected before
    token_sync_worker.py's periodic sync ever indexed it. Each such event
    triggers polymarket_simulator.fetch_market_by_token_id() (a direct
    Gamma `/markets/keyset?clob_token_ids=` call, verified live — NOT
    `/tokens/{token_id}`, which does not exist). On a resolution: the row
    is upserted into token_registry (so future events for the same token_id
    hit the fast INNER-JOIN path, not this fallback again) and immediately
    handed to the same _handle_matched_whale_event() the main loop above
    uses — same trade dict, same process_trade() call, same gates. On NO
    resolution (Gamma hasn't indexed the market either yet): the event is
    left unconsumed and retried on a later sweep, UNLESS it's already older
    than config.WHALE_EVENT_FALLBACK_MAX_AGE_SECONDS (1h default), in which
    case the sweep gives up and marks it consumed rather than retrying a
    token_id that may never resolve, forever.
    """
    for event in get_unconsumed_whale_events():
        if SHUTDOWN_REQUESTED:
            return
        try:
            _handle_matched_whale_event(event, positions, source_positions, source_cost_basis,
                                         trader_performance, muted_traders, tracked_by_lower,
                                         risk_state, wallet_scores, shadow_positions=shadow_positions,
                                         wallet_ev_stats=wallet_ev_stats)
        finally:
            mark_whale_event_consumed(event["id"])

    for event in get_unconsumed_whale_events_without_registry_match():
        if SHUTDOWN_REQUESTED:
            return
        token_id = event["token_id"]
        resolved = None
        try:
            resolved = polymarket_simulator.fetch_market_by_token_id(token_id)
        except Exception as e:
            logger.warning(f"live_whale_event {event['id']}: fallback lookup for "
                            f"token_id={token_id} failed: {e}")

        if resolved is None:
            age_seconds = time.time() - event["detected_at"]
            if age_seconds >= config.WHALE_EVENT_FALLBACK_MAX_AGE_SECONDS:
                mark_whale_event_consumed(event["id"])
                append_log({"timestamp": now_iso(), "event_type": "error",
                            "trader_address": event["wallet_address"],
                            "error": f"live_whale_event {event['id']}: token_id={token_id} still "
                                     f"unresolved after {age_seconds:.0f}s, giving up"})
            else:
                logger.info(f"live_whale_event {event['id']}: token_id={token_id} not yet "
                            f"resolvable via fallback, will retry (age {age_seconds:.0f}s)")
            continue

        market_slug, outcome = resolved
        upsert_token_registry_row(token_id, market_slug, outcome)
        event_with_market = {**event, "market_slug": market_slug, "outcome": outcome}
        try:
            _handle_matched_whale_event(event_with_market, positions, source_positions,
                                         source_cost_basis, trader_performance, muted_traders,
                                         tracked_by_lower, risk_state, wallet_scores,
                                         shadow_positions=shadow_positions, wallet_ev_stats=wallet_ev_stats)
        finally:
            mark_whale_event_consumed(event["id"])


def start_patient_exit(key, trader, market_slug, outcome, shares, close_reason):
    """Priority 3 (2026-07-26) entry point — places the FIRST resting
    limit-sell and records a pending_exit_order row. Does NOT touch
    `positions` at all: the position stays open (in the ledger's own
    accounting) until sweep_pending_exit_orders() confirms either a real
    fill or fires the guaranteed market-sell fallback, exactly mirroring
    how Rule 29's pending_execution never touches positions before a
    confirmed fill either.

    LIVE_MODE only by construction (the only caller,
    close_position_trailing_tp, already gates this behind
    config.LIVE_MODE) — a resting limit order is a real-money concept with
    no paper-mode analog worth simulating here (paper mode's existing
    measure_paper_shortfall already answers "what would an immediate fill
    cost us" without needing an order lifecycle).

    UNVERIFIED against a real fill (same status as extract_fill_price()
    already carries for plain buy/sell): bullpen's limit-sell response
    shape has never been exercised by this bot. extract_order_id() failing
    to find an id is treated as a hard failure (logged, order not tracked)
    rather than silently proceeding — better to fall through to
    close_position_trailing_tp's original raise/skip behavior than to
    track a pending_exit_order this code can never actually manage.
    """
    best_bid, best_ask, price_error = get_market_bid_ask(market_slug, outcome)
    if best_bid is None:
        logger.warning(f"start_patient_exit: no live bid/ask for {market_slug}/{outcome} "
                        f"({price_error}), cannot peg — caller should fall back to immediate sell")
        return None

    mid_price = (best_bid + best_ask) / 2
    live_edge = compute_live_edge_pct()
    floor_price = compute_slippage_floor_price(mid_price, live_edge)
    init_price = best_ask  # join the touch on the ask side -- a legitimate resting maker sell price

    try:
        response = run_bullpen_json([
            "polymarket", "limit-sell", market_slug, outcome,
            "--price", str(round(init_price, 4)), "--shares", str(shares), "--yes",
        ])
    except Exception as e:
        logger.error(f"start_patient_exit: limit-sell placement failed for {market_slug}/{outcome}: {e}")
        return None

    order_id = extract_order_id(response)
    if order_id is None:
        logger.error(f"start_patient_exit: limit-sell response had no recognizable order id "
                     f"(raw={response!r}) — not tracking, caller should fall back to immediate sell")
        return None

    pending_id = create_pending_exit_order(
        wallet_address=trader, market_slug=market_slug, outcome=outcome, position_key=key,
        shares=shares, init_price=init_price, floor_price=floor_price, close_reason=close_reason,
        bullpen_order_id=order_id,
    )
    append_log({"timestamp": now_iso(), "event_type": "patient_exit_started",
                "trader_address": trader, "market_slug": market_slug, "outcome": outcome,
                "pending_exit_order_id": pending_id, "init_price": init_price,
                "floor_price": floor_price, "live_edge_pct": live_edge, "close_reason": close_reason})
    return pending_id


def start_shadow_patient_exit(key, trader, market_slug, outcome, shares, immediate_exit_price,
                               close_reason):
    """Paper-only comparison sibling of start_patient_exit() (2026-08-01,
    item 2 of the post-kill-switch-fix roadmap) -- called unconditionally
    from close_position_trailing_tp(), in BOTH paper and live mode,
    alongside whatever exit actually happens. start_patient_exit() itself
    stays LIVE_MODE-only (a real resting order has no paper analog), which
    means its edge has never been measured while this bot only runs in
    paper mode -- this function exists to close that gap: same P_init/
    P_floor math, driven purely by direct market-data reads
    (get_market_bid_ask), never a bullpen call, never touching `positions`.

    Best-effort only: a failed price read here just means this one exit
    isn't tracked for comparison, not a fallback path -- there's nothing to
    fall back TO, since the real exit this shadows already happened by the
    time this is called.
    """
    best_bid, best_ask, price_error = get_market_bid_ask(market_slug, outcome)
    if best_bid is None:
        return None

    mid_price = (best_bid + best_ask) / 2
    live_edge = compute_live_edge_pct()
    floor_price = compute_slippage_floor_price(mid_price, live_edge)
    init_price = best_ask  # same "join the touch on the ask side" as the real mechanism

    row_id = create_shadow_patient_exit(
        wallet_address=trader, market_slug=market_slug, outcome=outcome, position_key=key,
        shares=shares, init_price=init_price, floor_price=floor_price,
        immediate_exit_price=immediate_exit_price, close_reason=close_reason,
    )

    # Go OMS mirror (2026-08-01, Session 6, config.ENABLE_OMS_SHADOW_MIRROR
    # default False) -- purely an additional, parallel record for
    # validating the OMS itself; never allowed to affect this function's
    # real return value or the shadow-patient-exit row just created above.
    if config.ENABLE_OMS_SHADOW_MIRROR and row_id is not None:
        try:
            oms_client.create_order(idempotency_key=row_id)
        except oms_client.OmsClientError as e:
            logger.warning(f"OMS shadow mirror create failed for {row_id}: {e}")

    return row_id


_SHADOW_STATUS_TO_OMS_STATUS = {
    "filled": "filled",
    "fallback_timeout": "expired",
    "abandoned": "invalidated",
}


def _mirror_shadow_patient_exit_terminal_to_oms(row_id, python_status):
    """Best-effort mirror of an already-decided shadow-patient-exit
    terminal outcome into the Go OMS (2026-08-01, Session 6) -- re-derives
    the OMS order's internal id via create_order()'s own idempotent replay
    (row_id is the SAME idempotency key start_shadow_patient_exit() used
    to create it, whether or not the mirror was actually on back then),
    then transitions it. Never raises: a failure here must never affect
    the real shadow-patient-exit row the caller already closed.
    """
    if not config.ENABLE_OMS_SHADOW_MIRROR:
        return
    to = _SHADOW_STATUS_TO_OMS_STATUS.get(python_status)
    if to is None:
        return
    try:
        oms_order = oms_client.create_order(idempotency_key=row_id)
        oms_client.transition_order(oms_order["id"], to=to)
    except oms_client.OmsClientError as e:
        logger.warning(f"OMS shadow mirror transition failed for {row_id} -> {to}: {e}")


def sweep_shadow_patient_exits():
    """Runs every poll cycle alongside sweep_pending_exit_orders() -- for
    each open shadow_patient_exit row, decides whether a resting maker sell
    at the current pegged price would have been crossed by now. A real
    limit-sell fills when a buyer's bid reaches (or exceeds) our resting
    ask; the direct market-data analog of that is simply best_bid >=
    current pegged price, so that's the fill test here (no order book to
    poll, since no order was ever placed).

    Same guaranteed-termination discipline as the real sweep: past
    config.ORDER_PEG_MAX_TOTAL_WAIT_SECONDS, this closes out at whatever
    best_bid currently is (the realistic price an actual guaranteed-
    fallback market sell would have obtained), never left open forever. If
    the market itself becomes unreadable mid-simulation (resolved, no
    book) the row is marked 'abandoned' with no resolved_price -- fabricating
    a comparison price here would corrupt get_shadow_patient_exit_comparison_stats(),
    so an unreadable market is dropped rather than guessed at.
    """
    now = time.time()
    for row in get_shadow_patient_exits(status="pending"):
        if SHUTDOWN_REQUESTED:
            return

        best_bid, best_ask, price_error = get_market_bid_ask(row["market_slug"], row["outcome"])
        if best_bid is None:
            close_shadow_patient_exit(row["id"], "abandoned", resolved_price=None)
            _mirror_shadow_patient_exit_terminal_to_oms(row["id"], "abandoned")
            continue

        if best_bid >= row["current_price"]:
            close_shadow_patient_exit(row["id"], "filled", resolved_price=row["current_price"])
            _mirror_shadow_patient_exit_terminal_to_oms(row["id"], "filled")
            continue

        elapsed = now - row["created_at"]
        if elapsed >= config.ORDER_PEG_MAX_TOTAL_WAIT_SECONDS:
            close_shadow_patient_exit(row["id"], "fallback_timeout", resolved_price=best_bid)
            _mirror_shadow_patient_exit_terminal_to_oms(row["id"], "fallback_timeout")
            continue

        mid = (best_bid + best_ask) / 2
        spread_ratio = (best_ask - best_bid) / mid if mid else None
        reprice_interval = compute_reprice_interval_seconds(spread_ratio)

        time_since_last_reprice = now - (row["last_repriced_at"] or row["created_at"])
        if time_since_last_reprice < reprice_interval:
            continue

        new_price = compute_pegged_price(row["init_price"], row["floor_price"], elapsed,
                                          reprice_interval)
        if new_price != row["current_price"]:
            update_shadow_patient_exit_price(row["id"], new_price)


def _book_completed_exit(order, fill_price, positions, trader_performance, muted_traders,
                          event_type_suffix):
    """Shared ledger-closing helper for sweep_pending_exit_orders()'s two
    terminal paths (a real fill, or the market-sell fallback) — both need
    the exact same position-ledger write and circuit-breaker check, just
    with different `fill_price`/`event_type_suffix`. Mirrors
    close_position_trailing_tp's own ledger-write logic (this mechanism is
    only ever reached FROM a trailing-TP trigger in this first pass — see
    that function's opt-in check), always a full (not proportional) close.
    """
    pos = positions.get(order["position_key"])
    if pos is None:
        # Position already closed some other way (e.g. resolved) while this
        # order was resting -- nothing left to book. Not an error: markets
        # resolving out from under a resting order is a real possibility.
        return
    shares_closed = pos["shares"]
    cost_basis_closed = pos["cost_basis_usd"]
    proceeds_usd = shares_closed * fill_price
    pnl_usd = proceeds_usd - cost_basis_closed

    del positions[order["position_key"]]

    nickname = order["wallet_address"]  # best-effort; sweep call site doesn't carry tracked_by_lower
    append_log({"timestamp": now_iso(), "event_type": f"live_sell_{event_type_suffix}",
                "trader_address": order["wallet_address"], "trader_nickname": nickname,
                "market_slug": order["market_slug"], "outcome": order["outcome"], "side": "SELL",
                "mode": "live", "pending_exit_order_id": order["id"],
                "our_shares_closed": shares_closed, "our_shares_remaining": 0.0,
                "proceeds_usd": proceeds_usd, "cost_basis_usd": cost_basis_closed,
                "pnl_usd": pnl_usd, "fill_price": fill_price})
    check_circuit_breaker(order["wallet_address"], nickname, pnl_usd, cost_basis_closed, trader_performance, muted_traders)


def sweep_pending_exit_orders(positions, trader_performance, muted_traders):
    """Priority 3's sweep (2026-07-26) — run every poll cycle. For each
    resting pending_exit_order: check fill status; if unfilled, reprice
    (cancel+replace) once the CURRENT liquidity-regime-appropriate Δt has
    elapsed since the last reprice, walking the price down toward
    floor_price; once total elapsed time exceeds
    config.ORDER_PEG_MAX_TOTAL_WAIT_SECONDS, cancel and fire an immediate
    market sell instead — this bound is not optional. A resting sell that
    never resolves would be exactly the "risk layer traps you in a
    position" failure mode Rule 6/11 exist to prevent; every order created
    by start_patient_exit() is guaranteed to terminate in either 'filled'
    or 'fallback_market_sell', never left open indefinitely.

    UNVERIFIED against real bullpen order responses (same status as
    start_patient_exit() and extract_order_id/extract_order_status) — the
    poll-order/orders --cancel/sell call shapes used below have never been
    exercised against a live fill in this session.
    """
    now = time.time()
    for order in get_pending_exit_orders(status="pending"):
        if SHUTDOWN_REQUESTED:
            return

        try:
            poll_response = run_bullpen_json(
                ["polymarket", "poll-order", order["bullpen_order_id"], "--timeout", "2"]
            )
        except Exception as e:
            logger.warning(f"sweep_pending_exit_orders: poll-order failed for "
                           f"{order['id']} ({order['bullpen_order_id']}): {e}")
            poll_response = {}

        status = extract_order_status(poll_response)
        if status in ("FILLED", "MATCHED"):
            fill_price = extract_fill_price(poll_response) or order["current_price"]
            close_pending_exit_order(order["id"], "filled", filled_at=int(now))
            _book_completed_exit(order, fill_price, positions, trader_performance, muted_traders,
                                 "patient_exit_filled")
            continue

        elapsed = now - order["created_at"]
        if elapsed >= config.ORDER_PEG_MAX_TOTAL_WAIT_SECONDS:
            try:
                run_bullpen_json(["polymarket", "orders", "--cancel", order["bullpen_order_id"], "--yes"])
            except Exception as e:
                logger.error(f"sweep_pending_exit_orders: cancel failed for {order['id']} "
                             f"({order['bullpen_order_id']}): {e} — attempting market sell anyway")

            try:
                response = require_filled(run_bullpen_json([
                    "polymarket", "sell", order["market_slug"], order["outcome"], str(order["shares"]),
                    "--yes",
                ]), "patient-exit fallback market sell")
                fill_price = extract_fill_price(response) or order["floor_price"]
                close_pending_exit_order(order["id"], "fallback_market_sell", filled_at=int(now))
                _book_completed_exit(order, fill_price, positions, trader_performance, muted_traders,
                                     "patient_exit_fallback")
                append_log({"timestamp": now_iso(), "event_type": "patient_exit_fallback_triggered",
                            "pending_exit_order_id": order["id"], "market_slug": order["market_slug"],
                            "outcome": order["outcome"],
                            "reason": f"unfilled after {elapsed:.0f}s, exceeded "
                                      f"{config.ORDER_PEG_MAX_TOTAL_WAIT_SECONDS}s max wait"})
            except Exception as e:
                # The one truly dangerous outcome: canceled the resting
                # order but the fallback market sell ALSO failed. Logged as
                # an error (not silently retried into a possible double
                # cancel/double sell) -- this needs a human to look at the
                # position directly.
                append_log({"timestamp": now_iso(), "event_type": "error",
                            "market_slug": order["market_slug"], "outcome": order["outcome"],
                            "pending_exit_order_id": order["id"],
                            "error": f"patient-exit fallback market sell FAILED after canceling the "
                                     f"resting order: {e} — position may be unmanaged, needs manual review"})
            continue

        best_bid, best_ask, price_error = get_market_bid_ask(order["market_slug"], order["outcome"])
        spread_ratio = None
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2
            spread_ratio = (best_ask - best_bid) / mid if mid else None
        reprice_interval = compute_reprice_interval_seconds(spread_ratio)

        time_since_last_reprice = now - (order["last_repriced_at"] or order["created_at"])
        if time_since_last_reprice < reprice_interval:
            continue  # not due for a reprice yet this cycle

        new_price = compute_pegged_price(order["init_price"], order["floor_price"], elapsed,
                                          reprice_interval)
        if new_price == order["current_price"]:
            continue  # already at this price (e.g. already at the floor) -- nothing to replace

        try:
            run_bullpen_json(["polymarket", "orders", "--cancel", order["bullpen_order_id"], "--yes"])
            response = run_bullpen_json([
                "polymarket", "limit-sell", order["market_slug"], order["outcome"],
                "--price", str(round(new_price, 4)), "--shares", str(order["shares"]), "--yes",
            ])
            new_order_id = extract_order_id(response)
            if new_order_id is None:
                raise RuntimeError(f"reprice limit-sell response had no recognizable order id: {response!r}")
            update_pending_exit_order_price(order["id"], new_price, bullpen_order_id=new_order_id)
        except Exception as e:
            append_log({"timestamp": now_iso(), "event_type": "error",
                        "market_slug": order["market_slug"], "outcome": order["outcome"],
                        "pending_exit_order_id": order["id"],
                        "error": f"reprice (cancel+replace) failed: {e} — will retry next sweep"})


def _handle_matched_whale_event(event, positions, source_positions, source_cost_basis,
                                 trader_performance, muted_traders, tracked_by_lower,
                                 risk_state, wallet_scores, shadow_positions=None,
                                 wallet_ev_stats=None):
    """Shared per-event handling once (market_slug, outcome) are known —
    used by both the fast INNER-JOIN path and the on-demand-fallback-
    resolved path in sweep_live_whale_events(). Deliberately does NOT mark
    consumed_at itself: the two callers have different retry/give-up
    semantics around that (the fallback path can legitimately choose to
    leave an event unconsumed for a retry), so ownership of that decision
    stays with the caller, not this shared helper.

    'Dual-Track' WSS-as-primary-trigger (2026-07-25): a NULL price/
    usdc_amount (the on-chain collateral leg wasn't found) no longer means
    skip. Direction and market are enough to act on — this fetches OUR OWN
    current execution price (get_market_ask_price(), the same no-custody
    read Rule 29's sweep already uses) and executes against THAT, tagged
    price_source="wss_estimated". The polling loop's process_trade() call
    later reconciles the dollar valuation once it has the whale's real
    price (see process_trade()'s reconciliation branch) — but the SHARE
    COUNT is never estimated in the first place: share_amount is a raw
    on-chain event field, always known exactly regardless of whether price
    was derivable, so size_usd is back-derived as
    (share_amount / 10**decimals) * execution_price specifically so
    source_shares = size_usd/price reproduces the exact real share count
    even though execution_price itself is just our own market read, not
    the whale's true fill.
    """
    if event["direction"] != "buy":
        logger.info(f"live_whale_event {event['id']}: direction={event['direction']!r} "
                    f"not handled by this sweep yet, marking consumed without action")
        return

    trader_addr = event["wallet_address"]
    if trader_addr.lower() not in tracked_by_lower:
        logger.info(f"live_whale_event {event['id']}: wallet {trader_addr} not in the "
                    f"active tracked-traders source, skipping")
        return

    if event["price"] is not None and event["usdc_amount"] is not None:
        execution_price = event["price"]
        size_usd = event["usdc_amount"]
        price_source = "wss_derived"
    else:
        execution_price, price_error = get_market_ask_price(event["market_slug"], event["outcome"])
        if execution_price is None:
            logger.info(f"live_whale_event {event['id']}: no whale price AND no live market "
                        f"price available ({price_error}), skipping")
            return
        share_amount_raw = event.get("share_amount")
        if not share_amount_raw:
            logger.info(f"live_whale_event {event['id']}: no share_amount on record, skipping")
            return
        shares_human = float(share_amount_raw) / (10 ** config.OUTCOME_TOKEN_DECIMALS_ASSUMPTION)
        size_usd = shares_human * execution_price
        price_source = "wss_estimated"

    trade = {
        "user_address": trader_addr,
        "market_slug": event["market_slug"],
        "outcome": event["outcome"],
        "side": "BUY",
        "price": execution_price,
        "size_usd": size_usd,
        "trade_id": f"onchain:{event['tx_hash']}:{event['log_index']}",
        "timestamp": None,
        "market_title": "",
        "price_source": price_source,
        "detected_by": "wss",
    }
    try:
        process_trade(trade, positions, source_positions, source_cost_basis,
                       trader_performance, muted_traders, tracked_by_lower,
                       risk_state, wallet_scores, shadow_positions=shadow_positions,
                       wallet_ev_stats=wallet_ev_stats)
    except Exception as e:
        append_log({"timestamp": now_iso(), "event_type": "error",
                    "source_trade_id": trade["trade_id"], "trader_address": trader_addr,
                    "error": f"live_whale_event processing failed: {e}"})


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
    load_state, save_state, append_log, get_tracked_traders, get_monitored_noncopying_traders,
    get_wallet_composite_scores,
    get_wallet_realized_ev_stats,
    get_risk_value, set_risk_value, clear_risk_value, load_market_events, save_market_event,
    load_market_categories, save_market_category, get_active_rule_set_version,
    realized_pnl_total, realized_pnl_since, get_or_create_evaluation_epoch,
    get_closed_trade_stats_since, record_pnl_snapshot, prune_event_log,
    create_pending_execution, get_pending_execution, get_pending_executions,
    update_pending_execution_anchor, update_pending_execution_lowest_seen,
    close_pending_execution,
    get_unconsumed_whale_events, mark_whale_event_consumed,
    get_unconsumed_whale_events_without_registry_match, upsert_token_registry_row,
    compute_live_edge_pct, create_pending_exit_order, get_pending_exit_orders,
    update_pending_exit_order_price, close_pending_exit_order,
    create_shadow_patient_exit, get_shadow_patient_exits,
    update_shadow_patient_exit_price, close_shadow_patient_exit,
    load_market_end_dates, save_market_end_date,
    load_shadow_positions, save_shadow_positions, get_shadow_rehab_returns,
    has_snapshot_for_today, record_daily_snapshot, realized_pnl_today,
)
import risk_manager  # noqa: E402


def resolve_market_event(market_slug):
    """market_slug -> (parent event slug, holding_rewards_enabled), via
    polymarket_simulator.fetch_market_metadata() (direct Gamma read, no
    bullpen — swapped 2026-07-28 after an outage where bullpen wasn't
    installed on the server at all, silently fail-closing every buy forever
    since this risk gate has no LIVE_MODE guard). The response's `events`
    field is a list of event objects (verified live 2026-07-18); sibling
    fields on this endpoint arrive JSON-string-encoded in some cases (see
    _parse_market_resolution), so tolerate both here too.

    holding_rewards_enabled is read straight off this SAME response's
    top-level `holdingRewardsEnabled` field (verified live 2026-07-23 against
    a real market) — zero extra API calls, captured purely for documentation/
    audit purposes (see bot_market_event.holding_rewards_enabled's schema
    comment: it does not feed compute_trade_size_usd() or any scoring
    formula). None when the field is absent from the response.

    Returns (None, None) on any failure — the caller fails CLOSED on the
    event slug (skips the buy), it never guesses an event; a lost
    holding_rewards_enabled reading on the same failure is inconsequential
    since the buy is skipped anyway.
    """
    try:
        info = polymarket_simulator.fetch_market_metadata(market_slug)
    except Exception:
        return None, None
    holding_rewards_enabled = info.get("holdingRewardsEnabled")
    if not isinstance(holding_rewards_enabled, bool):
        holding_rewards_enabled = None
    events = info.get("events")
    if isinstance(events, str):
        try:
            events = json.loads(events)
        except json.JSONDecodeError:
            return None, holding_rewards_enabled
    if isinstance(events, list) and events and isinstance(events[0], dict):
        event = events[0]
        slug = event.get("slug") or (str(event["id"]) if event.get("id") else None)
        return slug or None, holding_rewards_enabled
    return None, holding_rewards_enabled


def resolve_market_end_date(market_slug):
    """market_slug -> end_date_iso (YYYY-MM-DD string) or None on failure.
    "Priority 4" (2026-07-26, theta-decay TTP activation). Same direct-Gamma
    read resolve_market_event() uses (polymarket_simulator.
    fetch_market_metadata(), swapped off bullpen 2026-07-28), which derives
    `endDateIso` from Gamma's own `endDate` field — but kept as its OWN
    function rather than folded into resolve_market_event(), same precedent
    as resolve_market_category being separate: this is called at a
    different point in a position's lifecycle (TTP-sweep time, on an
    already-open position) than resolve_market_event() (BUY time), so the
    two can't actually share one network call across time even though they
    hit the same endpoint.
    """
    try:
        info = polymarket_simulator.fetch_market_metadata(market_slug)
    except Exception:
        return None
    end_date_iso = info.get("endDateIso")
    return end_date_iso if isinstance(end_date_iso, str) and end_date_iso else None


def compute_days_remaining(end_date_iso, now=None):
    """Whole days between now and end_date_iso (YYYY-MM-DD), floored at 0
    (a market past its own end date, still open for whatever reason, is
    treated as 0 days remaining -- the closest, most conservative point on
    compute_theta_decay_activation_pct()'s scale -- not a negative number
    that formula was never designed to take). Returns None if end_date_iso
    can't be parsed, so callers can fall back to the static threshold
    rather than guess.
    """
    if not end_date_iso:
        return None
    try:
        end_date = datetime.strptime(end_date_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    now = now if now is not None else datetime.now(timezone.utc)
    delta_days = (end_date - now).total_seconds() / 86400
    return max(delta_days, 0.0)


def compute_theta_decay_activation_pct(days_remaining):
    """T_act = T_min + (T_base - T_min) * min(1, D_rem / W) -- scales DOWN
    linearly from T_base (far from resolution, require a bigger move to
    trust it) to T_min (at/near resolution, trust a smaller move) as
    days_remaining shrinks toward config.THETA_DECAY_TP_WINDOW_DAYS and
    below. days_remaining=None (couldn't resolve an end date) falls back
    to the ORIGINAL static config.TRAILING_TP_ACTIVATION_PCT -- the
    conservative choice when we don't actually know how close resolution
    is, not an assumption either way.
    """
    if days_remaining is None:
        return config.TRAILING_TP_ACTIVATION_PCT
    t_min = config.THETA_DECAY_TP_MIN_ACTIVATION_PCT
    t_base = config.THETA_DECAY_TP_MAX_ACTIVATION_PCT
    window = config.THETA_DECAY_TP_WINDOW_DAYS
    fraction = min(1.0, days_remaining / window) if window else 1.0
    return t_min + (t_base - t_min) * fraction


def compute_lifespan_fraction_remaining(opened_at, days_remaining, now=None):
    """Time-Decay Loss Cut (2026-08-01): what fraction of a position's OWN
    entry-to-resolution runway is left right now -- e.g. 0.20 means 20% of
    the span from when WE opened this position to this market's resolution
    date is still ahead. Deliberately relative to each position's own
    lifespan, not a fixed absolute time window, so a 2-hour esports match
    and a 6-month macro market are judged on the same proportional scale
    rather than one fixed cutoff being wrong for both.

    Returns None (never guesses) when either input is unusable: no entry
    timestamp (a position loaded from before this field existed), or no
    resolvable end date (same "unknown isn't confirmed toxic" fallback
    compute_days_remaining() already uses) -- or when total lifespan comes
    out non-positive (a market already past its own end date some other
    way), where a fraction wouldn't be meaningful.
    """
    if opened_at is None or days_remaining is None:
        return None
    now = now if now is not None else time.time()
    days_held = (now - opened_at) / 86400.0
    total_lifespan_days = days_held + days_remaining
    if total_lifespan_days <= 0:
        return None
    return days_remaining / total_lifespan_days


def is_time_decay_loss_cut_eligible(peak_profit_pct, lifespan_fraction_remaining,
                                     peak_floor_pct=None, lifespan_fraction_threshold=None):
    """Time-Decay Loss Cut (2026-08-01) — see config.ENABLE_TIME_DECAY_LOSS_CUT's
    docstring for the real-data root cause this targets. BOTH conditions
    required: peak_profit_pct has NEVER exceeded peak_floor_pct (a position
    that showed real life stays on the TTP/resolution path, unaffected) AND
    lifespan_fraction_remaining has fallen to/below lifespan_fraction_threshold
    (deep into the position's own last stretch, not just 'been open a
    while'). lifespan_fraction_remaining=None (unresolvable end date or
    missing entry timestamp) never fires -- consistent with every other
    "don't guess when data's missing" gate in this codebase.
    """
    peak_floor_pct = (peak_floor_pct if peak_floor_pct is not None
                       else config.TIME_DECAY_LOSS_CUT_PEAK_FLOOR_PCT)
    lifespan_fraction_threshold = (lifespan_fraction_threshold if lifespan_fraction_threshold is not None
                                    else config.TIME_DECAY_LOSS_CUT_LIFESPAN_FRACTION)
    if lifespan_fraction_remaining is None:
        return False
    return (peak_profit_pct < peak_floor_pct
            and lifespan_fraction_remaining <= lifespan_fraction_threshold)


def position_key(trader, market_slug, outcome):
    """Lowercases `trader` before building the key (2026-07-26 fix) --
    without this, the SAME real wallet detected via two sources that report
    its address in different casing (e.g. an EIP-55 checksummed address
    from the polling feed vs. raw lowercase hex from an on-chain event)
    silently fragments into two different position_key entries. Confirmed
    live: 7 of 20 tracked wallets had exactly this split across paper_trade
    rows, and risk_manager.wallet_exposure_usd()'s per-wallet cap (Rule 26)
    was undercounting one of them by more than half its true exposure
    ($82.60 real vs. $50 cap, only ever seeing one casing at a time) because
    it never saw the combined total. market_slug/outcome are left as-is --
    Polymarket slugs/outcomes are not case-ambiguous the way an address is.
    """
    return f"{trader.lower()}|{market_slug}|{outcome}"


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
    lookup. Trader comparison is case-insensitive (2026-07-26 fix, same
    reasoning as position_key()'s own docstring) -- otherwise stale,
    not-yet-migrated data in a different casing than `trader`'s current
    casing would be misread as a genuinely different trader.
    """
    for other_key, other_pos in positions.items():
        if other_key == own_key:
            continue
        parts = other_key.split("|")
        if len(parts) != 3:
            continue
        other_trader, other_slug, other_outcome = parts
        if other_trader.lower() == trader.lower():
            continue
        if other_slug == market_slug and other_outcome == outcome and other_pos.get("shares", 0) > 0:
            return other_trader
    return None


def compute_wallet_ev_t_statistic(returns):
    """One-sample t-statistic testing whether the mean of `returns` (each a
    real, non-dust closed copy-trade's pnl_usd/cost_basis_usd) is
    significantly NEGATIVE. Mirrors should_skip_category()'s existing
    pnl_t_stat test (scoreWalletCategories.ts) exactly -- same statistical
    approach, computed live here instead of offline since a wallet's own
    recent copy-trade history isn't something that job has. Pure function,
    unit-testable without a DB call.

    Uses CATEGORY_SKIP_Z_CRITICAL as the critical value at the call site,
    not an exact df-adjusted t-critical value -- same simplification
    should_skip_category() already makes for the identical kind of
    decision; consistency with the established precedent, not a new
    inconsistency.

    Returns None if there are fewer than 2 samples. For zero variance,
    identical negative/positive samples return -inf/+inf respectively;
    only an all-zero sample remains neutral (None).
    """
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    # Epsilon, not a bare `<= 0` check -- floating-point returns that are
    # mathematically identical (e.g. every trade at exactly the same
    # price) can still leave a near-zero but nonzero variance due to
    # representation error, which would otherwise blow stderr up toward
    # zero and t toward a meaningless, arbitrarily huge magnitude.
    if variance < 1e-12:
        # Identical negative observations are not "no evidence". The old
        # None branch left strict-5 active after ten consecutive -100%
        # returns because its variance was exactly zero. Preserve the
        # undefined/neutral result only when the identical mean is zero.
        if mean < 0:
            return float("-inf")
        if mean > 0:
            return float("inf")
        return None
    stderr = (variance / n) ** 0.5
    return mean / stderr


def _shadow_wallet_cost_basis_usd(trader, shadow_positions):
    """Sum of cost_basis_usd across every shadow_positions entry belonging
    to `trader` -- keys are position_key(trader, market_slug, outcome), so
    matching the "trader|" prefix (already lowercased by position_key) is
    enough, no separate per-trader index needed for a dict this small."""
    prefix = trader.lower() + "|"
    return sum(pos["cost_basis_usd"] for k, pos in shadow_positions.items() if k.startswith(prefix))


def _execute_shadow_buy(base_event, key, trader, market_slug, outcome, price, shadow_positions,
                         wallet_ev_stats=None, shadow_kind="rehab"):
    """Shadow Rehab (2026-07-27, Rule 37): books a hypothetical copy of a
    MUTED wallet's real buy into an isolated ledger
    (paper_trade.strategy="shadow_rehab" once persisted via
    save_shadow_positions) -- never touches `positions`, never calls
    risk_manager, never consumes real exposure. The whole point is
    answering "would this wallet be profitable if we resumed copying it,"
    which needs no real capital at risk to measure.

    Fixed config.SHADOW_REHAB_TRADE_USD size, not wallet-score sizing: the
    rehab decision is driven by RETURN ratios (scale-invariant), so a real
    copy's exact tiered size carries no extra signal here -- deliberately
    simpler than _execute_buy() by design, not an oversight. Also
    deliberately skips MAX_BUYS_PER_TRADER_OUTCOME and the cross-trader
    duplicate-position guard: those manage OUR portfolio's real capital
    allocation, which doesn't apply to an isolated simulation -- multiple
    shadow buys into the same market simply average up freely.

    Same paper-shortfall-aware pricing as a real paper buy
    (measure_paper_shortfall) so the shadow simulation is held to the same
    fill-fidelity bar Rule 32 established for real paper trades -- an
    unrealistically-optimistic shadow simulation would make rehab
    decisions on fantasy numbers.

    Aggregate cap (2026-08-01): a muted wallet with no other limiter kept
    accumulating shadow positions forever (one observed case: 258 open
    positions, ~$121k phantom cost basis, 2,364 buys) -- pure noise/storage
    growth, since sweep_shadow_rehab()'s reinstatement test only ever reads
    the most recent config.MUTE_EV_MIN_SAMPLES CLOSED returns regardless of
    how large the ledger gets. Reuses risk_manager.wallet_exposure_cap_usd()
    -- the SAME formula that would apply to this wallet if it were actually
    live -- as the ceiling on total shadow cost basis; once reached, new
    shadow buys are simply skipped (existing open shadow positions are left
    alone to resolve naturally) until they close and free up room.
    """
    if shadow_kind not in ("rehab", "challenger"):
        raise ValueError(f"unsupported shadow kind: {shadow_kind!r}")
    event_prefix = f"shadow_{shadow_kind}"
    cap = risk_manager.wallet_exposure_cap_usd(trader, wallet_ev_stats)
    if cap is not None and _shadow_wallet_cost_basis_usd(trader, shadow_positions) >= cap:
        append_log({**base_event, "event_type": f"skip_{event_prefix}_wallet_cap",
                    "reason": f"shadow cost basis at/above wallet cap ${cap:.2f}"})
        return

    trade_usd = config.SHADOW_REHAB_TRADE_USD
    our_shares = trade_usd / price
    actual_cost_usd = trade_usd

    shortfall = measure_paper_shortfall(
        market_slug, outcome, "BUY", trade_usd, price, trade_usd=trade_usd,
    )
    base_event.update(shortfall)
    if shortfall.get("shortfall_status") == "ok":
        our_shares = trade_usd / shortfall["executable_price"]
        actual_cost_usd = trade_usd + shortfall.get("trading_fee_usd", 0.0) \
            + shortfall.get("network_fee_usd", 0.0)

    pos = shadow_positions.get(key) or {"shares": 0.0, "cost_basis_usd": 0.0,
                                         "avg_entry_price": 0.0, "buy_count": 0}
    new_shares = pos["shares"] + our_shares
    new_cost = pos["cost_basis_usd"] + actual_cost_usd
    pos["avg_entry_price"] = new_cost / new_shares if new_shares else 0.0
    pos["shares"] = new_shares
    pos["cost_basis_usd"] = new_cost
    pos["buy_count"] = pos.get("buy_count", 0) + 1
    shadow_positions[key] = pos

    append_log({**base_event, "event_type": f"{event_prefix}_buy",
                "our_trade_usd": trade_usd, "our_shares": our_shares})


def sweep_shadow_rehab(muted_traders):
    """Shadow Rehab (2026-07-27, Rule 37): evidence-based mute recovery,
    run once per poll cycle. Replaces "a mute is permanent" (the gap Joey
    flagged: "a permanent mute without an auto-recovery mechanism will
    eventually drain our active pool to zero due to normal variance")
    with "a mute lifts once shadow-copying has shown a statistically
    significant POSITIVE edge" -- the same t-test machinery Rule 36 built
    for muting, run in reverse: config.CATEGORY_SKIP_Z_CRITICAL as the
    positive-side critical value, config.MUTE_EV_MIN_SAMPLES as the
    minimum real shadow trades before the test is trusted.

    Mutates `muted_traders` in place (deletes a reinstated wallet's entry)
    -- the caller's next persist()/save_state() call picks this up
    naturally through the exact same mechanism that originally recorded
    the mute, no separate DB write needed here.
    """
    if not config.ENABLE_SHADOW_REHAB:
        return
    for key in list(muted_traders.keys()):
        returns = get_shadow_rehab_returns(key, limit=config.MUTE_EV_MIN_SAMPLES)
        if len(returns) < config.MUTE_EV_MIN_SAMPLES:
            continue
        t_stat = compute_wallet_ev_t_statistic(returns)
        if t_stat is not None and t_stat >= config.CATEGORY_SKIP_Z_CRITICAL:
            old_reason = muted_traders[key].get("reason", "?")
            del muted_traders[key]
            logger.warning(
                f"Trader {key} REINSTATED via Shadow Rehab: {len(returns)} shadow trades show "
                f"statistically significant positive edge (t={t_stat:.2f}); was muted for: {old_reason}"
            )
            append_log({"timestamp": now_iso(), "event_type": "shadow_rehab_reinstated",
                        "trader_address": key, "t_statistic": t_stat,
                        "shadow_trade_count": len(returns), "previous_mute_reason": old_reason})


def maybe_snapshot_daily_portfolio(positions, prices_by_key, tracked_traders, muted_traders):
    """Daily equity/cash/PnL/active-trader snapshot for the personal
    Grafana dashboard (2026-07-28) — one row/day in
    daily_portfolio_snapshots, read-only observability, feeds nothing back
    into any trading decision.

    Fires once conditions are both true: the current UTC hour is at/past
    config.DAILY_SNAPSHOT_TRIGGER_HOUR_UTC, AND today's UTC date has no
    row yet (db.has_snapshot_for_today() — a real DB check, not an
    in-memory flag, so this is correct across restarts: it neither
    re-snapshots on every restart near the trigger hour nor silently
    misses a day the bot happened to be down across it).

    active_traders_followed = tracked minus muted (the same "actively
    copying" definition the Next.js dashboard already established, not
    the raw static TRACKED_TRADERS count) — a muted wallet isn't actually
    being followed right now, even though it's still configured.

    Piggybacks on the TTP sweep's own prices_by_key (same reasoning as the
    kill-switch equity evaluation right above this call site in main()) —
    no separate price-fetch pass just for this.
    """
    now = datetime.now(timezone.utc)
    if now.hour < config.DAILY_SNAPSHOT_TRIGGER_HOUR_UTC:
        return
    if has_snapshot_for_today(now=now):
        return

    breakdown = risk_manager.compute_equity_breakdown(positions, prices_by_key, realized_pnl_total())
    active_traders_followed = len(tracked_traders) - len(muted_traders)
    realized_today = realized_pnl_today(now=now)

    record_daily_snapshot(
        total_equity=breakdown["total_equity"],
        total_cash=breakdown["total_cash"],
        total_unrealized_pnl=breakdown["total_unrealized_pnl"],
        realized_pnl_today=realized_today,
        active_traders_followed=active_traders_followed,
        now=now,
    )
    append_log({"timestamp": now_iso(), "event_type": "daily_portfolio_snapshot",
                "total_equity": breakdown["total_equity"], "total_cash": breakdown["total_cash"],
                "total_unrealized_pnl": breakdown["total_unrealized_pnl"],
                "active_traders_followed": active_traders_followed})
    logger.info(
        f"Daily portfolio snapshot recorded: equity=${breakdown['total_equity']:.2f}, "
        f"cash=${breakdown['total_cash']:.2f}, unrealized=${breakdown['total_unrealized_pnl']:.2f}, "
        f"active_traders_followed={active_traders_followed}"
    )
    # Phase 1 observability (2026-07-31) — piggybacks on this function's own
    # once-per-UTC-day trigger rather than a second schedule (see
    # config.py's note next to TELEGRAM_ERROR_ALERT_THROTTLE_SECONDS).
    telegram_alerts.send_telegram_alert(
        f"\U0001F4CA Daily copybot summary — equity ${breakdown['total_equity']:.2f}, "
        f"realized today ${realized_today:.2f}, "
        f"unrealized ${breakdown['total_unrealized_pnl']:.2f}, "
        f"{active_traders_followed} wallet(s) actively followed"
    )


def record_periodic_pnl_snapshots(positions, prices_by_key, evaluation_epoch, now=None):
    """Persist overall and clean-epoch mark-to-market rows every TTP sweep."""
    now = now or datetime.now(timezone.utc)
    total_realized = realized_pnl_total()
    total_breakdown = risk_manager.compute_equity_breakdown(
        positions, prices_by_key, total_realized,
    )
    all_stats = get_closed_trade_stats_since(0)
    record_pnl_snapshot(
        scope="portfolio", realized_pnl_usd=total_realized,
        unrealized_pnl_usd=total_breakdown["total_unrealized_pnl"],
        open_positions_count=len(positions),
        closed_trades_count=all_stats["closed_count"], win_rate=all_stats["win_rate"],
        now=now,
    )

    clean_positions = {
        key: pos for key, pos in positions.items()
        if int(pos.get("opened_at") or 0) >= int(evaluation_epoch)
    }
    clean_prices = {key: price for key, price in prices_by_key.items() if key in clean_positions}
    clean_realized = realized_pnl_since(evaluation_epoch)
    clean_unrealized = risk_manager.compute_unrealized_pnl(clean_positions, clean_prices)
    clean_stats = get_closed_trade_stats_since(evaluation_epoch)
    record_pnl_snapshot(
        scope="clean_epoch", realized_pnl_usd=clean_realized,
        unrealized_pnl_usd=clean_unrealized,
        open_positions_count=len(clean_positions),
        closed_trades_count=clean_stats["closed_count"], win_rate=clean_stats["win_rate"],
        now=now,
    )
    return total_breakdown


def update_drawdown_warning(risk_state, equity, hwm):
    warning, transition = risk_manager.evaluate_drawdown_warning(
        equity, hwm, risk_state.get("drawdown_warning"), now_iso=now_iso(),
    )
    risk_state["drawdown_warning"] = warning
    if transition == "triggered":
        set_risk_value("drawdown_warning", warning)
        append_log({"timestamp": now_iso(), "event_type": "risk_drawdown_warning_triggered",
                    **warning})
    elif transition == "cleared":
        clear_risk_value("drawdown_warning")
        append_log({"timestamp": now_iso(), "event_type": "risk_drawdown_warning_cleared",
                    "equity": equity, "hwm": hwm,
                    "drawdown_usd": max(0.0, hwm - equity)})
    elif warning:
        # Keep the persisted mark current without re-alerting every 5 min.
        set_risk_value("drawdown_warning", warning)
    return warning, transition


def check_circuit_breaker(trader, nickname, pnl_usd, cost_basis_usd, trader_performance, muted_traders):
    """EV-based circuit breaker (2026-07-26, Rule 35 rewrite -- replaces
    the original consecutive-loss-streak / win-rate-floor design
    entirely). Call this immediately after logging a realized pnl_usd on
    one of OUR OWN closed copy-trades (paper_sell/live_sell) for `trader`.

    Why the rewrite: confirmed live, twice, that a raw streak count
    false-positives on genuinely good wallets -- strict-7 (+44.6%
    EV/dollar-staked over 31 trades) and crypto-specialist-1 were both
    muted by a short losing run inside an otherwise strong track record.
    This fixes that false-positive case, verified against both wallets'
    real histories.

    Does NOT fix the opposite failure mode: geo-anon-3 (83% win rate, -11%
    EV from rare catastrophic losses among many small wins) is still not
    caught -- verified against its real 12-trade history, t=-0.92, nowhere
    near config.CATEGORY_SKIP_Z_CRITICAL. A t-test is powered to detect a
    CONSISTENT negative edge; one huge outlier inflates variance enough
    that a handful of samples can't distinguish a fat-tailed/rare-
    catastrophic-loss wallet from noise. That pattern still needs manual
    review (which is how it was actually caught), not automatic muting --
    stated plainly rather than oversold.

    New trigger: a one-sample t-test (compute_wallet_ev_t_statistic) on
    the wallet's recent REAL per-dollar-staked returns is significantly
    negative (t <= -config.CATEGORY_SKIP_Z_CRITICAL), once at least
    config.MUTE_EV_MIN_SAMPLES real trades are on record.

    Dust filter: a trade with cost_basis_usd below
    config.MUTE_MIN_TRADE_COST_USD is excluded entirely -- not counted as
    a win or loss, never enters the returns window. Confirmed live
    (fed-warren-buffett): a wallet unwinding its own position in many tiny
    increments can generate sub-cent "losses" (e.g. -$0.000006) via our
    proportional copy-sell, which the OLD `pnl_usd > 0` check counted
    identically to a real loss.

    Mutes are recorded into `muted_traders` (persisted via db.py into
    wallet_profile) so they persist across restarts; already-muted
    traders' return history still updates below (in case a future
    rehabilitation mechanism wants it) but no second mute reason is ever
    recorded.

    trader_performance/muted_traders are keyed by trader.lower() (not
    `trader` verbatim) — see db.py's module comment on why: they round-trip
    through wallet_profile.wallet_address, which is always lowercase, so
    keying them lowercase here too means no case-translation is needed on
    load, and a wallet muted under TRACKED_TRADERS_SOURCE="db" stays muted
    after a restart regardless of what casing wallet_profile happens to
    store it in.
    """
    if cost_basis_usd is None or cost_basis_usd < config.MUTE_MIN_TRADE_COST_USD:
        return

    key = trader.lower()
    perf = trader_performance.setdefault(key, {"recent_returns": []})
    perf["recent_returns"].append(pnl_usd / cost_basis_usd)
    perf["recent_returns"] = perf["recent_returns"][-config.MUTE_EV_MIN_SAMPLES:]

    if key in muted_traders:
        return

    if len(perf["recent_returns"]) >= config.MUTE_EV_MIN_SAMPLES:
        t_stat = compute_wallet_ev_t_statistic(perf["recent_returns"])
        if t_stat is not None and t_stat <= -config.CATEGORY_SKIP_Z_CRITICAL:
            reason = (f"statistically significant negative edge (t={t_stat:.2f}) "
                      f"over last {len(perf['recent_returns'])} real (non-dust) trades")
            muted_traders[key] = {"muted_at": now_iso(), "reason": reason}
            logger.warning(f"Trader {nickname} muted: {reason} - Performance check failed.")


def reevaluate_circuit_breakers_on_startup(trader_performance, muted_traders, tracked_by_lower):
    """Reapply the mute rule to persisted windows before the first poll.

    A restart or manual DB roster change must not leave a decisively
    negative wallet active merely because no new close arrives after boot.
    Returns the addresses newly muted during this audit.
    """
    if not config.REEVALUATE_CIRCUIT_BREAKERS_ON_STARTUP:
        return []
    newly_muted = []
    for key, perf in trader_performance.items():
        key = key.lower()
        if key in muted_traders or key not in tracked_by_lower:
            continue
        returns = list(perf.get("recent_returns") or [])[-config.MUTE_EV_MIN_SAMPLES:]
        if len(returns) < config.MUTE_EV_MIN_SAMPLES:
            continue
        t_stat = compute_wallet_ev_t_statistic(returns)
        if t_stat is None or t_stat > -config.CATEGORY_SKIP_Z_CRITICAL:
            continue
        nickname = tracked_by_lower[key][1]
        reason = (
            f"startup re-evaluation: statistically significant negative edge "
            f"(t={t_stat:.2f}) over last {len(returns)} real (non-dust) trades"
        )
        muted_traders[key] = {"muted_at": now_iso(), "reason": reason}
        newly_muted.append(key)
        append_log({"timestamp": now_iso(), "event_type": "trader_muted_startup_audit",
                    "trader_address": key, "trader_nickname": nickname,
                    "t_statistic": t_stat, "sample_count": len(returns), "reason": reason})
        logger.warning(f"Trader {nickname} muted during startup audit: {reason}")
    return newly_muted


def _execute_buy(base_event, key, trader, market_slug, outcome, price, trade_usd,
                  event_slug, score_breakdown, positions, risk_state, price_source=None,
                  wallet_ev_stats=None):
    """Risk-gate + execute + ledger-write for a BUY, extracted 2026-07-24
    (Rule 29) out of process_trade's immediate-copy path so it can ALSO be
    the fill path for sweep_pending_executions()'s dip-and-rebound orders —
    critical that both share exactly one implementation, because
    portfolio-level risk (exposure ceilings, kill switch) is time-sensitive
    and MUST be evaluated fresh at the actual moment of execution, not
    frozen back when a trade was first observed (which, for a
    pending_execution, can be hours earlier). `price` is whatever price
    this specific call is executing against — the live source trade's price
    for the immediate path, or the confirmed rebound fill price for the
    sweep path.

    `price_source` (2026-07-25, 'Dual-Track' dual-detection) is stamped
    onto the resulting position — "wss_estimated" marks a position whose
    dollar cost basis came from our own market read rather than the
    whale's real fill price, which is exactly the flag
    process_trade()'s reconciliation branch looks for later. None
    (default) for every caller that doesn't care (Rule 29's rebound fills,
    any pre-existing call site) — position dicts with no price_source at
    all are simply never reconciliation candidates, which is correct: a
    plain price=None was never possible via the immediate-copy or
    Rule-29 paths to begin with.

    Returns the new decision_journal id on success, None if any gate
    blocked the buy (already logged by the time this returns).
    """
    our_shares = trade_usd / price
    actual_cost_usd = trade_usd

    risk_ok, risk_event_type, risk_reason = risk_manager.check_buy(
        positions, risk_state["market_to_event"], event_slug,
        trade_usd, risk_state["kill_switch"], wallet_address=trader,
        wallet_ev_stats=wallet_ev_stats,
        drawdown_warning=risk_state.get("drawdown_warning"),
        entry_interlock=risk_state.get("entry_interlock"),
    )
    if not risk_ok:
        append_log({**base_event, "event_type": risk_event_type, "reason": risk_reason})
        return None

    # Execute (if live) BEFORE touching our position ledger. If the live
    # order fails, is unmatched, or reverts on-chain, log it as a
    # failed_trade and bail out — we must NOT record a position we never
    # actually acquired.
    if config.LIVE_MODE:
        spread_ok, spread_reason, executable_price = check_spread_tolerance(
            market_slug, outcome, trade_usd, "BUY"
        )
        if not spread_ok:
            append_log({**base_event, "event_type": "skip_wide_spread", "reason": spread_reason})
            return None

        # "Disciplined Taker" price ceiling — reuses the preview price
        # check_spread_tolerance just fetched, no extra network call.
        # BUY-only: exits are never blocked by a risk gate (see
        # check_slippage_ceiling's docstring).
        slippage_ok, slippage_reason = check_slippage_ceiling(price, executable_price, "BUY")
        if not slippage_ok:
            append_log({**base_event, "event_type": "skip_slippage_ceiling", "reason": slippage_reason})
            return None

        if config.ENABLE_ENTRY_SLIPPAGE_CEILING_FAK:
            # Marketable Limit Order (2026-07-26): a true Fill-And-Kill --
            # fills whatever's available immediately up to ceiling_price,
            # cancels the unfilled remainder (confirmed live via `bullpen
            # polymarket limit-buy --help`, not assumed). See
            # compute_entry_slippage_ceiling_pct()/RISK_MANAGEMENT.md Rule
            # 33 for why the ceiling is tied to the live measured edge
            # rather than a fixed percentage.
            live_edge_pct = compute_live_edge_pct()
            ceiling_pct = compute_entry_slippage_ceiling_pct(live_edge_pct)
            ceiling_price = round(price * (1 + ceiling_pct), 4)
            base_event["entry_slippage_ceiling_pct"] = ceiling_pct
            # Requesting trade_usd/ceiling_price shares caps worst-case
            # spend at exactly trade_usd (fully filled at the ceiling) --
            # any better fill price spends less, never more. "we never
            # overpay" per Joey's framing.
            target_shares = round(trade_usd / ceiling_price, 2)
            try:
                response = require_filled(run_bullpen_json([
                    "polymarket", "limit-buy", market_slug, outcome,
                    "--price", str(ceiling_price), "--shares", str(target_shares),
                    "--expiration", "fak", "--yes",
                ]), "live FAK buy")
            except BullpenTimeoutError as e:
                append_log({**base_event, "event_type": "unknown_fill_state", "reason": str(e)})
                return None
            except Exception as e:
                append_log({**base_event, "event_type": "failed_trade", "reason": str(e)})
                return None

            fill_price = extract_fill_price(response)
            filled_shares = extract_filled_shares(response)
            if fill_price and filled_shares is not None:
                # The precise case: a FAK order can legitimately PARTIAL
                # fill (some size at a good price, the rest killed) -- book
                # exactly what filled, never assume the full requested size
                # executed just because a price is present.
                our_shares = filled_shares
                actual_cost_usd = filled_shares * fill_price
            elif fill_price:
                # Response gave a price but not a share count -- can't tell
                # partial from full, so fall back to the same
                # full-budget-assumed accounting the plain market-buy path
                # already used (a pre-existing, not new, limitation), but
                # flagged so this is distinguishable from a precisely-known
                # fill.
                our_shares = trade_usd / fill_price
                base_event["fill_accounting"] = "fak_shares_unknown_assumed_full_budget"
            else:
                base_event["fill_accounting"] = "fallback_source_price"
                base_event["raw_trade_response"] = response
        else:
            max_price = round(price * (1 + config.SLIPPAGE_TOLERANCE), 4)
            try:
                response = require_filled(run_bullpen_json([
                    "polymarket", "buy", market_slug, outcome, str(trade_usd),
                    "--max-price", str(max_price), "--yes",
                ]), "live buy")
            except BullpenTimeoutError as e:
                append_log({**base_event, "event_type": "unknown_fill_state", "reason": str(e)})
                return None
            except Exception as e:
                append_log({**base_event, "event_type": "failed_trade", "reason": str(e)})
                return None

            # Record ACTUAL fill economics when the response exposes them —
            # a fill can be up to SLIPPAGE_TOLERANCE worse than the source
            # price, and a ledger built on the source price drifts from
            # what we really hold (and skews TTP profit math).
            fill_price = extract_fill_price(response)
            if fill_price:
                our_shares = trade_usd / fill_price
            else:
                base_event["fill_accounting"] = "fallback_source_price"
                base_event["raw_trade_response"] = response
    else:
        # Paper mode (2026-07-26): measure what this copy would ACTUALLY
        # fill at right now by walking the live order book, and feed that
        # straight into our_shares/actual_cost_usd -- paper PnL must reflect
        # real fillable prices, not the whale's own (unreachable-to-us) fill
        # price. Falls back to the source price, honestly, ONLY when the
        # book genuinely can't be read (shortfall_status != "ok": thin/
        # closed market with no live liquidity, or a transient fetch
        # failure) -- that fallback is still flagged per-trade via
        # shortfall_status in base_event, so it stays distinguishable from a
        # verified fill rather than silently masquerading as one.
        shortfall = measure_paper_shortfall(
            market_slug, outcome, "BUY", trade_usd, price,
            trade_usd=trade_usd,
        )
        base_event.update(shortfall)

        # Orderbook Liquidity Entry Gate (2026-08-01, opt-in, default
        # False) -- see config.ENABLE_ORDERBOOK_LIQUIDITY_ENTRY_GATE's own
        # docstring for the real-data root cause. Closes a paper/live
        # inconsistency: LIVE_MODE's check_spread_tolerance() above already
        # refuses an entry with no readable book, but paper mode has been
        # quietly falling back to the source price and taking the trade
        # anyway. Skips (never silently discounts) so this bucket's own
        # close_reason data stays as unambiguous as every other skip event.
        if config.ENABLE_ORDERBOOK_LIQUIDITY_ENTRY_GATE and shortfall.get("shortfall_status") != "ok":
            append_log({**base_event, "event_type": "skip_no_orderbook_liquidity",
                        "reason": shortfall.get("shortfall_error") or shortfall.get("shortfall_status")})
            return None

        if shortfall.get("shortfall_status") == "ok":
            our_shares = trade_usd / shortfall["executable_price"]
            actual_cost_usd = trade_usd + shortfall.get("trading_fee_usd", 0.0) \
                + shortfall.get("network_fee_usd", 0.0)

    pos = positions.get(key) or {"shares": 0.0, "cost_basis_usd": 0.0, "avg_entry_price": 0.0,
                                  "buy_count": 0, "peak_profit_pct": 0.0, "last_priced_at": time.time(),
                                  "opened_at": time.time()}
    prior_buy_count = pos.get("buy_count", 1)  # legacy default, same assumption as the cap check above
    new_shares = pos["shares"] + our_shares
    new_cost = pos["cost_basis_usd"] + actual_cost_usd
    pos["avg_entry_price"] = new_cost / new_shares if new_shares else 0.0
    pos["shares"] = new_shares
    pos["cost_basis_usd"] = new_cost
    pos["buy_count"] = prior_buy_count + 1
    if price_source is not None:
        pos["price_source"] = price_source
    positions[key] = pos

    decision_journal_id = append_log({
        **base_event, "event_type": "paper_buy" if not config.LIVE_MODE else "live_buy",
        "our_trade_usd": trade_usd,
        "our_shares": our_shares,
        "position_shares_after": pos["shares"],
        "position_avg_entry_price": pos["avg_entry_price"],
        "score_breakdown": score_breakdown,
    })
    # Consumed by save_state()'s decision_journal<->paper_trade linkage
    # (point 3.2 prerequisite) — persist() runs immediately after this call
    # returns (see main()'s poll loop), so this is always fresh when
    # save_state() reads it.
    pos["last_decision_journal_id"] = decision_journal_id
    return decision_journal_id


def _close_shadow_on_source_sell(base_event, key, market_slug, outcome, price,
                                 fraction_sold, shadow_positions, shadow_kind):
    if shadow_positions is None:
        return False
    shadow_pos = shadow_positions.get(key)
    if not shadow_pos or shadow_pos.get("shares", 0) <= 0:
        return False

    shadow_shares_closed = shadow_pos["shares"] * fraction_sold
    shadow_cost_basis_closed = shadow_pos["cost_basis_usd"] * fraction_sold
    shadow_effective_price = price
    shadow_exit_fee_usd = 0.0
    shadow_shortfall = measure_paper_shortfall(
        market_slug, outcome, "SELL", shadow_shares_closed, price,
        shares=shadow_shares_closed,
    )
    if shadow_shortfall.get("shortfall_status") == "ok":
        shadow_effective_price = shadow_shortfall["executable_price"]
        shadow_exit_fee_usd = shadow_shortfall.get("trading_fee_usd", 0.0) \
            + shadow_shortfall.get("network_fee_usd", 0.0)
    shadow_proceeds_usd = shadow_shares_closed * shadow_effective_price - shadow_exit_fee_usd
    shadow_pnl_usd = shadow_proceeds_usd - shadow_cost_basis_closed

    shadow_pos["shares"] -= shadow_shares_closed
    shadow_pos["cost_basis_usd"] -= shadow_cost_basis_closed
    if shadow_pos["shares"] <= 1e-9:
        del shadow_positions[key]
    else:
        shadow_positions[key] = shadow_pos

    append_log({**base_event, **shadow_shortfall,
                "event_type": f"shadow_{shadow_kind}_sell",
                "fraction_sold": fraction_sold,
                "our_shares_closed": shadow_shares_closed,
                "our_shares_remaining": shadow_positions.get(key, {}).get("shares", 0.0),
                "proceeds_usd": shadow_proceeds_usd,
                "cost_basis_usd": shadow_cost_basis_closed,
                "pnl_usd": shadow_pnl_usd})
    return True


def process_trade(trade, positions, source_positions, source_cost_basis, trader_performance,
                  muted_traders, tracked_by_lower, risk_state, wallet_scores, shadow_positions=None,
                  wallet_ev_stats=None, wallet_mode="track", challenger_positions=None):
    trader = trade["user_address"]
    nickname = nickname_for(trader, tracked_by_lower)
    market_slug = trade.get("market_slug") or ""
    outcome = trade.get("outcome") or ""
    side = trade.get("side", "").upper()
    price = trade.get("price")
    source_size_usd = trade.get("size_usd")
    trade_id = trade.get("trade_id")

    # 'Dual-Track' detection provenance (2026-07-25) — defaults to "polling"
    # since that's the original, always-accurate feed; wss-sourced calls
    # (sweep_live_whale_events -> _handle_matched_whale_event) explicitly
    # set both. Logged on every decision_journal row (item 4 of the
    # dual-track request) and used below to detect a reconciliation
    # opportunity (a polling-observed trade for a key WSS already opened
    # with only an estimated price).
    detected_by = trade.get("detected_by", "polling")
    price_source = trade.get("price_source", "polling")

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
        "detected_by": detected_by,
        # Snapshotted onto every decision_journal row this trade produces
        # (2026-07-23, point 3.2 prerequisite) — see risk_state's
        # active_rule_set_version comment in main().
        "rule_set_version": risk_state.get("active_rule_set_version"),
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
        # Resolved-market guard (2026-07-31) — see config.
        # STALE_TRADE_RESOLUTION_CHECK_SECONDS and _market_already_resolved()'s
        # docstrings. Checked before anything else in this branch (including
        # the muted-trader/Shadow-Rehab path) so a stale replayed trade can't
        # balloon a shadow position either.
        age = _trade_age_seconds(trade)
        if age is not None and age > config.STALE_TRADE_RESOLUTION_CHECK_SECONDS \
                and _market_already_resolved(market_slug, risk_state):
            append_log({**base_event, "event_type": "skip_resolved_market_stale_trade",
                        "reason": f"source trade is {age / 3600:.1f}h old and {market_slug} "
                                  f"has already resolved — refusing to open a fresh position "
                                  f"from a stale/replayed signal"})
            return

        # Per-trade entry-price floor (2026-07-31) — see config.
        # PER_TRADE_ENTRY_PRICE_FLOOR's docstring. Checked before the muted-
        # trader/Shadow-Rehab path too, same reasoning as the guard above:
        # an extreme-tail trade shouldn't balloon a shadow position either.
        if price < config.PER_TRADE_ENTRY_PRICE_FLOOR or price > 1 - config.PER_TRADE_ENTRY_PRICE_FLOOR:
            append_log({**base_event, "event_type": "skip_extreme_tail_entry_price",
                        "reason": f"source price {price} is within "
                                  f"{config.PER_TRADE_ENTRY_PRICE_FLOOR} of $0/$1 — these chronically "
                                  f"thin markets structurally can't be TTP-managed, forcing a "
                                  f"held-to-resolution outcome the 2026-07-25 sizing report found "
                                  f"is net-negative EV"})
            return

        # 'Dual-Track' reconciliation (2026-07-25): a polling-observed BUY
        # (always has an accurate whale price/size, being the purpose-built
        # feed) for a key WSS already opened with only an ESTIMATED dollar
        # valuation (price_source="wss_estimated" — see
        # _handle_matched_whale_event()) corrects that valuation instead of
        # treating this as a second, independent buy signal.
        #
        # Deliberately does NOT touch source_positions[key] (the share
        # count) here: that was already exact the moment WSS recorded it —
        # share_amount comes straight off the on-chain event, never
        # estimated, unlike price/usdc_amount. Re-running the normal
        # `source_positions[key] += source_shares` below for this SAME
        # underlying real-world trade, observed a second time through a
        # completely different feed, would DOUBLE-COUNT the whale's
        # position — the one failure mode this branch exists specifically
        # to avoid. Only source_cost_basis[key] (the dollar valuation) is
        # corrected, using the shares we already know are right times the
        # now-known-accurate price.
        #
        # Known, accepted limitation: this corrects the single most recent
        # wss_estimated buy cleanly. config.MAX_BUYS_PER_TRADER_OUTCOME
        # allows up to 2 buys before the position is capped — if BOTH land
        # via WSS before polling catches up on EITHER, this simple
        # single-correction can't perfectly attribute polling's one real
        # price to only one of the two estimated buys; the correction still
        # applies (better than leaving both estimated), just not exactly
        # apportioned. Not fixed here — flagged, given the narrow
        # reconciliation window this needs (seconds to tens of seconds) and
        # the small cap, this is judged a rare, low-severity gap, not
        # ignored.
        existing_pos = positions.get(key)
        if (detected_by == "polling" and existing_pos is not None
                and existing_pos.get("price_source") == "wss_estimated"):
            old_cost_basis = source_cost_basis.get(key, 0.0)
            real_shares_held = source_positions.get(key, 0.0)
            new_cost_basis = real_shares_held * price
            source_cost_basis[key] = new_cost_basis
            existing_pos["price_source"] = "reconciled"
            positions[key] = existing_pos
            append_log({**base_event, "event_type": "whale_price_reconciled",
                        "old_estimated_cost_basis": old_cost_basis,
                        "reconciled_cost_basis": new_cost_basis,
                        "reconciled_whale_price": price})
            return

        source_shares = source_size_usd / price if source_size_usd else 0.0
        source_positions[key] = source_positions.get(key, 0.0) + source_shares
        # Weighted-average cost basis of the source trader's currently-held
        # shares at this key (2026-07-24, Rule 29) — same
        # weighted-average-on-buy model as positions[key]["cost_basis_usd"]
        # below, mirrored onto the whale's own side. Maintained for EVERY
        # wallet, not just config.LIMIT_ORDER_TRACKED_WALLETS ones: cheap,
        # and keeps the door open for widening the pilot later without a
        # backfill gap.
        source_cost_basis[key] = source_cost_basis.get(key, 0.0) + (source_size_usd or 0.0)

        # Challenger/retiring routing is decided from wallet_profile.status
        # by main()'s periodically-refreshed roster. Challengers can only
        # enter the isolated shadow_challenger ledger; retiring wallets are
        # kept on the feed solely so existing positions still receive SELLs.
        if wallet_mode == "challenger":
            if challenger_positions is not None:
                _execute_shadow_buy(
                    dict(base_event), key, trader, market_slug, outcome, price,
                    challenger_positions, wallet_ev_stats=wallet_ev_stats,
                    shadow_kind="challenger",
                )
            return
        if wallet_mode == "retiring":
            append_log({**base_event, "event_type": "skip_retiring_trader_buy",
                        "reason": "wallet is retiring; exits remain monitored but new BUYs are blocked"})
            return

        # Circuit breaker (kill switch). Checked first -- blocks all new BUY
        # signals from this trader regardless of any other setting, but
        # source_positions tracking above still runs so a SELL against a
        # position we already hold from before the mute still computes the
        # right fraction_sold.
        if trader.lower() in muted_traders:
            append_log({**base_event, "event_type": "skip_muted_trader",
                        "reason": muted_traders[trader.lower()]["reason"]})
            # Shadow Rehab (2026-07-27, Rule 37): a blocked real copy still
            # gets simulated in the isolated shadow ledger, the only way a
            # muted wallet's recovery could ever be observed again (see
            # sweep_shadow_rehab()'s docstring for why).
            if config.ENABLE_SHADOW_REHAB and shadow_positions is not None:
                _execute_shadow_buy(dict(base_event), key, trader, market_slug, outcome, price,
                                     shadow_positions, wallet_ev_stats=wallet_ev_stats)
            return

        # Risk 3 (duplicate exposure) guard. Applies in BOTH paper and live
        # mode so paper runs stay representative of what live would do.
        other_trader = find_cross_trader_position(positions, key, trader, market_slug, outcome)
        if other_trader:
            other_nickname = nickname_for(other_trader, tracked_by_lower)
            logger.info(f"Duplicate position detected {market_slug} ({outcome}), skipping.")
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
                logger.info(f"Duplicate position detected {market_slug} ({outcome}), skipping.")
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
            event_slug, holding_rewards_enabled = resolve_market_event(market_slug)
            if event_slug:
                risk_state["market_to_event"][market_slug] = event_slug
                save_market_event(market_slug, event_slug, holding_rewards_enabled)
            else:
                append_log({**base_event, "event_type": "skip_risk_event_unresolved",
                            "reason": f"could not resolve parent event for {market_slug} — "
                                      f"failing closed rather than bypassing the per-event cap"})
                return

        # Category resolution (2026-07-22, category-specific sizing) — reuses
        # the event_slug just resolved above rather than a second lookup.
        # Unlike the event resolution right above, a missing/failed category
        # never fails closed: it's a sizing refinement (see
        # compute_trade_size_usd()'s two-tier fallback), not a risk gate, so
        # a copy is never skipped just because its category is unknown.
        # Only a real bucket (a config.CATEGORY_TAG_SLUGS match, or "other")
        # is cached/persisted — None (lookup failed) is deliberately never
        # written, so a transient failure gets retried next time this
        # market comes up rather than being stuck unresolved forever.
        category = risk_state["market_to_category"].get(market_slug)
        if category is None:
            category = polymarket_simulator.resolve_market_category(event_slug)
            if category:
                risk_state["market_to_category"][market_slug] = category
                save_market_category(market_slug, category)
        base_event["category"] = category

        # Hard skip (2026-07-23) — statistically significant evidence of
        # harm in this category, a stricter bar than the floor-sizing
        # should_skip_category() sits in front of below. Checked before
        # sizing, not after: a skipped copy shouldn't even compute a size.
        wallet_score_entry = wallet_scores.get(trader.lower())
        if should_skip_category(wallet_score_entry, category):
            append_log({**base_event, "event_type": "skip_poor_category_performance",
                        "reason": f"statistically significant negative PnL for {nickname} in "
                                  f"category={category!r} (one-tailed t-test, "
                                  f"z-critical={config.CATEGORY_SKIP_Z_CRITICAL})"})
            return

        trade_usd = compute_trade_size_usd(wallet_score_entry, price, category)

        # Depth-Aware Trade Sizing (2026-07-28) — always fetches and logs
        # what the depth-capped size WOULD be when it would actually bind,
        # regardless of config.ENABLE_DEPTH_AWARE_TRADE_SIZING; only the
        # actual shrink of trade_usd is gated behind that flag (same
        # "watch what would happen in the log first" rollout as
        # ENABLE_ZOMBIE_POSITION_DUMP). Skipped entirely when trade_usd is
        # already non-positive — no point spending a network call sizing a
        # copy that's about to be skipped anyway.
        if trade_usd > 0:
            book_depth_usd = fetch_book_depth_usd(market_slug, outcome)
            depth_capped_usd = risk_manager.depth_capped_trade_size_usd(
                trade_usd, book_depth_usd, config.TRADE_SIZE_DEPTH_FRACTION
            )
            if depth_capped_usd < trade_usd:
                append_log({**base_event, "event_type": "depth_cap_would_apply",
                            "reason": f"trade_usd ${trade_usd:.2f} exceeds "
                                      f"{config.TRADE_SIZE_DEPTH_FRACTION:.0%} of this market's "
                                      f"visible ask-side book depth (${book_depth_usd:.2f}) — "
                                      f"would clamp to ${depth_capped_usd:.2f} if "
                                      f"ENABLE_DEPTH_AWARE_TRADE_SIZING were on"})
                if config.ENABLE_DEPTH_AWARE_TRADE_SIZING:
                    trade_usd = depth_capped_usd

        # Score snapshot (2026-07-23, point 3.2 prerequisite; updated
        # 2026-07-24 for half-Kelly sizing) — mirrors compute_trade_size_usd()'s
        # own two-tier (win_rate, trade_count) fallback logic (see its
        # docstring) so sizing_tier/kelly_fraction accurately reflect what
        # that call actually used, without changing that function's return
        # type (a bare float, relied on by existing tests). This IS the
        # duplication-drift risk SAFETY.md §16 already flagged as accepted
        # and watched — updated here in the same turn the underlying
        # formula changed, not left stale. Recorded into
        # decision_journal.score_breakdown_json below so a later
        # calibration/structural-break analysis (RISK_MANAGEMENT.md
        # Rule 22) can see exactly what drove this decision, not just its
        # outcome.
        category_score_detail = None
        if category and wallet_score_entry:
            category_score_detail = wallet_score_entry.get("categories", {}).get(category)
        composite_win_rate = wallet_score_entry.get("composite_win_rate") if wallet_score_entry else None
        if category_score_detail and category_score_detail.get("win_rate") is not None:
            sizing_tier = "category"
            snapshot_win_rate = category_score_detail["win_rate"]
            snapshot_trade_count = category_score_detail.get("trade_count") or 0
        elif composite_win_rate is not None:
            sizing_tier = "composite"
            snapshot_win_rate = composite_win_rate
            snapshot_trade_count = (wallet_score_entry.get("composite_trade_count") or 0) if wallet_score_entry else 0
        else:
            sizing_tier = "base"
            snapshot_win_rate = None
            snapshot_trade_count = None
        shrunk_win_rate = None
        kelly_fraction = None
        if snapshot_win_rate is not None:
            shrunk_win_rate = compute_shrunk_win_rate(snapshot_win_rate, snapshot_trade_count, price)
            kelly_fraction = compute_kelly_fraction(shrunk_win_rate, price)
        score_breakdown = {
            "category": category, "category_score_detail": category_score_detail,
            "composite_score": wallet_score_entry.get("composite") if wallet_score_entry else None,
            "trade_size_usd": trade_usd, "sizing_tier": sizing_tier,
            "shrunk_win_rate": shrunk_win_rate, "kelly_fraction": kelly_fraction,
        }

        # Non-positive Kelly edge (2026-07-28) — compute_trade_size_usd()
        # returns exactly 0.0 when the model itself sees zero/negative edge
        # (never for sizing_tier="base", where trade_usd is always the
        # positive BASE_TRADE_USD constant). A softer signal than
        # should_skip_category() above (that one requires STATISTICALLY
        # SIGNIFICANT harm; this fires on any negative point estimate) —
        # see compute_trade_size_usd()'s own docstring for why flooring at
        # MIN_TRADE_USD here was wrong: found live, a wallet kept getting
        # floor-sized copies on a category where 8 of 9 same-day signals
        # had negative Kelly, well before the category's t-stat crossed
        # should_skip_category()'s stricter bar.
        if trade_usd <= 0:
            append_log({**base_event, "event_type": "skip_non_positive_kelly_edge",
                        "reason": f"half-Kelly fraction <= 0 for {nickname} in "
                                  f"category={category!r} (shrunk_win_rate={shrunk_win_rate}, "
                                  f"kelly_fraction={kelly_fraction}) — no assumed edge, no trade",
                        "score_breakdown": score_breakdown})
            return

        # Rule 29 (2026-07-24): tracked wallets in
        # config.LIMIT_ORDER_TRACKED_WALLETS never take the immediate-copy
        # path below at all — instead of executing now, this creates (or,
        # if one is already resting on this exact wallet+market+outcome,
        # ratchets down) a pending_execution row and defers ALL risk-gating
        # and the actual ledger write to sweep_pending_executions(), which
        # only fires on a confirmed dip-and-rebound (see that function's
        # docstring for the full adverse-selection rationale). trade_usd
        # computed above is frozen onto the row as target_usd, exactly as
        # paper_trade rows never retroactively resize on a later rescore.
        if trader.lower() in {w.lower() for w in config.LIMIT_ORDER_TRACKED_WALLETS}:
            vwap = (source_cost_basis.get(key, 0.0) / source_positions[key]
                    if source_positions.get(key, 0.0) > 0 else price)
            existing_pending = get_pending_execution(trader, market_slug, outcome, status="pending")
            if existing_pending:
                new_anchor = compute_anchor_price(existing_pending["anchor_price"], vwap)
                if new_anchor != existing_pending["anchor_price"]:
                    update_pending_execution_anchor(existing_pending["id"], new_anchor)
                append_log({**base_event, "event_type": "limit_order_anchor_updated",
                            "pending_execution_id": existing_pending["id"],
                            "anchor_price": new_anchor, "previous_anchor_price": existing_pending["anchor_price"]})
            else:
                anchor = vwap
                pending_id = create_pending_execution(
                    wallet_address=trader, market_slug=market_slug, outcome=outcome,
                    source_trade_id=trade_id, category=category, anchor_price=anchor,
                    whale_shares_at_creation=source_positions.get(key, 0.0),
                    target_usd=trade_usd, expires_at=int(time.time() + config.LIMIT_ORDER_TTL_SECONDS),
                )
                append_log({**base_event, "event_type": "limit_order_tracked",
                            "pending_execution_id": pending_id, "anchor_price": anchor,
                            "target_usd": trade_usd, "ttl_seconds": config.LIMIT_ORDER_TTL_SECONDS})
            return

        decision_journal_id = _execute_buy(
            base_event, key, trader, market_slug, outcome, price, trade_usd,
            event_slug, score_breakdown, positions, risk_state, price_source=price_source,
            wallet_ev_stats=wallet_ev_stats,
        )
        return

    elif side == "SELL":
        source_shares_held = source_positions.get(key, 0.0)
        source_shares_sold = source_size_usd / price if source_size_usd else 0.0
        fraction_sold = 1.0 if source_shares_held <= 0 else min(1.0, source_shares_sold / source_shares_held)
        source_positions[key] = max(0.0, source_shares_held - source_shares_sold)
        # Cost basis mirrors the same proportional-reduce-on-sell model our
        # own positions[key] uses below — moved ahead of the "do we hold a
        # position ourselves" check (2026-07-24) so it stays accurate even
        # when we hold no copy at all, which pending_execution's
        # whale_still_holding() guard (Rule 29) depends on: a wallet we're
        # tracking for a limit order might sell before we ever buy.
        source_cost_basis[key] = max(0.0, source_cost_basis.get(key, 0.0) * (1 - fraction_sold))

        # Shadow Rehab (2026-07-27, Rule 37): close/reduce a shadow
        # position too, using the SAME fraction_sold -- it's a property of
        # the whale's own trade, independent of whether we hold a real or
        # shadow copy. Runs unconditionally, not gated on "is this wallet
        # currently muted": a wallet reinstated mid-lifecycle can still
        # have a lingering shadow position from before reinstatement that
        # needs to wind down correctly rather than being silently
        # orphaned. Always paper-priced (measure_paper_shortfall) even
        # when config.LIVE_MODE is on -- a shadow trade is inherently a
        # simulation regardless of the real bot's mode.
        if config.ENABLE_SHADOW_REHAB:
            _close_shadow_on_source_sell(
                base_event, key, market_slug, outcome, price, fraction_sold,
                shadow_positions, "rehab",
            )
        _close_shadow_on_source_sell(
            base_event, key, market_slug, outcome, price, fraction_sold,
            challenger_positions, "challenger",
        )
        if wallet_mode == "challenger":
            return

        pos = positions.get(key)
        if not pos or pos["shares"] <= 0:
            append_log({**base_event, "event_type": "skip_sell_no_position",
                        "reason": "we hold no simulated position in this market/outcome"})
            return

        shares_closed = pos["shares"] * fraction_sold
        cost_basis_closed = pos["cost_basis_usd"] * fraction_sold
        effective_price = price
        exit_fee_usd = 0.0

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
            # Paper mode (2026-07-26): same measurement as the BUY side, and
            # wired into what this close actually books for the same reason
            # -- realized PnL is booked HERE, at the sell, so an unwired
            # exit price would leave PnL just as optimistic as an unwired
            # entry price did. Same honest fallback rule as the BUY side:
            # only trust the measurement when shortfall_status == "ok".
            shortfall = measure_paper_shortfall(
                market_slug, outcome, "SELL", shares_closed, price,
                shares=shares_closed,
            )
            base_event.update(shortfall)
            if shortfall.get("shortfall_status") == "ok":
                effective_price = shortfall["executable_price"]
                exit_fee_usd = shortfall.get("trading_fee_usd", 0.0) \
                    + shortfall.get("network_fee_usd", 0.0)

        proceeds_usd = shares_closed * effective_price - exit_fee_usd
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
        check_circuit_breaker(trader, nickname, pnl_usd, cost_basis_closed, trader_performance, muted_traders)

    else:
        append_log({**base_event, "event_type": "error",
                    "error": f"unrecognized side: {side}"})


def fetch_direct_feed(executor, wallet_addresses, known_trade_ids=None):
    """Fetches recent trade activity for every tracked wallet directly from
    Polymarket's own public Data API (polymarket_data_api.py) — the tracking
    feed as of the 2026-07-22 cutover (docs/copy-trading/RISK_MANAGEMENT.md
    Rule 14), replacing `bullpen tracker feed` entirely. No bullpen session
    or auth is involved in this call at all, so fetch_feed_with_auth_
    recovery()'s halt-and-recover logic (Rule 13) no longer applies to
    tracking specifically — see that function's own docstring, kept below
    since bullpen is still used elsewhere in this file.

    `executor` must be the ONE long-lived ThreadPoolExecutor created once in
    main() (see make_persistent_executor()) — passing a fresh executor every
    call would lose the persistent-connection speedup Rule 14 measured
    (0.4-0.5s/cycle warm vs. ~2.3s/cycle cold).

    A single wallet's fetch failing does not abort the poll cycle — logged
    as a normal error event; that wallet is simply retried on the very next
    cycle, same resilience shape as everything else in this loop.

    `known_trade_ids` is the current in-memory dedup boundary. Passing it to
    the API client stops per-wallet offset pagination once a page overlaps
    already-processed history: a real >20-trade burst still pages until it
    reaches that boundary, while an ordinary poll does not replay up to 200
    old rows into the main loop (2026-08-05 P0).

    Returns {"trades": [...]} — same shape bullpen's tracker-feed response
    had, so every line downstream (dedup against seen_trade_ids, sort,
    process_trade) remains unchanged.
    """
    result = fetch_all_wallets_concurrent(
        wallet_addresses,
        limit=config.DIRECT_API_PER_WALLET_LIMIT,
        executor=executor,
        known_trade_ids=known_trade_ids,
    )
    for err in result["errors"]:
        append_log({"timestamp": now_iso(), "event_type": "error",
                    "error": f"direct feed fetch failed for wallet {err['wallet_address']}: {err['error']}"})
    return {"trades": result["trades"]}


# Consecutive auth-recheck failures while halted, used ONLY to throttle
# repeated log lines (added 2026-07-21, same pattern as
# _closeout_fetch_failures above). The first halt is always logged loudly;
# if `bullpen login` isn't run for a while, a reminder (with the running
# duration) logs every 30th recheck (~1 hour at AUTH_RECHECK_INTERVAL_SECONDS)
# rather than spamming one row every 120s for however long the session stays
# dead.
_auth_halt_rechecks = 0


def fetch_feed_with_auth_recovery():
    """NOT CALLED BY THE MAIN LOOP AS OF THE 2026-07-22 CUTOVER — tracking now
    uses fetch_direct_feed() above, which has no bullpen/auth dependency at
    all. Left in place, still tested (test_bullpen_client.py): the halt-and-
    recover pattern this implements remains valid and could still matter for
    OTHER bullpen call sites in this file (execution, shortfall previews,
    closeout sweeps) that still depend on a live bullpen session.

    Wraps the tracker-feed fetch with auth-failure detection and a slow,
    patient recovery loop — replaces the previous behavior where an expired
    bullpen session was silently retried every POLL_INTERVAL_SECONDS (30s)
    for as long as it stayed broken, which is exactly what produced 264
    identical error rows over ~2 hours during the 2026-07-18 incident this
    was built in response to.

    On a BullpenAuthError: logs ONE clear "auth_halted" event, persists
    bot_risk_state["auth_halt"] (mirrors the kill_switch pattern — visible
    to any future status/dashboard check, not just this process's own log),
    then waits at the much slower AUTH_RECHECK_INTERVAL_SECONDS cadence,
    re-attempting only this same feed call as the recovery probe, until
    either it succeeds (session restored — logs "auth_recovered", clears
    the flag, returns the fresh feed) or shutdown is requested (returns
    None — caller must treat that as "no work to do this cycle").

    Deliberately does NOT attempt any non-interactive self-repair (e.g.
    `bullpen fix --refresh`): live-checked against bullpen's own diagnostics
    while a session was genuinely dead, and the CLI itself reported
    `resolution_owner: "user"` / `next_action: "bullpen login"` — a real
    login is the only thing that fixes this, so the bot's job is to notice
    fast, stop wasting cycles, and resume the instant a human has fixed it.
    """
    global _auth_halt_rechecks
    try:
        return run_bullpen_json(
            ["tracker", "feed", "--limit", str(config.FEED_LIMIT)],
            retries=config.FEED_FETCH_RETRIES,
            retry_delay=config.FEED_FETCH_RETRY_DELAY_SECONDS,
            timeout=config.FEED_POLL_TIMEOUT_SECONDS,
        )
    except BullpenAuthError as e:
        halted_at = now_iso()
        append_log({"timestamp": halted_at, "event_type": "auth_halted",
                    "error": str(e)})
        logger.error(f"BOT HALTED — bullpen session appears dead ({e}). "
                     f"Run `bullpen login` to resume; rechecking every "
                     f"{config.AUTH_RECHECK_INTERVAL_SECONDS}s in the meantime.")
        set_risk_value("auth_halt", {"triggered_at": halted_at, "error": str(e)})
        _auth_halt_rechecks = 0

        while not SHUTDOWN_REQUESTED:
            deadline = time.time() + config.AUTH_RECHECK_INTERVAL_SECONDS
            while not SHUTDOWN_REQUESTED and time.time() < deadline:
                time.sleep(1)
            if SHUTDOWN_REQUESTED:
                return None

            try:
                feed = run_bullpen_json(
                    ["tracker", "feed", "--limit", str(config.FEED_LIMIT)],
                    retries=config.FEED_FETCH_RETRIES,
                    retry_delay=config.FEED_FETCH_RETRY_DELAY_SECONDS,
                    timeout=config.FEED_POLL_TIMEOUT_SECONDS,
                )
            except BullpenAuthError as recheck_error:
                _auth_halt_rechecks += 1
                if _auth_halt_rechecks % 30 == 0:
                    minutes = _auth_halt_rechecks * config.AUTH_RECHECK_INTERVAL_SECONDS // 60
                    append_log({"timestamp": now_iso(), "event_type": "auth_halted",
                                "consecutive_rechecks": _auth_halt_rechecks,
                                "error": f"still halted after ~{minutes}min "
                                         f"(repeats throttled): {recheck_error}"})
                continue
            except Exception as other_error:
                # Not an auth failure — log and keep waiting at the slow
                # cadence rather than falling back to hammering every 30s.
                append_log({"timestamp": now_iso(), "event_type": "error",
                            "error": f"auth recheck hit a non-auth error, "
                                     f"still halted: {other_error}"})
                continue

            recovered_at = now_iso()
            append_log({"timestamp": recovered_at, "event_type": "auth_recovered",
                        "halted_at": halted_at,
                        "consecutive_rechecks": _auth_halt_rechecks})
            logger.info(f"Bullpen session restored — resuming normal polling.")
            clear_risk_value("auth_halt")
            _auth_halt_rechecks = 0
            return feed
        return None


_UNKNOWN_WALLET_KEY = "__unknown__"


def _mark_trade_seen(seen_by_wallet, seen_set, wallet_address, trade_id):
    """Records trade_id as seen for wallet_address, keeping seen_set (the
    flat `tid not in seen_set` lookup the main loop already used) in sync
    with each wallet's OWN bounded deque (config.SEEN_TRADE_IDS_PER_WALLET_CAP)
    instead of one shared global cap — see that constant's docstring for the
    bug this replaces: a busy wallet's volume evicting a quiet wallet's older
    trade_ids from a single shared cap, silently un-deduping them the next
    time bot.py restarts. wallet_address=None (pre-2026-07-31 legacy rows
    with no attribution) is grouped into one _UNKNOWN_WALLET_KEY bucket so it
    can't grow unbounded either.

    Idempotent (2026-07-31 fix): a no-op if trade_id is already in seen_set.
    Found live in production: a duplicate trade_id within the SAME raw feed
    batch (the caller's new_trades filter only checks seen_set once, before
    the per-trade loop, not per-iteration) used to append a second copy of
    the same id into dq. deque(maxlen=N) then evicts the FIRST copy on some
    later append and seen_set.discard()'s it — while the second, still-live
    copy sat deeper in the deque, un-eviction-tracked, silently un-deduping
    an already-processed trade the moment the first copy's slot rotated out.
    Confirmed live: a single resolved-market trade for
    will-spain-win-the-2026-fifa-world-cup-963 (and 19 other already-settled
    markets) got re-opened as a fresh paper_buy roughly hourly for 14+
    hours, each one immediately closed out again by the next hourly
    resolution sweep — corrupting realized_pnl_total() (which directly feeds
    risk_manager.compute_equity(), i.e. the drawdown kill switch) with
    repeated phantom PnL for trades that had already been booked.
    """
    if trade_id in seen_set:
        return
    wallet_key = (wallet_address or "").lower() or _UNKNOWN_WALLET_KEY
    dq = seen_by_wallet.setdefault(wallet_key, deque(maxlen=config.SEEN_TRADE_IDS_PER_WALLET_CAP))
    if len(dq) == dq.maxlen:
        seen_set.discard(dq[0])
    dq.append(trade_id)
    seen_set.add(trade_id)


def main():
    # Phase 1 observability (2026-07-31) — exposes copybot_events_total
    # (db.py) and the three gauges above on config.METRICS_PORT for
    # Prometheus to scrape (monitoring/docker-compose.yml, same EC2 box,
    # never a publicly-exposed port). Caught, not left to propagate: a
    # port conflict here (e.g. the exact stray-duplicate-process failure
    # mode from the 2026-07-29 outage) must degrade to "no metrics this
    # run," never block real trading from starting at all.
    try:
        start_http_server(config.METRICS_PORT)
    except OSError as e:
        logger.warning(f"Prometheus metrics server failed to start on port "
                        f"{config.METRICS_PORT}: {e} — continuing without it.")

    # Track wallets execute normal paper copies. Challenger and retiring
    # wallets are also polled, but main() routes them to shadow-only or
    # exit-only handling. The roster is refreshed every five minutes below
    # so a daily challenger enrollment or Telegram approval does not need a
    # process restart.
    tracked_traders = get_tracked_traders()
    tracked_by_lower = {addr.lower(): (addr, nick) for addr, nick in tracked_traders.items()}
    monitored_traders = get_monitored_noncopying_traders()
    monitored_by_lower = {
        addr.lower(): (addr, detail["nickname"], detail["mode"])
        for addr, detail in monitored_traders.items()
    }
    all_by_lower = dict(tracked_by_lower)
    all_by_lower.update({key: (value[0], value[1]) for key, value in monitored_by_lower.items()})
    wallet_addresses = [value[0] for value in all_by_lower.values()]

    # Published to bot_risk_state (2026-07-27) so the Next.js dashboard has
    # a real, always-current answer to "which wallets are actually being
    # copied right now" -- wallet_profile.status is TS-scorer-owned and
    # answers a completely different question (that pipeline's own
    # recommendation, not whether bot.py's config.TRACKED_TRADERS actually
    # includes this wallet); confirmed live that they'd drifted apart
    # (only 2 of 17 real tracked wallets happened to have status='track').
    # Refreshed alongside the DB roster below.
    set_risk_value("tracked_traders", {addr.lower(): nick for addr, nick in tracked_traders.items()})

    # Confidence-weighted position sizing (2026-07-22) — see config.py's
    # BASE/MIN/MAX_TRADE_USD and compute_trade_size_usd(). Independent of
    # TRACKED_TRADERS_SOURCE and fetched once at startup for the same
    # restart-to-pick-up-changes reason as tracked_traders above: a wallet
    # rescored mid-session keeps sizing off its old score until restart.
    wallet_scores = get_wallet_composite_scores()

    # Automatic EV-scaled per-wallet exposure cap (2026-07-31, replacing
    # config.VIP_WALLET_EXPOSURE_CAP_USD's manual curation as the default
    # mechanism — see risk_manager.wallet_exposure_cap_usd()). Same fetch-
    # once-at-startup convention as wallet_scores above.
    wallet_ev_stats = get_wallet_realized_ev_stats()

    # ONE long-lived executor for the whole process (2026-07-22 cutover, Rule
    # 14) — created once here, passed into every fetch_direct_feed() call
    # below, NEVER recreated per-cycle. Recreating it every poll would
    # discard its worker threads' persistent HTTPS connections and lose the
    # measured 0.4-0.5s/cycle (vs ~2.3s/cycle cold) speedup entirely. Sized
    # to comfortably cover every tracked wallet concurrently, not a fixed
    # guess, so this stays correct if the tracked list grows later. Also
    # passed into check_trailing_take_profit() (2026-07-31) to parallelize
    # its own per-position price fetches — same executor, different cadence
    # (every TRAILING_TP_CHECK_INTERVAL_SECONDS instead of every poll), no
    # conflict: submitted work just queues if all workers are briefly busy.
    direct_feed_executor = make_persistent_executor(max_workers=max(len(wallet_addresses), 10))

    # Portfolio-risk state (risk_manager.py): the latched kill switch and
    # equity high-water mark survive restarts via bot_risk_state, and the
    # market->event memo via bot_market_event. Mutated in place by
    # process_trade (new event resolutions) and the post-sweep equity
    # evaluation below; every mutation is persisted through db.py the
    # moment it happens. market_to_category (2026-07-22, category-specific
    # sizing) lives in the same risk_state dict for convenience even though
    # it isn't itself a risk gate — same bot_market_event table, same
    # resolve-once-and-cache shape as market_to_event.
    risk_state = {
        "kill_switch": get_risk_value("kill_switch"),
        "entry_interlock": get_risk_value("entry_interlock"),
        "drawdown_warning": get_risk_value("drawdown_warning"),
        "equity_hwm": get_risk_value("equity_hwm"),
        "market_to_event": load_market_events(),
        "market_to_category": load_market_categories(),
        # Priority 4 (2026-07-26), theta-decay TP activation — same
        # resolve-once-and-cache shape as market_to_event/market_to_category,
        # only ever populated when config.ENABLE_THETA_DECAY_TP_ACTIVATION
        # is on (see check_trailing_take_profit()).
        "market_to_end_date": load_market_end_dates(),
        # Active TS wallet-scorer rule_set version (2026-07-23, point 3.2
        # prerequisite) — read once at startup and cached, same
        # restart-to-pick-up-changes reasoning as wallet_scores/
        # tracked_traders above: rule_set only changes when scoreWallets.ts
        # bumps DEFAULT_RULES.version, not worth a per-trade DB read.
        # Snapshotted onto every decision_journal row (base_event below) so
        # a later structural-break analysis can tell "the wallet's edge
        # shifted" apart from "we changed the scoring formula out from
        # under it." None on a fresh DB with no active rule_set yet.
        "active_rule_set_version": get_active_rule_set_version(),
        # Resolved-market guard (2026-07-31 fix, belt-and-suspenders
        # alongside the _mark_trade_seen() idempotency fix above): once a
        # market_slug is confirmed resolved, remembered here for the rest
        # of this process's life so a stale trade for it (from any dedup
        # gap, present or future) can never open a fresh position again.
        # In-memory only, not persisted — a restart just re-pays one cheap
        # metadata fetch the next time that market_slug is (still
        # incorrectly) offered as a buy signal, which is the rare case this
        # guard exists for in the first place.
        "resolved_markets": set(),
    }
    evaluation_epoch = get_or_create_evaluation_epoch()
    METRIC_KILL_SWITCH_ACTIVE.set(1 if risk_state["kill_switch"] else 0)
    METRIC_ENTRY_INTERLOCK_ACTIVE.set(1 if risk_state["entry_interlock"] else 0)

    logger.info(f"Copybot starting — mode={'LIVE' if config.LIVE_MODE else 'PAPER'}, "
                f"source={config.TRACKED_TRADERS_SOURCE}, "
                f"tracking {len(tracked_traders)} trader(s), monitoring "
                f"{len(monitored_traders)} challenger/retiring wallet(s), "
                f"${config.MIN_TRADE_USD}-${config.MAX_TRADE_USD}/trade (base ${config.BASE_TRADE_USD} "
                f"if unscored), polling every {config.POLL_INTERVAL_SECONDS}s")
    if risk_state["kill_switch"]:
        ks = risk_state["kill_switch"]
        logger.warning(f"WARNING: drawdown kill switch is LATCHED (since {ks.get('triggered_at')}: "
                       f"{'; '.join(ks.get('reasons', []))}) — all new BUYs are halted. "
                       f"Run reset_kill_switch.py after review to resume buying.")
    if risk_state["entry_interlock"]:
        interlock = risk_state["entry_interlock"]
        reasons = interlock.get("reasons", []) if isinstance(interlock, dict) else []
        logger.warning(
            f"WARNING: execution-integrity entry interlock is active: "
            f"{'; '.join(str(reason) for reason in reasons) or 'unknown reason'} — "
            f"new BUYs halted; exits continue."
        )
    if risk_state["drawdown_warning"]:
        warning = risk_state["drawdown_warning"]
        logger.warning(
            f"WARNING: drawdown soft pause is active — equity "
            f"${warning.get('equity', 0):.2f}, drawdown ${warning.get('drawdown_usd', 0):.2f}; "
            f"new BUYs paused, exits continue."
        )

    signal.signal(signal.SIGTERM, _handle_sigterm)

    state = load_state()
    # Per-wallet dedup (2026-07-31, replacing a single global
    # deque(maxlen=2000)) — see config.SEEN_TRADE_IDS_PER_WALLET_CAP's
    # docstring for the bug this fixes: a busy wallet's volume evicting a
    # quiet wallet's older trade_ids from a single shared cap, which then
    # resurfaced as "new" copies of month-old trades on the next restart.
    # seen_set stays the flat, fast `tid not in seen_set` lookup every poll
    # already used — _mark_trade_seen() keeps it in sync with each wallet's
    # own bounded deque instead of one global one.
    seen_trade_ids_by_wallet = {}
    seen_set = set()
    for entry in state["seen_trade_ids"]:
        _mark_trade_seen(seen_trade_ids_by_wallet, seen_set,
                          entry.get("wallet_address"), entry["trade_id"])
    positions = state["positions"]
    source_positions = state["source_positions"]
    source_cost_basis = state["source_cost_basis"]
    trader_performance = state["trader_performance"]
    muted_traders = state["muted_traders"]
    # Shadow Rehab (2026-07-27, Rule 37) — its own isolated ledger, loaded/
    # persisted separately from save_state()'s positions (which stay
    # strategy="bot_filtered" only, see load_shadow_positions()'s own
    # docstring for why this never touches real risk/exposure).
    shadow_positions = load_shadow_positions()
    challenger_positions = load_shadow_positions("shadow_challenger")

    def persist():
        seen_flat = [
            {"trade_id": tid, "wallet_address": (wallet if wallet != _UNKNOWN_WALLET_KEY else None)}
            for wallet, dq in seen_trade_ids_by_wallet.items() for tid in dq
        ]
        save_state({"seen_trade_ids": seen_flat, "positions": positions,
                    "source_positions": source_positions, "source_cost_basis": source_cost_basis,
                    "trader_performance": trader_performance, "muted_traders": muted_traders})
        save_shadow_positions(shadow_positions)
        save_shadow_positions(challenger_positions, "shadow_challenger")

    newly_muted = reevaluate_circuit_breakers_on_startup(
        trader_performance, muted_traders, tracked_by_lower,
    )
    if newly_muted:
        persist()

    # load_state() (db.py) is now the sole source of truth regardless of
    # whether state.json exists on disk — bootstrap purely off whether it
    # returned any seen_trade_ids, not file presence (checking
    # os.path.exists(config.STATE_PATH) here would incorrectly re-bootstrap
    # on every restart once state.json is retired).
    bootstrap = not state["seen_trade_ids"]
    if bootstrap:
        try:
            feed = fetch_direct_feed(direct_feed_executor, wallet_addresses,
                                     known_trade_ids=seen_set)
            trades = feed.get("trades", [])
            for t in trades:
                tid = t.get("trade_id")
                if tid:
                    _mark_trade_seen(seen_trade_ids_by_wallet, seen_set, t.get("user_address"), tid)
            append_log({"timestamp": now_iso(), "event_type": "bootstrap",
                        "note": f"baseline-skipped {len(trades)} pre-existing trades; "
                                f"only trades after this point will be copied"})
            persist()
        except Exception as e:
            append_log({"timestamp": now_iso(), "event_type": "error",
                        "error": f"bootstrap failed: {e}"})

    last_ttp_sweep = 0.0
    last_closeout_sweep = 0.0
    last_zombie_sweep = 0.0
    last_prune_sweep = 0.0
    last_roster_refresh = time.time()

    while not SHUTDOWN_REQUESTED:
        try:
            if time.time() - last_roster_refresh >= config.ROSTER_REFRESH_INTERVAL_SECONDS:
                refreshed_tracked = get_tracked_traders()
                refreshed_monitored = get_monitored_noncopying_traders()
                tracked_traders = refreshed_tracked
                monitored_traders = refreshed_monitored
                tracked_by_lower = {
                    addr.lower(): (addr, nick) for addr, nick in tracked_traders.items()
                }
                monitored_by_lower = {
                    addr.lower(): (addr, detail["nickname"], detail["mode"])
                    for addr, detail in monitored_traders.items()
                }
                all_by_lower = dict(tracked_by_lower)
                all_by_lower.update({
                    key: (value[0], value[1]) for key, value in monitored_by_lower.items()
                })
                challenger_ledger_wallets = set(tracked_by_lower) | {
                    key for key, value in monitored_by_lower.items() if value[2] == "challenger"
                }
                for position_key_value in list(challenger_positions):
                    wallet_key = position_key_value.split("|", 1)[0].lower()
                    if wallet_key not in challenger_ledger_wallets:
                        challenger_positions.pop(position_key_value, None)
                wallet_addresses = [value[0] for value in all_by_lower.values()]
                wallet_scores = get_wallet_composite_scores()
                wallet_ev_stats = get_wallet_realized_ev_stats()
                set_risk_value(
                    "tracked_traders",
                    {addr.lower(): nick for addr, nick in tracked_traders.items()},
                )
                last_roster_refresh = time.time()

            # Direct Polymarket API, no bullpen/auth involved (Rule 14,
            # 2026-07-22 cutover) — a single wallet's fetch failure is
            # already handled inside fetch_direct_feed (logged, doesn't
            # raise), so this call itself can't fail the way the old
            # bullpen-backed fetch could.
            feed = fetch_direct_feed(direct_feed_executor, wallet_addresses,
                                     known_trade_ids=seen_set)
            trades = feed.get("trades", [])
            new_trades = [t for t in trades if t.get("trade_id") not in seen_set]
            # SELL/redeem signals sort ahead of BUY signals within the same
            # poll cycle (2026-07-26) — NOT because a buy can structurally
            # block a sell (it can't: risk_manager.check_buy never gates
            # sells, confirmed by direct code read, so nothing in
            # process_trade()'s SELL branch waits on anything a BUY does).
            # The real, narrower benefit: if a sell and an unrelated buy
            # land in the SAME 30s batch, processing the sell first means
            # any capital it frees is already reflected in `positions`
            # before the buy's exposure-ceiling check runs against it,
            # instead of the reverse order costing that buy a needless
            # skip_risk_exposure_ceiling this cycle. Chronological order
            # (timestamp) is preserved as the secondary key — this is a
            # stable sort, so trades within the same side still process in
            # the order they actually happened.
            new_trades.sort(key=lambda t: (0 if t.get("side", "").upper() == "SELL" else 1,
                                            t.get("timestamp", "")))

            for trade in new_trades:
                if SHUTDOWN_REQUESTED:
                    break
                # 'Dual-Track' detection provenance (2026-07-25) — this feed
                # always carries the whale's real price/size (it's the
                # purpose-built polling API), so it never sets price_source;
                # process_trade() defaults an untagged trade to "polling"
                # itself. detected_by is what item 4 of the dual-track
                # request (log which path caught a trade first) and the
                # reconciliation branch both key off.
                trade["detected_by"] = "polling"
                tid = trade.get("trade_id")
                if tid:
                    # Re-check seen_set per-iteration, not just via the
                    # new_trades filter above (2026-07-31 fix): if the SAME
                    # trade_id appears twice in one raw feed batch, both
                    # copies pass that one-time pre-loop filter (neither is
                    # in seen_set yet when it runs) and would otherwise both
                    # reach process_trade() below — a real double-copy of
                    # the same trade in a single poll cycle. See
                    # _mark_trade_seen()'s docstring for the production
                    # incident (will-spain-win-the-2026-fifa-world-cup-963
                    # and 19 other resolved markets, phantom-recopied
                    # hourly) this combines with to close.
                    if tid in seen_set:
                        continue
                    _mark_trade_seen(seen_trade_ids_by_wallet, seen_set, trade.get("user_address"), tid)
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
                trader_key = trader_addr.lower()
                if trader_key in tracked_by_lower:
                    wallet_mode = "track"
                elif trader_key in monitored_by_lower:
                    wallet_mode = monitored_by_lower[trader_key][2]
                else:
                    append_log({"timestamp": now_iso(), "event_type": "skip_untracked_trader",
                                "source_trade_id": tid, "trader_address": trader_addr,
                                "reason": f"trader not in active tracked-traders source "
                                          f"(TRACKED_TRADERS_SOURCE={config.TRACKED_TRADERS_SOURCE!r})"})
                    persist()
                    continue

                try:
                    process_trade(trade, positions, source_positions, source_cost_basis,
                                  trader_performance, muted_traders, all_by_lower,
                                  risk_state, wallet_scores, shadow_positions=shadow_positions,
                                  wallet_ev_stats=wallet_ev_stats, wallet_mode=wallet_mode,
                                  challenger_positions=challenger_positions)
                except Exception as e:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "source_trade_id": tid,
                                "trader_address": trade.get("user_address"),
                                "error": str(e)})
                # Persist again so a live fill is durable before we even
                # look at the next trade.
                persist()

            # Rule 29 (2026-07-24) dip-and-rebound sweep — run every cycle,
            # not gated by an interval like the sweeps below: the whole
            # point is catching a rebound (and a whale exit) as soon as it
            # happens, so extra staleness here directly works against the
            # feature. Cheap in practice: only wallets in
            # config.LIMIT_ORDER_TRACKED_WALLETS ever create a row, so this
            # is a no-op most cycles for the current strict-4-only pilot.
            if not SHUTDOWN_REQUESTED:
                try:
                    sweep_pending_executions(positions, source_positions, source_cost_basis,
                                              tracked_by_lower, risk_state, wallet_scores,
                                              wallet_ev_stats=wallet_ev_stats)
                except Exception as e:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "error": f"pending execution sweep failed: {e}"})
                persist()

            # Consumer sweep for wss_listener.py/token_sync_worker.py's
            # producer tables (live_whale_event/token_registry) — run every
            # cycle, same zero-latency reasoning as the sweep above.
            # Idempotency lives inside sweep_live_whale_events() itself
            # (each event's consumed_at is stamped in its own try/finally),
            # so a failure here can't cause an event to be silently dropped
            # OR double-processed — this outer try/except only guards
            # against sweep_live_whale_events() itself failing before it
            # even gets to iterate (e.g. the DB query raising).
            if not SHUTDOWN_REQUESTED:
                try:
                    sweep_live_whale_events(positions, source_positions, source_cost_basis,
                                             trader_performance, muted_traders, tracked_by_lower,
                                             risk_state, wallet_scores, shadow_positions=shadow_positions,
                                             wallet_ev_stats=wallet_ev_stats)
                except Exception as e:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "error": f"live whale event sweep failed: {e}"})
                persist()

            # Priority 3 (2026-07-26) patient-exit sweep — inert unless
            # config.ENABLE_PATIENT_EXIT_PEGGING is on (start_patient_exit()
            # is the only place that ever creates a pending_exit_order row,
            # and it's itself gated by the same flag), so this is a cheap
            # no-op query most cycles under the current default. Run every
            # cycle, not gated by an interval — the whole point is
            # reacting to reprice/fallback deadlines promptly, same
            # reasoning as the sweeps above.
            if not SHUTDOWN_REQUESTED:
                try:
                    sweep_pending_exit_orders(positions, trader_performance, muted_traders)
                except Exception as e:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "error": f"pending exit order sweep failed: {e}"})
                persist()

            # Shadow patient-exit sweep (2026-08-01) — paper-only comparison
            # data, no `positions` touched, so no persist() needed after.
            # Runs every cycle for the same reprice/timeout-promptness
            # reasoning as the real sweep above.
            if not SHUTDOWN_REQUESTED:
                try:
                    sweep_shadow_patient_exits()
                except Exception as e:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "error": f"shadow patient exit sweep failed: {e}"})

            # Shadow Rehab (2026-07-27, Rule 37) — cheap (one query per
            # currently-muted wallet), run every cycle rather than gated by
            # an interval, same reasoning as the sweeps above: no benefit
            # to delaying a reinstatement once the evidence is there.
            if not SHUTDOWN_REQUESTED:
                try:
                    sweep_shadow_rehab(muted_traders)
                except Exception as e:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "error": f"shadow rehab sweep failed: {e}"})
                persist()

            now = time.time()
            if not SHUTDOWN_REQUESTED and now - last_ttp_sweep >= config.TRAILING_TP_CHECK_INTERVAL_SECONDS:
                last_ttp_sweep = now
                try:
                    prices_by_key = check_trailing_take_profit(
                        positions, trader_performance, muted_traders, tracked_by_lower,
                        market_to_end_date=risk_state["market_to_end_date"],
                        executor=direct_feed_executor)
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
                        # Phase 1 observability (2026-07-31) — set on every
                        # evaluation (not just when something changes) so
                        # Grafana shows a continuous series, not gaps.
                        METRIC_EQUITY_USD.set(equity)
                        METRIC_OPEN_POSITIONS.set(len(positions))
                        if new_hwm != risk_state["equity_hwm"]:
                            risk_state["equity_hwm"] = new_hwm
                            set_risk_value("equity_hwm", new_hwm)
                        update_drawdown_warning(risk_state, equity, new_hwm)
                        if triggers and not risk_state["kill_switch"]:
                            kill_switch = {"triggered_at": now_iso(), "reasons": triggers,
                                           "equity": equity, "hwm": new_hwm}
                            risk_state["kill_switch"] = kill_switch
                            set_risk_value("kill_switch", kill_switch)
                            METRIC_KILL_SWITCH_ACTIVE.set(1)
                            append_log({"timestamp": now_iso(),
                                        "event_type": "risk_kill_switch_triggered",
                                        **kill_switch})
                            logger.critical(f"RISK KILL SWITCH TRIGGERED — halting all new BUYs: "
                                             f"{'; '.join(triggers)}. Run reset_kill_switch.py "
                                             f"after review to resume.")
                    except Exception as e:
                        append_log({"timestamp": now_iso(), "event_type": "error",
                                    "error": f"risk equity evaluation failed: {e}"})

                    # Daily portfolio snapshot (2026-07-28, Grafana personal
                    # dashboard) — piggybacks on this same TTP sweep's
                    # prices_by_key for the same reason the kill-switch
                    # evaluation above does; no separate price-fetch pass.
                    # Idempotent (db.has_snapshot_for_today()), so checking
                    # every ~5 minutes here is cheap and never double-writes.
                    try:
                        record_periodic_pnl_snapshots(
                            positions, prices_by_key, evaluation_epoch,
                        )
                        maybe_snapshot_daily_portfolio(positions, prices_by_key, tracked_traders, muted_traders)
                    except Exception as e:
                        append_log({"timestamp": now_iso(), "event_type": "error",
                                    "error": f"daily portfolio snapshot failed: {e}"})

            if not SHUTDOWN_REQUESTED and now - last_closeout_sweep >= config.CLOSEOUT_INTERVAL_SECONDS:
                last_closeout_sweep = now
                try:
                    run_closeout_sweep(positions, trader_performance, muted_traders, all_by_lower)
                    run_shadow_closeout_sweep(shadow_positions, "rehab")
                    run_shadow_closeout_sweep(challenger_positions, "challenger")
                except Exception as e:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "error": f"closeout sweep failed: {e}"})
                persist()

            # Zombie-position dump exit (2026-07-27) — see
            # sweep_zombie_positions' docstring. Own low-frequency interval,
            # separate from and much rarer than every other sweep here —
            # deliberately so, since it only ever acts on positions that
            # have already failed the normal TTP sweep for a very long time.
            if not SHUTDOWN_REQUESTED and now - last_zombie_sweep >= config.ZOMBIE_SWEEP_INTERVAL_SECONDS:
                last_zombie_sweep = now
                try:
                    sweep_zombie_positions(positions, trader_performance, muted_traders, tracked_by_lower)
                except Exception as e:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "error": f"zombie position sweep failed: {e}"})
                persist()

            # bot_event_log retention (2026-07-22) — see db.prune_event_log()'s
            # docstring for why this is a DELETE sweep, not a logging-handler
            # concept: this table is a SQLite table, not a file. Runs AFTER
            # the sweeps above log their own events for this cycle, so a
            # freshly-logged row is never at risk of being pruned in the same
            # cycle it was written (retention_days is measured in days, this
            # ordering nuance doesn't matter in practice, but it's the more
            # obviously-correct order to read).
            if not SHUTDOWN_REQUESTED and now - last_prune_sweep >= config.PRUNE_INTERVAL_SECONDS:
                last_prune_sweep = now
                try:
                    deleted = prune_event_log()
                    if deleted:
                        append_log({"timestamp": now_iso(), "event_type": "event_log_pruned",
                                    "rows_deleted": deleted,
                                    "retention_days": config.EVENT_LOG_RETENTION_DAYS})
                except Exception as e:
                    append_log({"timestamp": now_iso(), "event_type": "error",
                                "error": f"event log prune failed: {e}"})
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
    direct_feed_executor.shutdown(wait=False)
    logger.info("Copybot stopped cleanly (state saved).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Copybot stopped.")
        sys.exit(0)
