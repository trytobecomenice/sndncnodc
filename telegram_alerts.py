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
import socket
import threading
from contextlib import contextmanager

from dotenv import load_dotenv

import config

load_dotenv()

logger = logging.getLogger("copybot.telegram")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# socket.getaddrinfo is monkeypatched process-wide (however briefly) inside
# _force_ipv4_dns() -- this lock keeps two concurrent send_telegram_alert()
# calls (bot.py does call this from more than one thread, even if rarely:
# daily summary, kill-switch, throttled errors) from stepping on each
# other's patch/restore window.
_dns_patch_lock = threading.Lock()


@contextmanager
def _force_ipv4_dns():
    """Found live 2026-08-01: every alert from bot.py's own long-running
    process was silently failing (logged, never surfaced further — see this
    module's own "fails silently" design) with "no route to host" followed
    by an unrelated HTTP 401 a couple ms later, while a fresh standalone
    Python process on the SAME box sending the SAME message succeeded
    immediately. Root-caused, not guessed: `api.telegram.org` resolves to
    both an A and AAAA record, and this EC2 box has NO real IPv6 route at
    all (`ip -6 route show` returns only link-local `fe80::/64` entries,
    nothing global) -- confirmed directly via `curl -6` hanging/failing
    against the same host while `curl -4` succeeds instantly. `http.client`
    generally falls back to IPv4 when IPv6 fails, but evidently not
    reliably from inside bot.py's specific process/thread context, and
    guessing at exactly why wasn't worth chasing further when the fix is
    this direct: temporarily filter `socket.getaddrinfo()` to IPv4-only
    results for the lifetime of one connection attempt, restored
    immediately after (never a global, permanent change to socket
    behavior for the rest of the process).
    """
    original_getaddrinfo = socket.getaddrinfo

    def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    with _dns_patch_lock:
        socket.getaddrinfo = _ipv4_only
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo


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
            # DNS resolution + the actual connect() happen lazily, inside
            # request() -- both must be inside the IPv4-forcing scope, not
            # just HTTPSConnection's constructor (which does neither).
            with _force_ipv4_dns():
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
