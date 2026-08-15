#!/usr/bin/env python3
"""Read-only daily canary for the otherwise-dormant execution provider.

This first calls passive `bullpen status`, then an authenticated portfolio read
under the CLI's global `--read-only` interlock. The second call is essential:
`status` exits successfully even when login is missing and explicitly does not
contact Bullpen's backend. Neither call can create, change or cancel an order.
The canary writes no database fields and is never imported by research/scoring.
"""

import json
from datetime import datetime, timezone

import config
from bullpen_client import run_bullpen_json


# Paper mode does not use Bullpen execution. Keep the missing-login state
# explicit and time-bounded instead of emitting a permanent red light that
# operators learn to ignore. This suppression never applies in LIVE_MODE and
# expires before any live-readiness review can proceed.
PAPER_AUTH_SUPPRESSION_DEADLINE = datetime(2026, 8, 23, tzinfo=timezone.utc)


def run_canary(now=None):
    now = now or datetime.now(timezone.utc)
    checked_at = now.isoformat()
    try:
        status = run_bullpen_json(
            ["--read-only", "status"], retries=3, retry_delay=1.0, timeout=20
        )
        health = status.get("health") if isinstance(status, dict) else None
        if not isinstance(health, dict) or "logged_in" not in health:
            raise RuntimeError("bullpen status response is missing health.logged_in")
        if health["logged_in"] is not True:
            if health["logged_in"] is not False:
                raise RuntimeError("bullpen status health.logged_in is not boolean")
            if not config.LIVE_MODE and now < PAPER_AUTH_SUPPRESSION_DEADLINE:
                result = {
                    "ok": True,
                    "checked_at": checked_at,
                    "check": "bullpen_read_only_execution_health",
                    "state": "suppressed_paper_mode",
                    "execution_ready": False,
                    "suppression_deadline": PAPER_AUTH_SUPPRESSION_DEADLINE.isoformat(),
                    "required_before_live": "bullpen login and authenticated canary pass",
                }
                print(json.dumps(result, sort_keys=True))
                return 0
            raise RuntimeError("bullpen execution canary is not authenticated; run `bullpen login`")
        response = run_bullpen_json(
            ["--read-only", "portfolio"], retries=3, retry_delay=1.0, timeout=20
        )
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": checked_at,
            "check": "bullpen_read_only_execution_health",
            "error": str(exc),
        }
        print(json.dumps(result, sort_keys=True))
        return 1
    result = {
        "ok": True,
        "checked_at": checked_at,
        "check": "bullpen_read_only_execution_health",
        "state": "healthy",
        "execution_ready": True,
        "response_keys": sorted(response.keys()) if isinstance(response, dict) else [],
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_canary())
