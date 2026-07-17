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
    python3 reset_kill_switch.py            # show current state + clear it
    python3 reset_kill_switch.py --status   # show current state only

The running bot picks the reset up on its next BUY signal — no restart
needed (it re-reads nothing; the in-memory latch is only ever SET by the
bot, so clearing the DB row here plus the printed restart advice below
covers both cases).

NOTE: because bot.py caches risk_state in memory, a LATCHED bot must be
restarted after this reset to actually resume buying. If the bot is NOT
currently running, no restart is needed — it loads the cleared state on
next start.
"""

import sys

from db import get_risk_value, clear_risk_value


def main():
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

    if "--status" in sys.argv:
        return

    clear_risk_value("kill_switch")
    print()
    print("Cleared. If bot.py is currently running it must be RESTARTED to "
          "resume buying (its in-memory latch persists until restart); if it "
          "isn't running, just start it normally.")


if __name__ == "__main__":
    main()
