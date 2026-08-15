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

from bullpen_client import run_bullpen_json


def run_canary():
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        status = run_bullpen_json(["--read-only", "status"], retries=3, retry_delay=1.0, timeout=20)
        health = status.get("health") if isinstance(status, dict) else None
        if not isinstance(health, dict) or health.get("logged_in") is not True:
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
        "response_keys": sorted(response.keys()) if isinstance(response, dict) else [],
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_canary())
