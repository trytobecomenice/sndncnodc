#!/usr/bin/env python3
"""Long-running listener for the Telegram wallet-approval workflow
(2026-08-01) — the "receive" half of send_wallet_approvals.py's "send"
half. Long-polls Telegram's getUpdates for callback_query taps on the
Approve/Reject buttons send_wallet_approvals.py attaches to each candidate
message, and is the ONLY thing that ever flips wallet_profile.status to
'track'/'bench' as a result of this workflow (via
db.resolve_wallet_approval_request, itself the only writer of that
transition — see db.py's module comment on the table).

Deployed as a systemd service on EC2 (telegram-approval-listener.service,
mirrors omsd.service's shape — see docs/copy-trading/SAFETY.md), not a cron
job: Telegram long-polling holds one connection open for up to
LONG_POLL_TIMEOUT_SECONDS at a time, which doesn't fit a short-lived
cron-fired script well, and a systemd Restart=on-failure unit gives
near-instant response to a tap instead of waiting for the next cron tick.

RESILIENCE: same shape as wss_listener.py's reconnect/backoff loop — a
single failed getUpdates call (network blip, Telegram outage) is caught,
logged, and retried with exponential backoff, never crashes the process. A
single malformed/unauthorized callback_query is logged and skipped, never
blocks processing the rest of the batch.

SECURITY: every callback_query is checked against TELEGRAM_CHAT_ID before
acting on it (is_authorized_chat) — this bot must not let a stranger who
somehow messages it approve a real-money wallet.

Usage:
    python3 telegram_approval_listener.py
"""

import http.client
import json
import logging
import os
import re
import sys
import time

import config
import db
import telegram_alerts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] telegram_approval_listener: %(message)s",
)
logger = logging.getLogger("telegram_approval_listener")

LONG_POLL_TIMEOUT_SECONDS = int(os.environ.get("TELEGRAM_LONG_POLL_TIMEOUT_SECONDS", "30"))
RECONNECT_BACKOFF_INITIAL_SECONDS = float(os.environ.get("RECONNECT_BACKOFF_INITIAL_SECONDS", "2"))
RECONNECT_BACKOFF_MAX_SECONDS = float(os.environ.get("RECONNECT_BACKOFF_MAX_SECONDS", "60"))

# Plain local file, not a DB table — this offset is this process's own
# read-position bookmark into Telegram's update stream, not shared
# application state (same "small local file, not a DB row" choice as
# bot.pid). Surviving a restart without replaying every update ever sent
# matters (Telegram keeps undelivered updates for ~24h); losing it just
# means re-fetching from the start of that window, not a correctness bug —
# resolve_wallet_approval_request's already-resolved check makes a replayed
# callback_query a safe no-op either way.
OFFSET_FILE = os.path.join(config.BASE_DIR, "data", "telegram_update_offset.txt")

CALLBACK_DATA_RE = re.compile(r"^wa:([^:]+):(approve|reject)$")


class TelegramApiError(Exception):
    pass


def load_offset():
    try:
        with open(OFFSET_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


def _telegram_api_call(method, payload, timeout=15, raise_on_failure=False):
    """POST to the Telegram Bot API, JSON-encoded. Returns the parsed
    'result' field on success. On failure (network error, non-2xx,
    unparseable body, ok:false): returns None by default (best-effort,
    matching telegram_alerts.py's whole-module "never raise into the
    caller" contract) — pass raise_on_failure=True for get_updates, where a
    swallowed failure would otherwise be indistinguishable from "genuinely
    no updates right now" and defeat main()'s reconnect backoff.
    """
    token = telegram_alerts.TELEGRAM_BOT_TOKEN
    if not token:
        if raise_on_failure:
            raise TelegramApiError("TELEGRAM_BOT_TOKEN not configured")
        return None

    body = json.dumps(payload).encode("utf-8")
    try:
        conn = http.client.HTTPSConnection("api.telegram.org", timeout=timeout)
        try:
            # Same IPv4-forcing scope telegram_alerts.py needs on this EC2
            # box — see _force_ipv4_dns()'s docstring there for why.
            with telegram_alerts._force_ipv4_dns():
                conn.request(
                    "POST", f"/bot{token}/{method}",
                    body=body, headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
            raw_body = response.read()
            if not (200 <= response.status < 300):
                message = f"{method} failed: HTTP {response.status}"
                if raise_on_failure:
                    raise TelegramApiError(message)
                logger.warning(message)
                return None
            data = json.loads(raw_body)
            if not data.get("ok"):
                message = f"{method} failed: {data}"
                if raise_on_failure:
                    raise TelegramApiError(message)
                logger.warning(message)
                return None
            return data.get("result")
        finally:
            conn.close()
    except (http.client.HTTPException, OSError, ValueError) as e:
        if raise_on_failure:
            raise TelegramApiError(f"{method} failed: {e}") from e
        logger.warning(f"{method} failed: {e}")
        return None


def get_updates(offset, timeout_seconds):
    """Long-polls for new updates starting at `offset`, restricted to
    callback_query updates only (allowed_updates) — this bot has no other
    use for messages/commands/etc. Raises TelegramApiError on any failure
    (see _telegram_api_call) so main()'s backoff engages correctly instead
    of treating "call failed" the same as "no updates right now."
    """
    result = _telegram_api_call(
        "getUpdates",
        {"offset": offset, "timeout": timeout_seconds, "allowed_updates": ["callback_query"]},
        timeout=timeout_seconds + 10,
        raise_on_failure=True,
    )
    return result or []


def answer_callback_query(callback_query_id, text):
    """Acks the tap so Telegram's client-side loading spinner clears.
    Best-effort — a failed ack doesn't undo the already-applied approval/
    rejection, it's purely a UX nicety."""
    _telegram_api_call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


def edit_message_text(chat_id, message_id, text):
    """Rewrites the original candidate message to show the outcome and
    (by omitting reply_markup entirely) removes the Approve/Reject buttons
    — Telegram treats a missing reply_markup on editMessageText as "no
    markup," not "leave the old one." Best-effort, same reasoning as
    answer_callback_query."""
    _telegram_api_call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})


def parse_callback_data(data):
    """Parses 'wa:{request_id}:approve|reject' into (request_id, action),
    or None if the string doesn't match this shape — e.g. a stray callback
    from some other bot feature, or a malformed/tampered payload. Never
    raises.
    """
    if not data:
        return None
    match = CALLBACK_DATA_RE.match(data)
    if not match:
        return None
    return match.group(1), match.group(2)


def is_authorized_chat(chat_id):
    """True only if chat_id matches the configured TELEGRAM_CHAT_ID exactly
    — this bot must not let a stranger who somehow messages it approve a
    real-money wallet. Both sides are coerced to str: Telegram delivers
    chat_id as a JSON integer, TELEGRAM_CHAT_ID (loaded from .env) is a
    string. Returns False (never authorized) if TELEGRAM_CHAT_ID isn't
    configured at all.
    """
    expected = telegram_alerts.TELEGRAM_CHAT_ID
    if not expected:
        return False
    return str(chat_id) == str(expected)


def format_resolution_text(original_text, action, requested_tier=None):
    if action == "approve":
        outcome = f"✅ Approved — now '{requested_tier}'" if requested_tier else "✅ Approved"
    else:
        outcome = "❌ Rejected"
    return f"{original_text}\n\n{outcome}"


def handle_callback_query(callback_query):
    """Processes one callback_query update end to end: authorizes the chat,
    parses the callback_data, resolves the wallet_approval_request (the
    only place wallet_profile.status actually changes as a result of this
    workflow), and edits the original message to show the outcome. Every
    step is best-effort/logged — a single bad update must never crash the
    long-poll loop (see main()).
    """
    callback_id = callback_query.get("id")
    message = callback_query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    data = callback_query.get("data")

    if not is_authorized_chat(chat_id):
        logger.warning(f"ignoring callback_query from unauthorized chat_id={chat_id!r}")
        if callback_id:
            answer_callback_query(callback_id, "Not authorized.")
        return

    parsed = parse_callback_data(data)
    if parsed is None:
        logger.warning(f"ignoring callback_query with unrecognized data={data!r}")
        if callback_id:
            answer_callback_query(callback_id, "Unrecognized action.")
        return

    request_id, action = parsed
    request = db.get_wallet_approval_request(request_id)
    if request is None:
        logger.warning(f"callback_query for unknown wallet_approval_request id={request_id!r}")
        if callback_id:
            answer_callback_query(callback_id, "This request no longer exists.")
        return

    resolved_status = "approved" if action == "approve" else "rejected"
    changed = db.resolve_wallet_approval_request(request_id, resolved_status)

    if not changed:
        # Double-tap, or a replayed getUpdates offset re-delivering an
        # already-handled callback — resolve_wallet_approval_request's own
        # "no longer pending" guard is the real safety net here.
        if callback_id:
            answer_callback_query(callback_id, "Already handled.")
        return

    logger.info(f"{resolved_status}: {request['wallet_address']} -> {request['requested_tier']} "
                f"(request_id={request_id})")

    if callback_id:
        answer_callback_query(callback_id, "Approved." if action == "approve" else "Rejected.")

    original_text = message.get("text") or f"{request['wallet_address']} ({request['requested_tier']})"
    new_text = format_resolution_text(original_text, action, request["requested_tier"])
    if chat_id is not None and message_id is not None:
        edit_message_text(chat_id, message_id, new_text)


def main():
    if not telegram_alerts.TELEGRAM_BOT_TOKEN or not telegram_alerts.TELEGRAM_CHAT_ID:
        logger.critical("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set -- nothing to listen for. Exiting.")
        sys.exit(1)

    offset = load_offset()
    logger.info(f"telegram_approval_listener starting, offset={offset}")

    backoff = RECONNECT_BACKOFF_INITIAL_SECONDS
    while True:
        try:
            updates = get_updates(offset, LONG_POLL_TIMEOUT_SECONDS)
            backoff = RECONNECT_BACKOFF_INITIAL_SECONDS  # reset after any successful poll
        except TelegramApiError as e:
            logger.error(f"getUpdates failed: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SECONDS)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            callback_query = update.get("callback_query")
            if callback_query:
                try:
                    handle_callback_query(callback_query)
                except Exception as e:
                    logger.error(f"failed to handle callback_query: {e}", exc_info=True)
            save_offset(offset)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("telegram_approval_listener stopped.")
