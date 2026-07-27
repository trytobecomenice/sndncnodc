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


def compute_equity(positions, prices_by_key, realized_pnl):
    """Portfolio equity = bankroll + realized PnL + unrealized PnL.

    prices_by_key maps position key -> latest indicative price (collected
    by the TTP sweep). A position the sweep couldn't price contributes ZERO
    unrealized PnL (it's carried at cost) rather than being guessed at —
    conservative in the sense that it neither inflates equity toward a
    higher HWM nor manufactures a phantom drawdown from a missing quote.
    """
    unrealized = 0.0
    for key, pos in positions.items():
        price = prices_by_key.get(key)
        if price is None or price <= 0:
            continue
        unrealized += pos.get("shares", 0.0) * price - pos.get("cost_basis_usd", 0.0)
    return config.PAPER_BANKROLL_USD + realized_pnl + unrealized


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
