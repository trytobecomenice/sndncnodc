#!/usr/bin/env python3
"""Read-only daily canary for the otherwise-dormant execution provider.

This deliberately calls only `bullpen status`: it verifies that the binary,
authentication session and backend are reachable without creating, changing
or cancelling an order. It writes no database fields and is never imported by
the research/scoring pipeline.
"""

import json
from datetime import datetime, timezone

from bullpen_client import run_bullpen_json


def run_canary():
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        response = run_bullpen_json(["status"], retries=3, retry_delay=1.0, timeout=20)
    except Exception as exc:
        result = {"ok": False, "checked_at": checked_at, "check": "bullpen_status", "error": str(exc)}
        print(json.dumps(result, sort_keys=True))
        return 1
    result = {
        "ok": True,
        "checked_at": checked_at,
        "check": "bullpen_status",
        "response_keys": sorted(response.keys()) if isinstance(response, dict) else [],
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_canary())
