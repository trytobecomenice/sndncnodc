#!/usr/bin/env python3
"""Telegram alerting for bot.py (2026-07-31, Phase 1 observability — see
docs/copy-trading/SAFETY.md Sec.54). Built in direct response to tonight's
kill-switch incident, which was only found because Joey happened to ask
"how's the bot running" rather than being surfaced automatically.

Stdlib only (http.client), matching this project's existing zero-third-
party-dependency convention for outbound HTTPS calls (see
polymarket_simulator.py's own docstring for the same choice).

TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID already existed in .env.example (added
2026-07-26 for a TS end-of-day report, packages/shared/src/telegram.ts,
that was never actually built) — reused here, not re-invented. Loaded via
python-dotenv (already a real dependency, see wss_listener.py), never
committed (.env is git-ignored).

Every function here fails silently (logs, never raises) — a notification
failure must never take down the main trading loop the way a real risk
check failure would.
"""

import http.client
import json
import logging
import os

from dotenv import load_dotenv

import config

load_dotenv()

logger = logging.getLogger("copybot.telegram")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram_alert(message):
    """Best-effort send of `message` to TELEGRAM_CHAT_ID via the Telegram
    Bot API's sendMessage endpoint. No-ops (returns False) if alerts are
    disabled or the token/chat_id aren't configured — a missing .env entry
    must degrade to "no notifications" silently, not crash bot.py's main
    loop. Returns True only on a confirmed 2xx response.
    """
    if not config.ENABLE_TELEGRAM_ALERTS:
        return False
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    body = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode("utf-8")
    try:
        conn = http.client.HTTPSConnection("api.telegram.org", timeout=10)
        try:
            conn.request(
                "POST", f"/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                body=body, headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            response.read()  # drain the body so the connection can be reused/closed cleanly
            if 200 <= response.status < 300:
                return True
            logger.warning(f"Telegram alert failed: HTTP {response.status}")
            return False
        finally:
            conn.close()
    except (http.client.HTTPException, OSError) as e:
        logger.warning(f"Telegram alert failed: {e}")
        return False
