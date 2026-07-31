#!/usr/bin/env python3
"""Portfolio-level risk controls for bot.py.

Four controls, all gating NEW BUYs only (sells/exits are never blocked —
a risk layer that traps you in positions adds risk instead of removing it),
applied in both paper and live mode so paper stays representative:

  1. Total exposure ceiling  (config.MAX_TOTAL_EXPOSURE_USD)
  2. Per-event exposure cap  (config.MAX_EVENT_EXPOSURE_USD)
  3. Per-wallet exposure cap (config.MAX_WALLET_EXPOSURE_USD, added
                              2026-07-24, Rule 26 — see wallet_exposure_usd()'s
                              docstring for why this is a genuinely different
                              axis from the two caps above, not a duplicate)
  4. Drawdown kill switch    (config.EQUITY_FLOOR_USD +
                              config.MAX_DRAWDOWN_FROM_PEAK_USD, latched
                              until cleared via reset_kill_switch.py)

Every function in this module is PURE (no I/O, no DB, no subprocess) —
deliberately, so the decision logic is unit-testable the same way the TS
scoring gates are (see test_risk_manager.py). bot.py owns all the wiring:
it loads/persists state via db.py, resolves market->event via bullpen, and
calls these functions with plain values.

Equity model: equity = config.PAPER_BANKROLL_USD + realized PnL
+ unrealized PnL. Unrealized comes from the trailing-TP sweep's price
fetches (the only place that already prices every open position), so the
kill switch reacts within one sweep interval (~5 min), not per-trade — an
accepted tradeoff at current stakes; the per-trade checks (ceiling / event
cap / latched-switch lookup) are all instant.
"""

import config


def total_exposure_usd(positions):
    """Sum of open positions' cost basis — 'USD currently deployed'.

    Cost basis (not current market value) on purpose: it's what the ceiling
    is meant to limit (capital at risk), it needs no price fetches, and it
    can't swing with the market between two consecutive checks.
    """
    return sum(pos.get("cost_basis_usd", 0.0) for pos in positions.values())


def event_exposure_usd(positions, market_to_event, event_slug):
    """USD deployed across all open positions in one Polymarket event.

    Positions in markets whose event hasn't been resolved yet (missing from
    market_to_event) don't count toward any event's total — by the time a
    BUY is being risk-checked, bot.py has already fail-closed on resolving
    THAT market's event, so the gap only affects legacy positions opened
    before this feature existed.
    """
    total = 0.0
    for key, pos in positions.items():
        parts = key.split("|")
        if len(parts) != 3:
            continue
        _, market_slug, _ = parts
        if market_to_event.get(market_slug) == event_slug:
            total += pos.get("cost_basis_usd", 0.0)
    return total


def wallet_exposure_usd(positions, wallet_address):
    """USD deployed across ALL open positions tied to one TRADER, regardless
    of which market/category each is in.

    Added 2026-07-24 (Rule 26) alongside the category-quota system (Rule 24)
    allowing one wallet to occupy multiple category slots when its stats
    justify it — a wallet's edge might be process-driven (speed, arbitrage)
    rather than domain-specific, so multi-category tracking is intentional,
    but shouldn't multiply that wallet's capital budget by however many
    slots it fills. Genuinely different from total_exposure_usd() (portfolio-
    wide) and event_exposure_usd() (per-EVENT, could span several wallets):
    this is per-WALLET, which can span several events/categories for the
    SAME trader.

    Trader comparison is case-insensitive (2026-07-26 fix — previously
    matched with plain `==`, on the documented-but-false assumption that
    positions/source_positions always keep one consistent casing per
    wallet). Confirmed live this doesn't hold: the same real wallet can be
    detected via two sources reporting its address in different casing
    (e.g. an EIP-55 checksummed polling response vs. raw lowercase hex from
    an on-chain event), fragmenting its positions across two different key
    prefixes. That silently broke exactly the cap this function exists to
    enforce — one tracked wallet's true open exposure was $82.60 against a
    $50 cap, and this function only ever saw one casing's share of it.
    bot.position_key() now also lowercases at the point positions are
    keyed, but this compares defensively regardless of caller/key casing
    rather than depending on that discipline alone.
    """
    wallet_lower = wallet_address.lower() if wallet_address else wallet_address
    total = 0.0
    for key, pos in positions.items():
        parts = key.split("|")
        if len(parts) != 3:
            continue
        trader, _, _ = parts
        if trader.lower() == wallet_lower:
            total += pos.get("cost_basis_usd", 0.0)
    return total


def wallet_exposure_cap_usd(wallet_address):
    """The per-wallet exposure cap that actually applies to `wallet_address`
    -- config.VIP_WALLET_EXPOSURE_CAP_USD (2026-07-26, manually curated
    override for proven, high-EV, high-volume wallets, e.g. strict-7 and
    political-whale-1) takes precedence over the flat
    config.MAX_WALLET_EXPOSURE_USD when the wallet has an entry. Matched
    case-insensitively, same reasoning as wallet_exposure_usd() -- a VIP
    override keyed to one casing must not silently fail to apply just
    because this specific call arrived in the other casing.

    Deliberately a manual, address-keyed dict rather than a scored/
    automatic tier: with the EV-based circuit breaker (Rule 35) and Shadow
    Rehab still new, there isn't yet a trustworthy automatic signal to key
    a bigger capital allocation off -- a human decision for now, same
    status as TRACKED_TRADERS membership itself.

    Returns None if no wallet-level cap applies at all (mirrors
    config.MAX_WALLET_EXPOSURE_USD's own None-disables convention).
    """
    if wallet_address is None:
        return config.MAX_WALLET_EXPOSURE_USD
    override = config.VIP_WALLET_EXPOSURE_CAP_USD.get(wallet_address.lower())
    return override if override is not None else config.MAX_WALLET_EXPOSURE_USD


def depth_capped_trade_size_usd(trade_size_usd, book_depth_usd, depth_fraction):
    """Clamps a single trade's own size to a fraction of ITS market's visible
    order-book depth (added 2026-07-28) -- a genuinely different axis from
    every other guard in this file: those cap aggregate exposure across many
    positions/markets for one wallet/event/portfolio; this caps how much of
    ONE market's own liquidity a SINGLE trade is allowed to eat, independent
    of whatever else is open. Deliberately NOT folded into
    wallet_exposure_cap_usd()/event_exposure_usd() -- book depth is a
    property of the specific market being traded right now, not something
    that aggregates the way dollars-deployed-across-positions does (see
    docs/copy-trading/RISK_MANAGEMENT.md's Depth-Aware Trade Sizing rule for
    the full reasoning).

    book_depth_usd is None (fetch failed/unavailable, or simply never
    computed by a caller that doesn't have it) returns trade_size_usd
    UNCLAMPED -- a guard that can't get real data must not invent a worse
    number (same posture as compute_unrealized_pnl's treatment of an
    unpriceable position below), and never treats "unknown" as "zero
    liquidity." Pure function, unit-testable without a DB or network call.
    """
    if book_depth_usd is None:
        return trade_size_usd
    return min(trade_size_usd, book_depth_usd * depth_fraction)


def check_buy(positions, market_to_event, event_slug, trade_usd, kill_switch, wallet_address=None):
    """The pre-BUY risk gate. Returns (ok, skip_event_type, reason):
    (True, None, None) if the BUY may proceed, otherwise ok=False with the
    bot_event_log event_type to log and a human-readable reason.

    Check order is deliberate — cheapest and most absolute first:
      1. kill switch (latched halt beats everything)
      2. total exposure ceiling
      3. per-event cap
      4. per-wallet cap (added 2026-07-24, Rule 26 — see
         wallet_exposure_usd()'s docstring; 2026-07-26 addendum: this cap
         can be individually raised per wallet via
         config.VIP_WALLET_EXPOSURE_CAP_USD, see wallet_exposure_cap_usd())
    Limits use strict 'would exceed' semantics: a BUY landing exactly ON a
    limit is allowed; the first dollar past it is not. Any limit set to
    None in config is disabled. wallet_address defaults to None (which,
    combined with config.MAX_WALLET_EXPOSURE_USD's own None-disables
    check below, means callers that don't pass it simply skip check 4 —
    no call site is forced to change just because this parameter exists).
    """
    if kill_switch:
        reasons = kill_switch.get("reasons") if isinstance(kill_switch, dict) else None
        return False, "skip_risk_kill_switch", (
            f"kill switch latched since {kill_switch.get('triggered_at', '?')}: "
            f"{'; '.join(reasons) if reasons else 'unknown trigger'} — run "
            f"reset_kill_switch.py after review to resume buying"
        )

    if config.MAX_TOTAL_EXPOSURE_USD is not None:
        current = total_exposure_usd(positions)
        if current + trade_usd > config.MAX_TOTAL_EXPOSURE_USD:
            return False, "skip_risk_exposure_ceiling", (
                f"total exposure ${current:.2f} + ${trade_usd:.2f} would exceed "
                f"ceiling ${config.MAX_TOTAL_EXPOSURE_USD:.2f}"
            )

    if config.MAX_EVENT_EXPOSURE_USD is not None:
        current = event_exposure_usd(positions, market_to_event, event_slug)
        if current + trade_usd > config.MAX_EVENT_EXPOSURE_USD:
            return False, "skip_risk_event_cap", (
                f"event '{event_slug}' exposure ${current:.2f} + ${trade_usd:.2f} "
                f"would exceed per-event cap ${config.MAX_EVENT_EXPOSURE_USD:.2f}"
            )

    if wallet_address is not None:
        wallet_cap = wallet_exposure_cap_usd(wallet_address)
        if wallet_cap is not None:
            current = wallet_exposure_usd(positions, wallet_address)
            if current + trade_usd > wallet_cap:
                return False, "skip_risk_wallet_cap", (
                    f"wallet '{wallet_address}' exposure ${current:.2f} + ${trade_usd:.2f} "
                    f"would exceed per-wallet cap ${wallet_cap:.2f}"
                )

    return True, None, None


def compute_unrealized_pnl(positions, prices_by_key):
    """Sum of (mark value - cost basis) across every open position we have
    a usable price for. Extracted out of compute_equity (2026-07-28, the
    Grafana dashboard's daily-snapshot breakdown) so both that function and
    compute_equity_breakdown below share the exact same math rather than
    two independently-maintained copies of it.

    prices_by_key maps position key -> latest indicative price (collected
    by the TTP sweep). A position the sweep couldn't price contributes ZERO
    unrealized PnL (it's carried at cost) rather than being guessed at —
    conservative in the sense that it neither inflates equity toward a
    higher HWM nor manufactures a phantom drawdown from a missing quote.

    Extreme-tail positions (avg_entry_price within config.
    EQUITY_MARK_MIN_ENTRY_PRICE of either $0 or $1) are carried at cost too,
    for the same conservatism, not because they're unimportant but because
    they're the opposite of a missing quote: a tiny cost basis (typically
    $5-10) implies a HUGE share count at these prices, so a single bad/stale
    CLOB read on an illiquid market amplifies into a multi-thousand-dollar
    phantom swing in this function's total — confirmed live 2026-07-31: a
    handful of ultra-longshot 2028 election bets (entry 0.001-0.008, $5-10
    cost basis each) coincided with the drawdown kill switch's equity
    swinging by ~$4900-5000 in a single sweep, latching it on a fabricated
    drawdown. Gated on avg_entry_price (fixed at buy time), not on whether
    THIS particular reading looks suspicious — the vulnerability is inherent
    to the position's own economics, not to any one price fetch, so excluding
    by entry price is deterministic and doesn't depend on guessing which
    reads are "bad." Same "explicit judgment call, not dressed up as
    rigorous" framing as DEFAULT_TCA_MIN_ENTRY_PRICE (packages/copy-trading/
    src/discoverCategorySpecialists.ts), reused here for a different
    subsystem.
    """
    unrealized = 0.0
    for key, pos in positions.items():
        avg_entry = pos.get("avg_entry_price", 0.0)
        if avg_entry and (avg_entry < config.EQUITY_MARK_MIN_ENTRY_PRICE
                          or avg_entry > 1 - config.EQUITY_MARK_MIN_ENTRY_PRICE):
            continue
        price = prices_by_key.get(key)
        if price is None or price <= 0:
            continue
        unrealized += pos.get("shares", 0.0) * price - pos.get("cost_basis_usd", 0.0)
    return unrealized


def compute_equity(positions, prices_by_key, realized_pnl):
    """Portfolio equity = bankroll + realized PnL + unrealized PnL.

    Unchanged in behavior/signature by the 2026-07-28 refactor above — this
    still returns exactly the single float every existing caller (the
    kill-switch evaluation in bot.py's main loop) already depends on.
    """
    return config.PAPER_BANKROLL_USD + realized_pnl + compute_unrealized_pnl(positions, prices_by_key)


def compute_equity_breakdown(positions, prices_by_key, realized_pnl):
    """Same inputs as compute_equity, but returns every component
    separately — built for the Grafana daily-snapshot table
    (daily_portfolio_snapshots), which wants total_cash and
    total_unrealized_pnl as their own columns, not just the combined
    equity figure compute_equity has always returned.

    total_cash here means "bankroll not currently deployed into an open
    position" — bankroll adjusted for realized gains/losses, minus
    whatever's tied up in open positions AT COST (their cost_basis_usd
    sum, not mark value — cash freed by a position isn't affected by that
    position's current unrealized swing, only by it actually closing).
    """
    unrealized_pnl = compute_unrealized_pnl(positions, prices_by_key)
    deployed_cost_basis = sum(pos.get("cost_basis_usd", 0.0) for pos in positions.values())
    total_cash = config.PAPER_BANKROLL_USD + realized_pnl - deployed_cost_basis
    total_equity = config.PAPER_BANKROLL_USD + realized_pnl + unrealized_pnl
    return {
        "total_equity": total_equity,
        "total_cash": total_cash,
        "total_unrealized_pnl": unrealized_pnl,
    }


def evaluate_equity(equity, prior_hwm):
    """Updates the high-water mark and evaluates both kill-switch triggers.

    Returns (new_hwm, triggers) where triggers is a list of human-readable
    reason strings (empty = no halt). The HWM only ever ratchets UP; on the
    very first evaluation (prior_hwm None) it seeds at current equity, so
    the drawdown trigger measures from feature-launch equity forward rather
    than retroactively treating the configured bankroll as a past peak.
    """
    hwm = equity if prior_hwm is None else max(prior_hwm, equity)

    triggers = []
    if config.EQUITY_FLOOR_USD is not None and equity < config.EQUITY_FLOOR_USD:
        triggers.append(
            f"equity ${equity:.2f} below floor ${config.EQUITY_FLOOR_USD:.2f} "
            f"(bankroll ${config.PAPER_BANKROLL_USD:.2f})"
        )
    if config.MAX_DRAWDOWN_FROM_PEAK_USD is not None and equity < hwm - config.MAX_DRAWDOWN_FROM_PEAK_USD:
        triggers.append(
            f"equity ${equity:.2f} is ${hwm - equity:.2f} below peak ${hwm:.2f}, "
            f"exceeding max drawdown ${config.MAX_DRAWDOWN_FROM_PEAK_USD:.2f}"
        )
    return hwm, triggers
