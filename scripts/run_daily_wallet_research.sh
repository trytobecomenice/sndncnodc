#!/usr/bin/env bash
set -euo pipefail

# One fail-fast entry point so cron redirects the whole pipeline, not only
# its final command. All decision inputs below are official Polymarket raw
# data; Bullpen's read-only canary is intentionally a separate schedule.
pnpm run scan:leaderboard
pnpm run score:wallet-categories
pnpm --filter @copybot/copy-trading discover:category-specialists -- --queue-approvals
python3 send_wallet_approvals.py
