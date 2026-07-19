#!/usr/bin/env python3
"""
sqlite3 DAO backing bot.py's persistence. Replaces state.json/trades_log.json
(atomic_write_json) with reads/writes against the shared SQLite DB whose
schema/migrations live in packages/db (TS/Drizzle owns all DDL — this module
only ever issues SELECT/INSERT/UPDATE/DELETE, never CREATE/ALTER TABLE).

load_state()/save_state()/append_log() return/accept the EXACT same shapes
bot.py already used with the JSON files, so bot.py's trading logic
(process_trade, check_trailing_take_profit, check_circuit_breaker,
run_closeout_sweep) does not change at all — only these four functions'
storage backend does.

Ownership boundary (see docs/copy-trading/SAFETY.md): this module writes
wallet_profile.circuit_breaker_muted/mute_reason/muted_at/consecutive_losses/
recent_results_json (bot.py's own circuit-breaker fields) but NEVER
wallet_profile.status — that column belongs to the TS leaderboard-scan/
scoring layer (packages/copy-trading). Mixing writers on one column is how
the two systems would silently fight each other.
"""

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import config

SEEN_TRADE_ID_CAP = 2000  # mirrors the old deque(maxlen=2000) in bot.py

# wallet_profile.wallet_address is stored lowercase (normalized so it stays
# consistent with the TS scoring layer, which writes lowercase — see
# save_state() below). positions/source_positions (paper_trade) intentionally
# keep whatever casing the live tracker feed reports (checksummed mixed-case)
# since they're never cross-referenced against wallet_profile by key.
#
# trader_performance/muted_traders, however, round-trip through
# wallet_profile every save/load — so bot.py keys those two dicts by
# LOWERCASE address (see check_circuit_breaker/process_trade in bot.py).
# This used to be handled by translating wallet_profile's lowercase address
# back to config.TRACKED_TRADERS' checksummed casing on load, but that only
# ever worked for the static 20-wallet list: a wallet muted under
# TRACKED_TRADERS_SOURCE="db" (sourced from wallet_profile, not config.py)
# had no checksummed form to translate back to, so `trader in muted_traders`
# would silently stop matching after a restart and un-mute it. Keying both
# sides lowercase sidesteps that entirely instead of trying to recover a
# checksum we don't have (no web3/eth_utils dependency in this project —
# see requirements.txt).


def _connect():
    conn = sqlite3.connect(config.SQLITE_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _new_id():
    return str(uuid.uuid4())


def _now_ts():
    return int(time.time())


def _iso_to_ts(iso_str):
    if not iso_str:
        return None
    try:
        return int(datetime.fromisoformat(iso_str).timestamp())
    except ValueError:
        return None


def _ts_to_iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def load_state():
    """Same return shape as the old JSON-backed load_state(): a dict with
    seen_trade_ids/positions/source_positions/trader_performance/muted_traders.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT trade_id FROM bot_seen_trade ORDER BY seen_at DESC LIMIT ?",
            (SEEN_TRADE_ID_CAP,),
        )
        # DESC-then-reverse so the returned list is oldest-first, matching the
        # order a deque(maxlen=2000) would have held it in.
        seen_trade_ids = [row["trade_id"] for row in cur.fetchall()]
        seen_trade_ids.reverse()

        positions = {}
        cur = conn.execute(
            "SELECT wallet_address, market_slug, outcome, our_shares, cost_basis_usd, "
            "avg_entry_price, buy_count, peak_profit_pct FROM paper_trade "
            "WHERE status = 'open' AND strategy = 'bot_filtered' AND is_demo_data = 0"
        )
        for row in cur.fetchall():
            key = f"{row['wallet_address']}|{row['market_slug']}|{row['outcome']}"
            positions[key] = {
                "shares": row["our_shares"],
                "cost_basis_usd": row["cost_basis_usd"],
                "avg_entry_price": row["avg_entry_price"],
                "buy_count": row["buy_count"],
                "peak_profit_pct": row["peak_profit_pct"],
            }

        source_positions = {}
        cur = conn.execute("SELECT key, shares FROM bot_source_position")
        for row in cur.fetchall():
            source_positions[row["key"]] = row["shares"]

        trader_performance = {}
        muted_traders = {}
        cur = conn.execute(
            "SELECT wallet_address, recent_results_json, consecutive_losses, "
            "circuit_breaker_muted, mute_reason, muted_at FROM wallet_profile"
        )
        for row in cur.fetchall():
            # Lowercase, matching how bot.py now keys trader_performance/
            # muted_traders (see the module-level comment above) — no case
            # translation needed since both sides agree on lowercase.
            addr = row["wallet_address"]
            if row["recent_results_json"] is not None:
                trader_performance[addr] = {
                    "recent_results": json.loads(row["recent_results_json"]),
                    "consecutive_losses": row["consecutive_losses"] or 0,
                }
            if row["circuit_breaker_muted"]:
                muted_traders[addr] = {
                    "muted_at": _ts_to_iso(row["muted_at"]),
                    "reason": row["mute_reason"],
                }

        return {
            "seen_trade_ids": seen_trade_ids,
            "positions": positions,
            "source_positions": source_positions,
            "trader_performance": trader_performance,
            "muted_traders": muted_traders,
        }
    finally:
        conn.close()


def save_state(state):
    """Diffs the in-memory dicts against the DB and writes through. Called
    after every trade and every sweep, exactly as save_state(state) was
    before — see bot.py's persist().
    """
    conn = _connect()
    try:
        for tid in state.get("seen_trade_ids", []):
            conn.execute(
                "INSERT OR IGNORE INTO bot_seen_trade (trade_id, seen_at) VALUES (?, ?)",
                (tid, _now_ts()),
            )
        conn.execute(
            "DELETE FROM bot_seen_trade WHERE trade_id NOT IN "
            "(SELECT trade_id FROM bot_seen_trade ORDER BY seen_at DESC LIMIT ?)",
            (SEEN_TRADE_ID_CAP,),
        )

        # source_positions is small and fully owned here — wholesale replace
        # rather than diffing row by row.
        conn.execute("DELETE FROM bot_source_position")
        for key, shares in state.get("source_positions", {}).items():
            conn.execute(
                "INSERT INTO bot_source_position (key, shares) VALUES (?, ?)", (key, shares)
            )

        cur = conn.execute(
            "SELECT id, wallet_address, market_slug, outcome FROM paper_trade "
            "WHERE status = 'open' AND strategy = 'bot_filtered' AND is_demo_data = 0"
        )
        existing = {
            (r["wallet_address"], r["market_slug"], r["outcome"]): r["id"] for r in cur.fetchall()
        }

        seen_keys = set()
        for key, pos in state.get("positions", {}).items():
            parts = key.split("|")
            if len(parts) != 3:
                continue
            trader, market_slug, outcome = parts
            seen_keys.add((trader, market_slug, outcome))
            shares = pos.get("shares", 0.0)
            cost_basis = pos.get("cost_basis_usd", 0.0)
            avg_entry = pos.get("avg_entry_price", 0.0)
            buy_count = pos.get("buy_count", 0)
            peak = pos.get("peak_profit_pct", 0.0)
            row_id = existing.get((trader, market_slug, outcome))
            if row_id:
                conn.execute(
                    "UPDATE paper_trade SET our_shares=?, cost_basis_usd=?, avg_entry_price=?, "
                    "buy_count=?, peak_profit_pct=? WHERE id=?",
                    (shares, cost_basis, avg_entry, buy_count, peak, row_id),
                )
            else:
                conn.execute(
                    "INSERT INTO paper_trade (id, strategy, wallet_address, market_slug, outcome, "
                    "our_size_usd, cost_basis_usd, our_shares, avg_entry_price, buy_count, status, "
                    "opened_at, peak_profit_pct) "
                    "VALUES (?, 'bot_filtered', ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                    (
                        _new_id(), trader, market_slug, outcome, cost_basis, cost_basis,
                        shares, avg_entry, buy_count, _now_ts(), peak,
                    ),
                )

        # Fail-safe only: append_log's close-event handling (see
        # _maybe_close_paper_trade below) is what normally closes a row the
        # instant a full sell/trailing-tp/resolve event is logged, BEFORE
        # this diff ever runs. A row still open here with a key missing from
        # the dict means something closed it without going through
        # append_log — flag it rather than silently losing the close.
        for (trader, market_slug, outcome), row_id in existing.items():
            if (trader, market_slug, outcome) not in seen_keys:
                conn.execute(
                    "UPDATE paper_trade SET status='closed', closed_at=?, "
                    "close_reason=COALESCE(close_reason, 'reconciled_missing_from_state') "
                    "WHERE id=?",
                    (_now_ts(), row_id),
                )

        trader_performance = state.get("trader_performance", {})
        muted_traders = state.get("muted_traders", {})
        # trader_performance/muted_traders are already lowercase-keyed (see
        # module comment above). config.TRACKED_TRADERS is checksummed
        # mixed-case, so it's normalized into the same lowercase key space
        # here — unioning the raw dicts directly would put the same real
        # wallet in the set twice under two different strings (e.g. a
        # TRACKED_TRADERS_SOURCE="db" wallet that's also in the static list),
        # splitting its perf/mute data and its nickname across two separate
        # writes that then race to overwrite the same row.
        nickname_by_lower = {addr.lower(): nick for addr, nick in config.TRACKED_TRADERS.items()}
        all_traders = set(trader_performance) | set(muted_traders) | set(nickname_by_lower)
        for wallet_address in all_traders:
            perf = trader_performance.get(wallet_address)
            mute = muted_traders.get(wallet_address)
            nickname = nickname_by_lower.get(wallet_address)
            conn.execute(
                # `status` is deliberately never written here — it's owned by
                # the TS scoring layer, not bot.py. Omitting it from both the
                # insert column list and the ON CONFLICT clause means new
                # rows take the schema default ('watch') and existing rows'
                # status is left completely untouched by this process.
                """
                INSERT INTO wallet_profile
                    (id, wallet_address, nickname, circuit_breaker_muted, mute_reason,
                     muted_at, consecutive_losses, recent_results_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_address) DO UPDATE SET
                    nickname = COALESCE(excluded.nickname, wallet_profile.nickname),
                    circuit_breaker_muted = excluded.circuit_breaker_muted,
                    mute_reason = excluded.mute_reason,
                    muted_at = excluded.muted_at,
                    consecutive_losses = excluded.consecutive_losses,
                    recent_results_json = excluded.recent_results_json,
                    updated_at = excluded.updated_at
                """,
                (
                    _new_id(), wallet_address, nickname,
                    1 if mute else 0,
                    mute.get("reason") if mute else None,
                    _iso_to_ts(mute.get("muted_at")) if mute else None,
                    (perf or {}).get("consecutive_losses", 0),
                    json.dumps(perf.get("recent_results", [])) if perf is not None else None,
                    _now_ts(), _now_ts(),
                ),
            )

        conn.commit()
    finally:
        conn.close()


# event_type (+ side, for the ambiguous skip_wide_spread case) -> DecisionJournal
# decisionType. Only events that represent an actual copy/skip decision on a
# BUY signal are journaled here — SELL-side events are position management,
# not a "should we copy this wallet's trade" decision in the spec's sense.
def _decision_type_for_event(event_type, side):
    if event_type in ("paper_buy", "live_buy"):
        return "copy"
    if event_type in ("skip_muted_trader", "skip_duplicate_position"):
        return "skip"
    if event_type == "skip_wide_spread" and side == "BUY":
        return "skip"
    # Portfolio-risk gates (risk_manager.py) — all BUY-only by construction.
    if event_type in ("skip_risk_kill_switch", "skip_risk_exposure_ceiling",
                      "skip_risk_event_cap", "skip_risk_event_unresolved"):
        return "skip"
    # "Disciplined Taker" price ceiling (bot.py's check_slippage_ceiling) —
    # BUY-only by construction (see its docstring: never gates a SELL).
    if event_type == "skip_slippage_ceiling":
        return "skip"
    return None


# event_type -> (close_reason, is_always_full_close). paper_sell/live_sell can
# be partial (fraction_sold < 1, position stays open at reduced size) — those
# are only a full close when our_shares_remaining has hit ~0.
_CLOSE_REASON_BY_EVENT = {
    "paper_sell_trailing_tp": ("trailing_tp", True),
    "live_sell_trailing_tp": ("trailing_tp", True),
    "position_resolved": ("resolved", True),
    "paper_sell": ("source_sell", False),
    "live_sell": ("source_sell", False),
}


def _maybe_close_paper_trade(conn, event):
    event_type = event.get("event_type")
    mapping = _CLOSE_REASON_BY_EVENT.get(event_type)
    if not mapping:
        return
    close_reason, always_full = mapping
    trader = event.get("trader_address")
    market_slug = event.get("market_slug")
    outcome = event.get("outcome")
    if not trader or not market_slug or not outcome:
        return
    if not always_full:
        remaining = event.get("our_shares_remaining", 0.0) or 0.0
        if remaining > 1e-9:
            return  # partial sell — row stays open, save_state() updates its size
    conn.execute(
        "UPDATE paper_trade SET status='closed', closed_at=?, close_reason=?, realized_pnl_usd=? "
        "WHERE wallet_address=? AND market_slug=? AND outcome=? "
        "AND status='open' AND strategy='bot_filtered' AND is_demo_data=0",
        (_now_ts(), close_reason, event.get("pnl_usd"), trader, market_slug, outcome),
    )


def append_log(event):
    """Same call signature as the old JSON-backed append_log(event): inserts
    the raw event into bot_event_log (successor of trades_log.json), plus:
    - a DecisionJournal row, for events that represent a copy/skip decision
    - a paper_trade close (status/closedAt/closeReason/realizedPnlUsd), for
      events that fully close a position
    """
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO bot_event_log (id, timestamp, event_type, trader_address, "
            "market_slug, outcome, side, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id(), _now_ts(), event.get("event_type"), event.get("trader_address"),
                event.get("market_slug"), event.get("outcome"), event.get("side"),
                json.dumps(event),
            ),
        )

        decision_type = _decision_type_for_event(event.get("event_type"), event.get("side"))
        if decision_type and event.get("trader_address") and event.get("market_slug") and event.get("outcome"):
            conn.execute(
                "INSERT INTO decision_journal (id, created_at, wallet_address, market_slug, "
                "outcome, side, decision_type, decision_reason, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_id(), _now_ts(), event["trader_address"], event["market_slug"],
                    event["outcome"], event.get("side"), decision_type,
                    event.get("reason") or event.get("event_type"), "bot.py",
                ),
            )

        _maybe_close_paper_trade(conn, event)
        conn.commit()
    finally:
        conn.close()

    print(f"[{event['timestamp']}] {event['event_type']}: {event.get('market_slug', '')} {event.get('outcome', '')}")


def get_tracked_traders():
    """Returns {address: nickname}, same shape as config.TRACKED_TRADERS.

    config.TRACKED_TRADERS_SOURCE gates the switch from the hardcoded list to
    the TS scoring layer's output (see config.py) — call sites in bot.py
    never need to know which source is active.

    Called once at bot.py startup (main()), not per-poll: this is a
    deliberate restart-to-pick-up-changes design, matching the
    fail-loudly-at-startup framing of the MIN_TRACKED_TRADERS check below.
    bot.py uses the returned dict as BOTH the nickname lookup (as before)
    AND, new as of this function actually being wired in, the authoritative
    membership filter for which trades get copied at all — see
    bot.py main()'s `tracked_by_lower`.
    """
    if config.TRACKED_TRADERS_SOURCE != "db":
        return dict(config.TRACKED_TRADERS)

    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT wallet_address, nickname FROM wallet_profile "
            "WHERE status = 'track' AND circuit_breaker_muted = 0"
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    if len(rows) < config.MIN_TRACKED_TRADERS:
        raise RuntimeError(
            f"TRACKED_TRADERS_SOURCE='db' but only {len(rows)} wallet(s) have "
            f"status='track' (minimum {config.MIN_TRACKED_TRADERS}) — refusing to "
            f"start with a near-empty tracker list. Run scan:leaderboard/scan:wallets "
            f"or set TRACKED_TRADERS_SOURCE back to 'static' in config.py."
        )
    return {row["wallet_address"]: (row["nickname"] or row["wallet_address"]) for row in rows}


# --- Portfolio-risk state (bot_risk_state / bot_market_event) ----------------
# Owned exclusively by bot.py's risk layer (risk_manager.py) — see the
# ownership notes on these tables in packages/db/src/schema.ts. Known
# bot_risk_state keys: "equity_hwm" (float), "kill_switch" (dict, present =
# new BUYs halted; cleared via reset_kill_switch.py).


def get_risk_value(key):
    """Returns the JSON-decoded value for `key`, or None if unset."""
    conn = _connect()
    try:
        cur = conn.execute("SELECT value_json FROM bot_risk_state WHERE key = ?", (key,))
        row = cur.fetchone()
    finally:
        conn.close()
    return json.loads(row["value_json"]) if row else None


def set_risk_value(key, value):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO bot_risk_state (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value), _now_ts()),
        )
        conn.commit()
    finally:
        conn.close()


def clear_risk_value(key):
    conn = _connect()
    try:
        conn.execute("DELETE FROM bot_risk_state WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


def load_market_events():
    """Returns the full {market_slug: event_slug} memo (see bot_market_event)."""
    conn = _connect()
    try:
        cur = conn.execute("SELECT market_slug, event_slug FROM bot_market_event")
        return {row["market_slug"]: row["event_slug"] for row in cur.fetchall()}
    finally:
        conn.close()


def save_market_event(market_slug, event_slug):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO bot_market_event (market_slug, event_slug, resolved_at) "
            "VALUES (?, ?, ?) ON CONFLICT(market_slug) DO UPDATE SET "
            "event_slug = excluded.event_slug, resolved_at = excluded.resolved_at",
            (market_slug, event_slug, _now_ts()),
        )
        conn.commit()
    finally:
        conn.close()


def realized_pnl_total():
    """Total realized PnL across every position close the bot has ever
    logged — one term of the portfolio-equity calculation (see
    risk_manager.compute_equity). Recomputed from bot_event_log on each call
    rather than maintained incrementally: it's a single SUM over an indexed
    scan, called once per TTP sweep (~5 min), and can never drift from the
    log the way a running counter could.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT COALESCE(SUM(json_extract(payload_json, '$.pnl_usd')), 0) AS total "
            "FROM bot_event_log WHERE event_type IN "
            "('paper_sell', 'live_sell', 'paper_sell_trailing_tp', "
            "'live_sell_trailing_tp', 'position_resolved')"
        )
        return float(cur.fetchone()["total"])
    finally:
        conn.close()
