#!/usr/bin/env python3
"""
Thin wrapper around the `bullpen` CLI subprocess: JSON invocation, retry
policy, and fill-verification helpers. Extracted out of bot.py so the same
subprocess semantics (exit-code-aware error extraction, timeout-as-unknown-
fill-state, single-shot-by-default retries) are documented and testable in
one place, and so other Python entry points (snapshot_poller.py,
migrate_to_sqlite.py) can reuse them without importing bot.py itself.

packages/bullpen-client's TS twin mirrors this file's behavior for the new
TypeScript research/scoring layer.
"""

import json
import os
import subprocess
import time

import config


if config.PRIVATE_POLYGON_RPC_URL:
    # Applies to every bullpen subprocess call below (feed polling AND live
    # buy/sell) since it's a plain env var bullpen itself reads on startup —
    # see config.PRIVATE_POLYGON_RPC_URL for how this was verified.
    os.environ["BULLPEN_POLYGON_RPC_URL"] = config.PRIVATE_POLYGON_RPC_URL


class BullpenTimeoutError(RuntimeError):
    """The bullpen subprocess hit its timeout. For money-moving calls this
    is fundamentally different from a clean failure: the order MAY have
    executed on-chain even though we never saw the response — callers must
    log it as unknown_fill_state for manual reconciliation, never retry it.
    """


def run_bullpen_json(args, retries=1, retry_delay=0.5, timeout=None):
    """retries=1 means "try once, no retry" — that's the default and MUST stay
    the default for any call that can move funds (buy/sell). Only read-only
    calls (tracker feed) should pass retries>1: a retried buy/sell risks
    double-executing a trade that actually filled but errored on the
    response leg.

    timeout is the hard subprocess ceiling in seconds; None means
    config.BULLPEN_CALL_TIMEOUT_SECONDS (60 — generous ON PURPOSE for
    buy/sell, see that constant's doc comment). Only read-only,
    high-frequency call sites should pass a tighter value (the tracker-
    feed poll passes config.FEED_POLL_TIMEOUT_SECONDS) — never tighten a
    money-moving call, that manufactures unknown_fill_state outcomes.
    """
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return _run_bullpen_json_once(args, timeout)
        except RuntimeError as e:
            last_error = e
            if attempt < retries:
                time.sleep(retry_delay)
    raise last_error


def _run_bullpen_json_once(args, timeout=None):
    if timeout is None:
        timeout = config.BULLPEN_CALL_TIMEOUT_SECONDS
    try:
        result = subprocess.run(
            ["bullpen"] + args + ["--output", "json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # Raised as a RuntimeError subclass so the retry loop above still
        # retries read-only calls, while trade call sites can distinguish
        # "timed out, fill state unknown" from a clean rejection.
        raise BullpenTimeoutError(
            f"bullpen {' '.join(args)} timed out after {timeout}s; "
            f"if this was a trade, the order MAY still have executed"
        )
    data = None
    if result.stdout.strip():
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = None

    # A trade command can exit non-zero (e.g. exit 4 "trade execution
    # failed") while still printing a JSON error body to stdout — check the
    # exit code independent of whether stdout parsed, or a rejected/reverted
    # order silently sails through as if it succeeded.
    if result.returncode != 0:
        detail = result.stderr.strip()
        if isinstance(data, dict):
            detail = data.get("error") or data.get("error_code") or data.get("message") or detail
        raise RuntimeError(
            f"bullpen {' '.join(args)} exited {result.returncode}: {detail or 'no error detail'}"
        )
    if data is None:
        raise RuntimeError(f"bullpen {' '.join(args)} produced no parseable JSON output: {result.stdout!r}")
    if isinstance(data, dict) and data.get("ok") is False:
        raise RuntimeError(f"bullpen {' '.join(args)} error: {data.get('error')}")
    return data


# Statuses `bullpen polymarket buy/sell --output json` can report. Only
# MATCHED means the CLI actually settled shares/cash on-chain — UNMATCHED
# (no counterparty, e.g. swept by a much larger concurrent order), DELAYED,
# or a resting LIVE limit order are not fills. A 0 exit code plus parseable
# JSON only proves the CLI accepted the request, not that it filled.
FILLED_TRADE_STATUSES = {"MATCHED"}


def require_filled(response, action_desc):
    status = str(response.get("status") or "").upper()
    tx_hashes = response.get("transaction_hashes") or []
    if status not in FILLED_TRADE_STATUSES or not tx_hashes:
        raise RuntimeError(
            f"{action_desc} did not confirm an on-chain fill "
            f"(status={status or 'missing'}, transaction_hashes={tx_hashes})"
        )
    return response


def extract_fill_price(response):
    """Best-effort read of the ACTUAL average fill price from a buy/sell
    response. No live fill has ever been captured by this bot yet (all
    history is paper), so the exact field name is unverified — we try the
    plausible candidates and return None if none is present, in which case
    the caller falls back to the source trade's price and logs the raw
    response so the first real fill documents the true shape.
    """
    for key in ("avg_price", "average_price", "fill_price", "executed_price", "price"):
        value = response.get(key)
        if isinstance(value, (int, float)) and 0 < value <= 1:
            return float(value)
    return None
