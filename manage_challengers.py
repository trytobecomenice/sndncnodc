#!/usr/bin/env python3
"""Maintain the paper-only challenger pool and queue proven promotions.

Run after the daily wallet-scoring jobs and before send_wallet_approvals.py.
No candidate can become ``track`` here: qualifying challengers only create a
Telegram approval request. Approval performs a transactional one-in-one-out
swap; the replaced muted wallet becomes ``retiring`` until all open positions
are closed.
"""

import math
import time

import config
import db


def compute_shadow_evidence(returns, z=None):
    """Copy-slippage-adjusted evidence from equal-stake shadow returns."""
    z = config.CHALLENGER_LCB_Z if z is None else z
    values = [float(x) for x in returns]
    n = len(values)
    if not values:
        return {"tradeCount": 0, "meanReturn": None, "lowerConfidenceBound": None,
                "winRate": None, "maxDrawdownReturnUnits": None}

    mean = sum(values) / n
    if n < 2:
        lcb = None
    else:
        variance = sum((x - mean) ** 2 for x in values) / (n - 1)
        lcb = mean if variance < 1e-12 else mean - z * math.sqrt(variance / n)

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    return {
        "tradeCount": n,
        "meanReturn": mean,
        "lowerConfidenceBound": lcb,
        "winRate": sum(x > 0 for x in values) / n,
        "maxDrawdownReturnUnits": max_drawdown,
    }


def challenger_is_ready(profile, evidence, now_ts=None):
    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    changed_at = profile.get("status_changed_at")
    age_seconds = now_ts - int(changed_at) if changed_at else 0
    return (
        age_seconds >= config.CHALLENGER_MIN_AGE_DAYS * 86400
        and evidence["tradeCount"] >= config.CHALLENGER_MIN_CLOSED_TRADES
        and evidence["lowerConfidenceBound"] is not None
        and evidence["lowerConfidenceBound"] > 0
    )


def evaluate_challengers(now_ts=None):
    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    queued = []
    failed = []
    for profile in db.get_wallets_by_status("challenger"):
        address = profile["wallet_address"].lower()
        returns = db.get_shadow_returns(
            address, "shadow_challenger",
            min_cost_basis_usd=config.MUTE_MIN_TRADE_COST_USD,
        )
        evidence = compute_shadow_evidence(returns)
        age_days = ((now_ts - int(profile.get("status_changed_at") or now_ts)) / 86400)
        evidence.update({
            "shadowAgeDays": age_days,
            "compositeScore": profile.get("composite_score"),
            "sourceWinRate": profile.get("win_rate"),
            "sourceTradeCount": profile.get("trade_count_all_time"),
        })

        old_enough = age_days >= config.CHALLENGER_MIN_AGE_DAYS
        enough_trades = evidence["tradeCount"] >= config.CHALLENGER_MIN_CLOSED_TRADES
        if old_enough and enough_trades and not challenger_is_ready(profile, evidence, now_ts):
            db.set_wallet_status(
                address, "ignore",
                "challenger failed clean shadow lower-confidence-bound gate",
            )
            db.abandon_open_shadow_positions(
                address, "shadow_challenger", "challenger_failed_evidence_gate",
            )
            failed.append(address)
            continue

        if not challenger_is_ready(profile, evidence, now_ts):
            continue
        if db.has_pending_wallet_approval(address, source="challenger_shadow"):
            continue

        replacement = db.get_replacement_wallet_candidate()
        if replacement is None:
            # One-in-one-out is mandatory; do not silently grow the roster.
            continue
        evidence["replacementWalletAddress"] = replacement["wallet_address"].lower()
        evidence["replacementNickname"] = replacement.get("nickname")
        evidence["replacementRealizedPnlUsd"] = replacement.get("realized_pnl_usd")
        evidence["replacementEvPct"] = replacement.get("ev_pct")
        reason = (
            f"Clean shadow passed: {evidence['tradeCount']} non-dust closes over "
            f"{age_days:.1f}d, mean return {evidence['meanReturn']:.2%}, "
            f"one-sided LCB {evidence['lowerConfidenceBound']:.2%}. "
            f"Approval retires muted wallet "
            f"{replacement.get('nickname') or replacement['wallet_address']} one-for-one."
        )
        request_id = db.create_wallet_approval_request(
            address, "track", "challenger_shadow", evidence, reason,
            category=profile.get("category"),
        )
        queued.append(request_id)
    return {"queued": queued, "failed": failed}


def enroll_challengers():
    gap = max(0, config.TARGET_ACTIVE_TRADER_COUNT - db.get_active_tracked_count())
    if gap == 0:
        return []
    existing = db.get_wallets_by_status("challenger")
    target = min(config.CHALLENGER_POOL_MAX,
                 max(gap, gap * config.CHALLENGER_POOL_MULTIPLIER))
    slots = max(0, target - len(existing))
    if slots == 0:
        return []

    tracked = db.get_risk_value("tracked_traders") or {}
    excluded = {address.lower() for address in tracked} | db.get_ever_tracked_wallets()
    candidates = db.get_pool_refill_candidates(
        excluded, config.POOL_REFILL_MIN_COMPOSITE_SCORE, limit=slots,
    )
    enrolled = []
    for candidate in candidates:
        address = candidate["wallet_address"].lower()
        if db.set_wallet_status(
            address, "challenger",
            "auto-enrolled into paper-only clean shadow evaluation",
        ):
            enrolled.append(address)
    return enrolled


def main():
    retired = db.retire_completed_wallets()
    result = evaluate_challengers()
    enrolled = enroll_challengers()
    print(
        f"challenger manager: retired={len(retired)} queued={len(result['queued'])} "
        f"failed={len(result['failed'])} enrolled={len(enrolled)}"
    )


if __name__ == "__main__":
    main()
