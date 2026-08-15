#!/usr/bin/env python3
"""Manually clear the portfolio drawdown kill switch (risk_manager.py).

The kill switch LATCHES by design: once portfolio equity breaches the
floor (config.EQUITY_FLOOR_USD) or the max drawdown from peak
(config.MAX_DRAWDOWN_FROM_PEAK_USD), all new BUYs halt — across restarts —
until a human runs this script. That's the point: a breached limit means
someone reviews WHY before risk resumes, so this deliberately lives as a
standalone command rather than a dashboard button (dashboard.py's
documented boundary is that it never writes to data/app.db).

Usage:
    python3 reset_kill_switch.py            # show current state only
    python3 reset_kill_switch.py --clear --expected-triggered-at ... \
      --reviewed-equity 1100.00 --reviewed-at 2026-08-16T00:00:00+00:00

The clear path re-audits the realized ledger and independently re-evaluates
the configured floor/drawdown conditions against a fresh, timestamped equity
review. It refuses stale evidence, a changed latch, a non-PASS ledger, or an
equity value that still breaches either condition.

NOTE: because bot.py caches risk_state in memory, a LATCHED bot must be
restarted after this reset to actually resume buying. If the bot is NOT
currently running, no restart is needed — it loads the cleared state on
next start.
"""

import argparse
from datetime import datetime, timezone
import math

import risk_manager
from db import clear_risk_value, get_realized_ledger_integrity_status, get_risk_value


MAX_REVIEW_AGE_SECONDS = 600


def _parse_review_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--reviewed-at must include a timezone")
    return parsed.astimezone(timezone.utc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true", help="deprecated; status is the default")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--expected-triggered-at")
    parser.add_argument("--reviewed-equity", type=float)
    parser.add_argument("--reviewed-at")
    args = parser.parse_args()
    kill_switch = get_risk_value("kill_switch")
    if not kill_switch:
        print("Kill switch is not latched — nothing to clear.")
        return

    print("Kill switch is LATCHED:")
    print(f"  triggered_at: {kill_switch.get('triggered_at')}")
    for reason in kill_switch.get("reasons", []):
        print(f"  reason: {reason}")
    print(f"  equity at trigger: ${kill_switch.get('equity', float('nan')):.2f}")
    print(f"  peak (HWM) at trigger: ${kill_switch.get('hwm', float('nan')):.2f}")

    if not args.clear:
        print("Status only. Clearing requires --clear plus fresh reviewed-equity evidence.")
        return

    if args.status:
        raise SystemExit("choose either --status or --clear")
    if not args.expected_triggered_at or not args.reviewed_at:
        raise SystemExit("--clear requires --expected-triggered-at and --reviewed-at")
    if args.expected_triggered_at != kill_switch.get("triggered_at"):
        raise SystemExit("kill-switch trigger changed since review; refusing to clear")
    if args.reviewed_equity is None or not math.isfinite(args.reviewed_equity):
        raise SystemExit("--clear requires a finite --reviewed-equity")
    try:
        reviewed_at = _parse_review_time(args.reviewed_at)
    except (TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    review_age = (datetime.now(timezone.utc) - reviewed_at).total_seconds()
    if review_age < -30 or review_age > MAX_REVIEW_AGE_SECONDS:
        raise SystemExit(
            f"equity review is not fresh (age={review_age:.0f}s, max={MAX_REVIEW_AGE_SECONDS}s)"
        )

    integrity = get_realized_ledger_integrity_status()
    if integrity.get("status") != "PASS":
        raise SystemExit(
            "realized ledger is not PASS; refusing to clear: "
            + ",".join(integrity.get("failures", []) + integrity.get("warnings", []))
        )
    hwm = get_risk_value("equity_hwm")
    try:
        hwm = float(hwm)
    except (TypeError, ValueError) as exc:
        raise SystemExit("equity HWM is missing or invalid") from exc
    _, triggers = risk_manager.evaluate_equity(args.reviewed_equity, hwm)
    if triggers:
        raise SystemExit(
            "reviewed equity still breaches the kill switch; refusing to clear: "
            + "; ".join(triggers)
        )

    clear_risk_value("kill_switch")
    print()
    print("Cleared. If bot.py is currently running it must be RESTARTED to "
          "resume buying (its in-memory latch persists until restart); if it "
          "isn't running, just start it normally.")


if __name__ == "__main__":
    main()
