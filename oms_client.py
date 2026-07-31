#!/usr/bin/env python3
"""Thin HTTP client for the Go OMS service (oms/httpserver.go) — Session 5
of the 4-layer architecture roadmap's Phase 2 (see
.claude/plans/async-questing-oasis.md). Stdlib only (http.client), same
zero-third-party-dependency convention as telegram_alerts.py/
polymarket_simulator.py.

Unlike telegram_alerts.py, this client does NOT fail silently — it raises
OmsClientError on any failure (bad response, connection error, non-2xx
status). That's a deliberate difference: a notification is fire-and-forget
by nature, but an OMS call result is data a caller needs to actually
reason about (did this order get created? what's its real status?), so
swallowing failures here would hide exactly the information a caller
needs. The "never let this break the real path" discipline instead
belongs at each CALL SITE (wrap in try/except there), the same two-layer
split already used for start_shadow_patient_exit() in bot.py — this
module stays honest, callers stay defensive.

Not wired into any bot.py call site yet — a standalone, independently
testable client. See the roadmap doc's Phase 2 section for why the
originally-planned call site (start_patient_exit()/
sweep_pending_exit_orders(), Rule 31 Priority 3) turned out to be the
wrong fit: it's LIVE_MODE-only by construction, so wiring it there would
never actually run against this bot's current paper-only configuration.
"""

import http.client
import json

import config

DEFAULT_TIMEOUT_SECONDS = 5


class OmsClientError(RuntimeError):
    """The OMS HTTP call failed to complete, or returned a non-2xx
    response. Carries whatever detail the server/transport provided in its
    message — no finer-grained exception hierarchy yet (e.g. no separate
    type for a 404 vs a 409 vs a connection failure); callers that need to
    distinguish those cases today have to inspect the message string."""


def _request(method, path, body=None, timeout=None):
    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS
    conn = None
    try:
        conn = http.client.HTTPConnection(config.OMS_HOST, config.OMS_PORT, timeout=timeout)
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        if response.status >= 400:
            detail = parsed.get("error") if isinstance(parsed, dict) else None
            raise OmsClientError(
                f"OMS {method} {path} returned HTTP {response.status}: {detail or raw!r}"
            )
        return parsed
    except (http.client.HTTPException, OSError) as e:
        raise OmsClientError(f"OMS {method} {path} failed: {e}") from e
    finally:
        if conn is not None:
            conn.close()


def create_order(idempotency_key, timeout=None):
    """POST /orders — idempotent (see oms/store.CreateOrder's own
    contract): calling this twice with the SAME idempotency_key returns
    the order created by the first call, never a duplicate. Returns
    {"id": ..., "status": "pending"}.
    """
    return _request("POST", "/orders", body={"idempotency_key": idempotency_key}, timeout=timeout)


def get_order(order_id, timeout=None):
    """GET /orders/{id}. Raises OmsClientError (HTTP 404 in the message)
    if no such order exists."""
    return _request("GET", f"/orders/{order_id}", timeout=timeout)


def cancel_order(order_id, timeout=None):
    """POST /orders/{id}/cancel. Raises OmsClientError on a 409 (the
    order's current state disallows cancellation, e.g. already terminal)
    the same way it raises on any other non-2xx response — there is no
    separate exception type yet for "illegal transition" vs "genuinely
    failed"; a caller that cares has to inspect the message."""
    return _request("POST", f"/orders/{order_id}/cancel", timeout=timeout)


TRANSITIONABLE_STATUSES = {"filled", "expired", "invalidated"}


def transition_order(order_id, to, timeout=None):
    """POST /orders/{id}/transition — mirrors an ALREADY-DECIDED outcome
    into the OMS (2026-08-01, Session 6). `to` must be one of "filled",
    "expired", "invalidated" — matches the Go server's own
    transitionableStatuses (see oms/httpserver.go); checked client-side
    first purely to fail fast with a clear message rather than a round
    trip for an obviously-wrong value, the server re-validates regardless.
    Raises OmsClientError on a 409 (illegal transition, e.g. the order is
    already terminal) the same as any other non-2xx response.
    """
    if to not in TRANSITIONABLE_STATUSES:
        raise ValueError(f"to={to!r} must be one of {sorted(TRANSITIONABLE_STATUSES)}")
    return _request("POST", f"/orders/{order_id}/transition", body={"to": to}, timeout=timeout)
