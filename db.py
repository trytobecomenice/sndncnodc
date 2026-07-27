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
storage backend does. One deliberate exception (2026-07-23, point 3.2
prerequisite): append_log() now RETURNS the new decision_journal row's id
(None when no such row was written) — there is no JSON-file equivalent to
preserve parity with here, since decision_journal/outcome_review are new,
SQLite-only concepts the old state.json/trades_log.json never had. See
save_state()'s decision_journal_id/linked_paper_trade_id handling below.

Ownership boundary (see docs/copy-trading/SAFETY.md): this module writes
wallet_profile.circuit_breaker_muted/mute_reason/muted_at/consecutive_losses/
recent_results_json (bot.py's own circuit-breaker fields) but NEVER
wallet_profile.status — that column belongs to the TS leaderboard-scan/
scoring layer (packages/copy-trading). Mixing writers on one column is how
the two systems would silently fight each other.
"""

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import config

# Named logger, no handlers attached HERE (2026-07-22, disk-exhaustion
# hardening — replaces append_log()'s old print()). Deliberately relies on
# propagation to whatever the CALLING process already configured under the
# "copybot" name (bot.py sets up "copybot" with a RotatingFileHandler at
# import time) rather than attaching its own handler pointed at the same
# file: two independent RotatingFileHandler instances both watching
# bot.out.log would each track the file's size via their own file handle,
# and could rotate at slightly different moments — the SAME rotation-
# blindness bug fixed in dashboard.py's start_bot(), just reintroduced a
# different way. If db.py is ever used from a context that never imports
# bot.py (a standalone script), this falls back to Python's own
# logging.lastResort default (stderr) rather than crashing — a minor,
# acceptable behavior difference from the old print(), not a new failure
# mode.
logger = logging.getLogger("copybot.db")

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
            "avg_entry_price, buy_count, peak_profit_pct, opened_at, last_priced_at FROM paper_trade "
            "WHERE status = 'open' AND strategy = 'bot_filtered' AND is_demo_data = 0"
        )
        for row in cur.fetchall():
            key = f"{row['wallet_address']}|{row['market_slug']}|{row['outcome']}"
            # last_priced_at falls back to opened_at (2026-07-27, zombie-
            # position dump exit): a row from before this column existed, or
            # one that's simply never had a successful TTP price read yet,
            # must start its zombie clock from when the position was
            # actually opened -- NOT from None/0, which would make it look
            # infinitely stale and eligible for a forced exit immediately.
            positions[key] = {
                "shares": row["our_shares"],
                "cost_basis_usd": row["cost_basis_usd"],
                "avg_entry_price": row["avg_entry_price"],
                "buy_count": row["buy_count"],
                "peak_profit_pct": row["peak_profit_pct"],
                "last_priced_at": row["last_priced_at"] or row["opened_at"],
            }

        source_positions = {}
        source_cost_basis = {}
        cur = conn.execute("SELECT key, shares, cost_basis_usd FROM bot_source_position")
        for row in cur.fetchall():
            # Normalize the trader portion of the key to lowercase on load
            # (2026-07-27 bug fix, found during a full audit -- the SAME
            # casing-fragmentation bug Rule 34 fixed for paper_trade/
            # bot_event_log/decision_journal, missed here because this
            # table is fully replaced on every save_state() call, which
            # made it LOOK self-healing -- but load_state() never
            # normalized what it read back, so a stale pre-fix mixed-case
            # row just kept getting loaded and re-saved forever, and any
            # fresh post-fix trade for that same wallet created a SEPARATE
            # lowercase entry alongside it rather than replacing it.
            # Confirmed live: 150 (wallet, market, outcome) triples had
            # BOTH casings simultaneously, meaning source_positions held
            # two inconsistent views of the same whale's real holdings --
            # this fed directly into fraction_sold's accuracy on SELLs.
            # Summed (not overwritten) when two DB rows fold to the same
            # lowercase key, since both represent real, already-happened
            # trade history that must not be discarded.
            parts = row["key"].split("|")
            if len(parts) == 3:
                key = f"{parts[0].lower()}|{parts[1]}|{parts[2]}"
            else:
                key = row["key"]
            source_positions[key] = source_positions.get(key, 0.0) + row["shares"]
            source_cost_basis[key] = source_cost_basis.get(key, 0.0) + row["cost_basis_usd"]

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
                # In-memory key renamed "recent_returns" (2026-07-26, Rule 35
                # EV-based circuit breaker) -- the DB column name is
                # unchanged (recent_results_json/consecutive_losses, no
                # migration needed), but what's stored inside the JSON list
                # changed from booleans (win/loss) to floats (pnl_usd/
                # cost_basis_usd per real trade) -- see
                # bot.check_circuit_breaker()'s own docstring.
                # consecutive_losses is no longer read: the new trigger is a
                # t-test over recent_returns, not a streak count.
                trader_performance[addr] = {
                    "recent_returns": json.loads(row["recent_results_json"]),
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
            "source_cost_basis": source_cost_basis,
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
        # rather than diffing row by row. source_cost_basis (2026-07-24, the
        # pending_execution VWAP anchor) rides along in the same table/same
        # replace — see bot_source_position.cost_basis_usd's schema comment.
        conn.execute("DELETE FROM bot_source_position")
        source_cost_basis = state.get("source_cost_basis", {})
        for key, shares in state.get("source_positions", {}).items():
            conn.execute(
                "INSERT INTO bot_source_position (key, shares, cost_basis_usd) VALUES (?, ?, ?)",
                (key, shares, source_cost_basis.get(key, 0.0)),
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
            last_priced_at = pos.get("last_priced_at")
            last_priced_at = int(last_priced_at) if last_priced_at is not None else None
            # decision_journal linkage (2026-07-23, point 3.2 prerequisite —
            # see docs/copy-trading/RISK_MANAGEMENT.md Rule 22). A position
            # can receive multiple buys (config.MAX_BUYS_PER_TRADER_OUTCOME),
            # so this is many-to-one, not 1:1: paper_trade.decision_journal_id
            # records only the OPENING decision (set once, on INSERT below);
            # decision_journal.linked_paper_trade_id is set for WHICHEVER
            # decision most recently touched this position (open or
            # average-up), every time — see bot.py's process_trade() for
            # where last_decision_journal_id gets set on the position dict.
            # None when a persist() call wasn't preceded by a fresh decision
            # (e.g. a TTP/closeout sweep) — re-linking the same id is a
            # harmless no-op in that case, not treated as an error.
            last_decision_journal_id = pos.get("last_decision_journal_id")
            row_id = existing.get((trader, market_slug, outcome))
            if row_id:
                conn.execute(
                    "UPDATE paper_trade SET our_shares=?, cost_basis_usd=?, avg_entry_price=?, "
                    "buy_count=?, peak_profit_pct=?, last_priced_at=? WHERE id=?",
                    (shares, cost_basis, avg_entry, buy_count, peak, last_priced_at, row_id),
                )
            else:
                row_id = _new_id()
                conn.execute(
                    "INSERT INTO paper_trade (id, strategy, wallet_address, market_slug, outcome, "
                    "our_size_usd, cost_basis_usd, our_shares, avg_entry_price, buy_count, status, "
                    "opened_at, peak_profit_pct, last_priced_at, decision_journal_id) "
                    "VALUES (?, 'bot_filtered', ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                    (
                        row_id, trader, market_slug, outcome, cost_basis, cost_basis,
                        shares, avg_entry, buy_count, _now_ts(), peak, last_priced_at or _now_ts(),
                        last_decision_journal_id,
                    ),
                )

            if last_decision_journal_id:
                conn.execute(
                    "UPDATE decision_journal SET linked_paper_trade_id = ? WHERE id = ?",
                    (row_id, last_decision_journal_id),
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
                    # consecutive_losses always 0 now (2026-07-26, Rule 35):
                    # the new circuit breaker doesn't track a streak count,
                    # only the returns list below. Column kept, not
                    # migrated away -- nothing reads it as meaningful
                    # anymore.
                    0,
                    json.dumps(perf.get("recent_returns", [])) if perf is not None else None,
                    _now_ts(), _now_ts(),
                ),
            )

        conn.commit()
    finally:
        conn.close()


def load_shadow_positions():
    """Shadow Rehab (2026-07-27, Rule 37): same shape/keying as
    load_state()'s positions (wallet|market_slug|outcome -> shares/
    cost_basis_usd/avg_entry_price/buy_count), but sourced from
    strategy='shadow_rehab' rows -- an isolated, paper-only ledger
    simulating what copying a MUTED wallet's real trades would have
    earned. Never read by any real risk/exposure calculation (those all
    filter on strategy='bot_filtered' exclusively, confirmed by grep
    before this was added) -- a wrong number here can only affect a
    future rehab decision, never real capital.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT wallet_address, market_slug, outcome, our_shares, cost_basis_usd, "
            "avg_entry_price, buy_count FROM paper_trade "
            "WHERE status = 'open' AND strategy = 'shadow_rehab' AND is_demo_data = 0"
        )
        shadow_positions = {}
        for row in cur.fetchall():
            key = f"{row['wallet_address']}|{row['market_slug']}|{row['outcome']}"
            shadow_positions[key] = {
                "shares": row["our_shares"],
                "cost_basis_usd": row["cost_basis_usd"],
                "avg_entry_price": row["avg_entry_price"],
                "buy_count": row["buy_count"],
            }
        return shadow_positions
    finally:
        conn.close()


def save_shadow_positions(shadow_positions):
    """Diffs shadow_positions against strategy='shadow_rehab' rows -- same
    insert/update/fail-safe-close pattern as save_state()'s positions
    handling, deliberately scoped to just this one dict (no
    source_positions/trader_performance/muted_traders/decision_journal
    linkage: shadow trades aren't real copy decisions in the Rule 22
    sense, they're a simulation).
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT id, wallet_address, market_slug, outcome FROM paper_trade "
            "WHERE status = 'open' AND strategy = 'shadow_rehab' AND is_demo_data = 0"
        )
        existing = {
            (r["wallet_address"], r["market_slug"], r["outcome"]): r["id"] for r in cur.fetchall()
        }

        seen_keys = set()
        for key, pos in shadow_positions.items():
            parts = key.split("|")
            if len(parts) != 3:
                continue
            trader, market_slug, outcome = parts
            seen_keys.add((trader, market_slug, outcome))
            shares = pos.get("shares", 0.0)
            cost_basis = pos.get("cost_basis_usd", 0.0)
            avg_entry = pos.get("avg_entry_price", 0.0)
            buy_count = pos.get("buy_count", 0)
            row_id = existing.get((trader, market_slug, outcome))
            if row_id:
                conn.execute(
                    "UPDATE paper_trade SET our_shares=?, cost_basis_usd=?, avg_entry_price=?, "
                    "buy_count=? WHERE id=?",
                    (shares, cost_basis, avg_entry, buy_count, row_id),
                )
            else:
                conn.execute(
                    "INSERT INTO paper_trade (id, strategy, wallet_address, market_slug, outcome, "
                    "our_size_usd, cost_basis_usd, our_shares, avg_entry_price, buy_count, status, "
                    "opened_at) VALUES (?, 'shadow_rehab', ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                    (
                        _new_id(), trader, market_slug, outcome, cost_basis, cost_basis,
                        shares, avg_entry, buy_count, _now_ts(),
                    ),
                )

        # Same fail-safe as save_state(): a row still open here with a key
        # missing from shadow_positions means something closed it without
        # going through append_log.
        for (trader, market_slug, outcome), row_id in existing.items():
            if (trader, market_slug, outcome) not in seen_keys:
                conn.execute(
                    "UPDATE paper_trade SET status='closed', closed_at=?, "
                    "close_reason=COALESCE(close_reason, 'reconciled_missing_from_state') "
                    "WHERE id=?",
                    (_now_ts(), row_id),
                )

        conn.commit()
    finally:
        conn.close()


def get_shadow_rehab_returns(wallet_address, limit=None):
    """Returns the per-trade RETURNS (realized_pnl_usd/cost_basis_usd,
    most-recent-first) for `wallet_address`'s closed strategy='shadow_rehab'
    trades -- the exact input bot.compute_wallet_ev_t_statistic() needs to
    decide whether a muted wallet has earned reinstatement. `limit`, when
    given, caps to the most recent N (mirrors config.MUTE_EV_MIN_SAMPLES'
    rolling-window role on the mute side).
    """
    conn = _connect()
    try:
        query = (
            "SELECT realized_pnl_usd, cost_basis_usd FROM paper_trade "
            "WHERE wallet_address = ? AND strategy = 'shadow_rehab' AND status = 'closed' "
            "AND is_demo_data = 0 ORDER BY closed_at DESC"
        )
        params = [wallet_address.lower()]
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [
            row["realized_pnl_usd"] / row["cost_basis_usd"]
            for row in rows if row["cost_basis_usd"]
        ]
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


# event_type -> (close_reason, is_always_full_close, strategy). paper_sell/
# live_sell/shadow_rehab_sell can be partial (fraction_sold < 1, position
# stays open at reduced size) — those are only a full close when
# our_shares_remaining has hit ~0. strategy (2026-07-27, Shadow Rehab)
# scopes the UPDATE below to the right ledger — "shadow_rehab" closes must
# never touch a real "bot_filtered" row and vice versa, even if a wallet
# somehow had the same (market_slug, outcome) open in both simultaneously.
_CLOSE_REASON_BY_EVENT = {
    "paper_sell_trailing_tp": ("trailing_tp", True, "bot_filtered"),
    "live_sell_trailing_tp": ("trailing_tp", True, "bot_filtered"),
    "position_resolved": ("resolved", True, "bot_filtered"),
    "paper_sell": ("source_sell", False, "bot_filtered"),
    "live_sell": ("source_sell", False, "bot_filtered"),
    "shadow_rehab_sell": ("source_sell", False, "shadow_rehab"),
}


def _maybe_close_paper_trade(conn, event):
    event_type = event.get("event_type")
    mapping = _CLOSE_REASON_BY_EVENT.get(event_type)
    if not mapping:
        return
    close_reason, always_full, strategy = mapping
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
        "AND status='open' AND strategy=? AND is_demo_data=0",
        (_now_ts(), close_reason, event.get("pnl_usd"), trader, market_slug, outcome, strategy),
    )


def append_log(event):
    """Same call signature as the old JSON-backed append_log(event): inserts
    the raw event into bot_event_log (successor of trades_log.json), plus:
    - a DecisionJournal row, for events that represent a copy/skip decision
    - a paper_trade close (status/closedAt/closeReason/realizedPnlUsd), for
      events that fully close a position

    Returns the new decision_journal row's id, or None if this event didn't
    produce one (not a copy/skip decision) — see module docstring for why
    this is a deliberate departure from strict JSON-file-era parity. Two
    OPTIONAL event fields feed the new decision_journal columns (added
    2026-07-23, point 3.2 prerequisite — see docs/copy-trading/
    RISK_MANAGEMENT.md Rule 22): `score_breakdown` (a dict — the score(s)
    that actually drove compute_trade_size_usd()'s sizing decision, snapshot
    at decision time; caller's responsibility, this function just persists
    whatever it's given) and `rule_set_version` (the active wallet-scorer
    rule_set version at decision time, from get_active_rule_set_version()).
    Both are simply absent (NULL) for events that don't pass them — no
    scoring/skip logic here changes based on their presence.
    """
    conn = _connect()
    decision_journal_id = None
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
            decision_journal_id = _new_id()
            score_breakdown = event.get("score_breakdown")
            conn.execute(
                "INSERT INTO decision_journal (id, created_at, wallet_address, market_slug, "
                "outcome, side, decision_type, decision_reason, score_breakdown_json, "
                "rule_set_version, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_journal_id, _now_ts(), event["trader_address"], event["market_slug"],
                    event["outcome"], event.get("side"), decision_type,
                    event.get("reason") or event.get("event_type"),
                    json.dumps(score_breakdown) if score_breakdown is not None else None,
                    event.get("rule_set_version"), "bot.py",
                ),
            )

        _maybe_close_paper_trade(conn, event)
        conn.commit()
    finally:
        conn.close()

    logger.info(f"[{event['timestamp']}] {event['event_type']}: {event.get('market_slug', '')} {event.get('outcome', '')}")
    return decision_journal_id


def prune_event_log(retention_days=None):
    """Deletes bot_event_log rows older than retention_days (default
    config.EVENT_LOG_RETENTION_DAYS). Added 2026-07-22 as the actual fix
    for this table's unbounded growth — NOT logging.handlers.
    RotatingFileHandler, which cannot attach to a database table at all
    (this table is written via direct SQL INSERT above, never through
    Python's `logging` module). Age-based rather than a row-count cap: a
    fixed retention WINDOW keeps recent history useful for debugging/
    shortfall-PnL analysis, which an arbitrary row ceiling wouldn't
    guarantee (a burst of activity could evict genuinely recent rows under
    a row cap; it never can under an age cutoff).

    Returns the number of rows deleted, so the caller (bot.py's periodic
    sweep) can log something meaningful rather than a silent no-op.
    """
    if retention_days is None:
        retention_days = config.EVENT_LOG_RETENTION_DAYS
    cutoff = _now_ts() - retention_days * 86400
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM bot_event_log WHERE timestamp < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


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


def get_wallet_composite_scores():
    """Returns {wallet_address_lower: {"composite": score_or_None,
    "composite_win_rate": win_rate_or_None, "composite_trade_count":
    trade_count_or_None, "categories": {category: {"score": score_or_None,
    "pnl_t_stat": t_stat_or_None, "win_rate": win_rate_or_None,
    "trade_count": trade_count_or_None}}}} for every row in wallet_profile —
    used for half-Kelly position sizing (config.py's KELLY_*/MIN/MAX/
    BASE_TRADE_USD, bot.compute_trade_size_usd()) AND the hard-skip decision
    (config.CATEGORY_SKIP_Z_CRITICAL, bot.should_skip_category()).

    "categories" (added 2026-07-22 for category-specific scoring, extended
    2026-07-23 with pnl_t_stat, extended 2026-07-24 with win_rate/trade_count
    for Rule 22 Part C's Brier calibration) is parsed from
    category_scores_json — a JSON object like {"politics": {"score": 0.72,
    "trade_count": 34, "win_rate": 0.65, "pnl_t_stat": -2.1, ...}, ...},
    written by scoreWalletCategories.ts. Only these four fields are surfaced
    here; the full breakdown (avg_pnl_usd, roi, avg_entry_price,
    avg_fee_rate) stays in the DB for anything that wants it later.
    "categories" is {} (not missing) for a wallet with no
    category_scores_json yet, or if it fails to parse — malformed JSON here
    is a scoring-pipeline bug, not something that should crash bot.py's
    startup, so this degrades to "no category signal" rather than raising.

    "composite_win_rate"/"composite_trade_count" (added 2026-07-24 for
    half-Kelly sizing's composite-tier fallback — see bot.
    compute_trade_size_usd()): the wallet's LIFETIME rolling win rate and
    trade count (wallet_profile.win_rate/trade_count_all_time, both real
    columns scoreWallets.ts already populates but this function never
    selected before). Distinct from any single category's win_rate/
    trade_count above — used only when no category-specific data exists for
    the market being copied.

    Deliberately independent of config.TRACKED_TRADERS_SOURCE: a wallet can
    be tracked via the static config.py list yet still have a real score
    in wallet_profile from a past scan:wallets run (the two aren't the same
    gate — this one only affects sizing, never membership/eligibility).
    Lowercased keys since bot.py's trader addresses are compared
    case-insensitively everywhere else (see tracked_by_lower).

    Called once at bot.py startup, matching get_tracked_traders()'s
    restart-to-pick-up-changes design — a wallet rescored mid-session keeps
    its old size until the next restart, not silently mid-flight.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT wallet_address, composite_score, win_rate, trade_count_all_time, "
            "category_scores_json FROM wallet_profile"
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    result = {}
    for row in rows:
        categories = {}
        raw = row["category_scores_json"]
        if raw:
            try:
                parsed = json.loads(raw)
                categories = {
                    cat: {
                        "score": detail.get("score"),
                        "pnl_t_stat": detail.get("pnl_t_stat"),
                        "win_rate": detail.get("win_rate"),
                        "trade_count": detail.get("trade_count"),
                    }
                    for cat, detail in parsed.items()
                }
            except (json.JSONDecodeError, AttributeError):
                categories = {}
        result[row["wallet_address"].lower()] = {
            "composite": row["composite_score"],
            "composite_win_rate": row["win_rate"],
            "composite_trade_count": row["trade_count_all_time"],
            "categories": categories,
        }
    return result


def get_muted_wallets():
    """Every wallet_address currently circuit_breaker_muted=1, lowercased.
    Used by propose_pool_refill.py to compute the real "actively copying"
    count (tracked minus muted) -- same source of truth
    check_circuit_breaker()/sweep_shadow_rehab() maintain, just read
    directly rather than requiring bot.py's in-memory muted_traders dict.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT wallet_address FROM wallet_profile WHERE circuit_breaker_muted = 1"
        ).fetchall()
        return {r["wallet_address"].lower() for r in rows}
    finally:
        conn.close()


def get_ever_tracked_wallets():
    """Every wallet address that has ever had a real (strategy='bot_filtered')
    paper_trade row -- i.e. was tracked and actually copied at some point,
    whether still tracked today or since dropped. Used by
    propose_pool_refill.py to make sure a previously-kicked wallet is never
    re-proposed on the strength of the same track record that got it
    kicked (get_pool_refill_candidates() only excludes what it's told to;
    this is how the caller learns the full "ever tracked" set, not just
    who's tracked right now).
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT wallet_address FROM paper_trade WHERE strategy = 'bot_filtered'"
        ).fetchall()
        return {r["wallet_address"].lower() for r in rows}
    finally:
        conn.close()


def get_pool_refill_candidates(exclude_addresses_lower, min_composite_score, limit=None):
    """Pool-refill candidates (2026-07-27, Rule 37) for
    propose_pool_refill.py — wallet_profile rows scoring at least
    `min_composite_score`, excluding any address in `exclude_addresses_lower`
    (already-tracked wallets AND every wallet EVER tracked-and-dropped, so a
    previously-kicked wallet is never re-proposed on the strength of the
    same track record that got it kicked), ranked best-first.

    "Ever tracked" is determined by the CALLER (propose_pool_refill.py
    unions bot_risk_state["tracked_traders"] with every distinct
    wallet_address that has a strategy='bot_filtered' paper_trade row,
    tracked or not) -- this function only applies the exclusion set and
    score filter, it doesn't compute what belongs in it.

    Returns a list of dicts (wallet_address, nickname, composite_score,
    win_rate, trade_count_all_time, category), best composite_score first.
    """
    conn = _connect()
    try:
        query = (
            "SELECT wallet_address, nickname, composite_score, win_rate, "
            "trade_count_all_time, category FROM wallet_profile "
            "WHERE composite_score >= ? ORDER BY composite_score DESC"
        )
        rows = conn.execute(query, (min_composite_score,)).fetchall()
    finally:
        conn.close()

    excluded = {a.lower() for a in exclude_addresses_lower}
    candidates = [dict(r) for r in rows if r["wallet_address"].lower() not in excluded]
    return candidates[:limit] if limit else candidates


# --- Portfolio-risk state (bot_risk_state / bot_market_event) ----------------
# Owned exclusively by bot.py's risk layer (risk_manager.py) — see the
# ownership notes on these tables in packages/db/src/schema.ts. Known
# bot_risk_state keys: "equity_hwm" (float), "kill_switch" (dict, present =
# new BUYs halted; cleared via reset_kill_switch.py), "auth_halt" (dict,
# present = the poll loop is halted on a dead bullpen session, auto-clears
# itself once `bullpen login` restores a valid session — see bot.py's
# fetch_feed_with_auth_recovery()).


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


def get_active_rule_set_version():
    """Read-only lookup against the TS-owned `rule_set` table (populated
    exclusively by packages/copy-trading/src/scoreWallets.ts's
    getActiveRuleSet() — this module never writes to rule_set, matching the
    same cross-layer write boundary already established for wallet_profile.
    status). Added 2026-07-23 (point 3.2 prerequisite): decision_journal
    rows now snapshot this at decision time so a later structural-break
    analysis (see docs/copy-trading/RISK_MANAGEMENT.md Rule 22) can tell
    "the wallet's edge shifted" apart from "we changed the scoring formula
    out from under it." Returns None if no row is marked active yet (e.g. a
    fresh DB before scoreWallets.ts has ever run) — callers must treat that
    as "unknown," never guess a version.
    """
    conn = _connect()
    try:
        cur = conn.execute("SELECT version FROM rule_set WHERE is_active = 1 LIMIT 1")
        row = cur.fetchone()
        return row["version"] if row else None
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


def save_market_event(market_slug, event_slug, holding_rewards_enabled=None):
    """holding_rewards_enabled is optional (default None) purely so existing
    call sites/tests that only care about event_slug don't need updating —
    bot.py's real call site always passes it, sourced at zero extra API cost
    alongside event_slug itself (see resolve_market_event's docstring). Not
    read by any scoring/sizing logic — audit/documentation field only (see
    bot_market_event.holding_rewards_enabled's schema comment).
    """
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO bot_market_event (market_slug, event_slug, holding_rewards_enabled, resolved_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(market_slug) DO UPDATE SET "
            "event_slug = excluded.event_slug, "
            "holding_rewards_enabled = excluded.holding_rewards_enabled, "
            "resolved_at = excluded.resolved_at",
            (market_slug, event_slug, holding_rewards_enabled, _now_ts()),
        )
        conn.commit()
    finally:
        conn.close()


def load_market_categories():
    """Returns the full {market_slug: category} memo — a SEPARATE read from
    load_market_events() (event_slug), even though both live in the
    bot_market_event table (added for category-specific wallet scoring,
    2026-07-22). Kept as its own function rather than folded into
    load_market_events()'s existing {market_slug: event_slug} shape: that
    shape already feeds risk_manager's per-event exposure cap elsewhere in
    bot.py, and this is a genuinely separate concern (wallet-scoring
    sizing, not risk gating) that shouldn't force a shape change on
    something already depended on. Rows with no category resolved yet
    (NULL) are simply absent from the returned dict.
    """
    conn = _connect()
    try:
        cur = conn.execute("SELECT market_slug, category FROM bot_market_event WHERE category IS NOT NULL")
        return {row["market_slug"]: row["category"] for row in cur.fetchall()}
    finally:
        conn.close()


def save_market_category(market_slug, category):
    """Writes just the category column for an EXISTING bot_market_event row.
    An UPDATE, not an upsert: market_slug is that table's primary key, and a
    row only exists once save_market_event() has already resolved its
    event_slug — a market whose event was never resolved has no row here
    to attach a category to yet, so this is a no-op (0 rows affected) in
    that case rather than an error.
    """
    conn = _connect()
    try:
        conn.execute(
            "UPDATE bot_market_event SET category = ? WHERE market_slug = ?",
            (category, market_slug),
        )
        conn.commit()
    finally:
        conn.close()


def load_market_end_dates():
    """Returns the full {market_slug: end_date_iso} memo — a SEPARATE read
    from load_market_events()/load_market_categories(), same reasoning as
    the latter's own comment: a genuinely separate concern (Priority 4's
    theta-decay TTP activation, 2026-07-26) that shouldn't force a shape
    change on the existing dicts. Rows with no end date resolved yet (NULL)
    are simply absent from the returned dict.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT market_slug, end_date_iso FROM bot_market_event WHERE end_date_iso IS NOT NULL"
        )
        return {row["market_slug"]: row["end_date_iso"] for row in cur.fetchall()}
    finally:
        conn.close()


def save_market_end_date(market_slug, end_date_iso):
    """Writes just the end_date_iso column for an EXISTING bot_market_event
    row — an UPDATE, not an upsert, same precedent as save_market_category():
    a market whose event was never resolved (via save_market_event()) has
    no row here yet, so this is a no-op (0 rows affected), not an error.
    Safe in practice because resolve_market_end_date() is only ever called
    from the TTP sweep against an ALREADY-OPEN position, and a position can
    only exist because process_trade()'s BUY branch already resolved (and
    saved) that market's event first — the row is always there by then.
    """
    conn = _connect()
    try:
        conn.execute(
            "UPDATE bot_market_event SET end_date_iso = ? WHERE market_slug = ?",
            (end_date_iso, market_slug),
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


# --- pending_execution (2026-07-24, RISK_MANAGEMENT.md Rule 29 — "Dip &
# Rebound" resting paper orders) -------------------------------------------

def create_pending_execution(wallet_address, market_slug, outcome, source_trade_id,
                              category, anchor_price, whale_shares_at_creation,
                              target_usd, expires_at):
    """Inserts a new 'pending' row. expires_at is a unix timestamp (int),
    computed by the caller (bot.py) as now + config.LIMIT_ORDER_TTL_SECONDS.
    Returns the new row's id.
    """
    conn = _connect()
    try:
        row_id = _new_id()
        conn.execute(
            "INSERT INTO pending_execution (id, wallet_address, market_slug, outcome, "
            "source_trade_id, category, anchor_price, whale_shares_at_creation, target_usd, "
            "status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (row_id, wallet_address, market_slug, outcome, source_trade_id, category,
             anchor_price, whale_shares_at_creation, target_usd, _now_ts(), expires_at),
        )
        conn.commit()
        return row_id
    finally:
        conn.close()


def get_pending_execution(wallet_address, market_slug, outcome, status="pending"):
    """The single active row (if any) for one wallet+market+outcome key —
    used by process_trade to decide whether a new BUY signal should ratchet
    an existing order's anchor down or create a fresh one. None if no such
    row exists. Callers must not assume at most one 'pending' row can ever
    exist for a key across all time (filled/expired/invalidated rows for the
    same key are left in place, not deleted) — only the current 'pending'
    one, which this filters to.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM pending_execution WHERE wallet_address = ? AND market_slug = ? "
            "AND outcome = ? AND status = ? ORDER BY created_at DESC LIMIT 1",
            (wallet_address, market_slug, outcome, status),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_pending_executions(status="pending"):
    """All rows in the given status, oldest first — the TTL sweep's input.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM pending_execution WHERE status = ? ORDER BY created_at ASC", (status,)
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def update_pending_execution_anchor(pending_execution_id, anchor_price):
    """Ratchet-down update only — bot.py's compute_anchor_price() is what
    enforces the 'never raises' rule; this function just writes whatever
    value it's given."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE pending_execution SET anchor_price = ? WHERE id = ?",
            (anchor_price, pending_execution_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_pending_execution_lowest_seen(pending_execution_id, lowest_seen_price):
    conn = _connect()
    try:
        conn.execute(
            "UPDATE pending_execution SET lowest_seen_price = ? WHERE id = ?",
            (lowest_seen_price, pending_execution_id),
        )
        conn.commit()
    finally:
        conn.close()


def close_pending_execution(pending_execution_id, status, invalidated_reason=None, filled_at=None):
    """Terminal status transition: 'filled' | 'expired' | 'invalidated'.
    filled_at is a unix timestamp, only meaningful when status='filled'.
    """
    conn = _connect()
    try:
        conn.execute(
            "UPDATE pending_execution SET status = ?, invalidated_reason = ?, filled_at = ? "
            "WHERE id = ?",
            (status, invalidated_reason, filled_at, pending_execution_id),
        )
        conn.commit()
    finally:
        conn.close()


# --- live_whale_event / token_registry (2026-07-24) — the Consumer half of
# wss_listener.py / token_sync_worker.py's Producer-Consumer hand-off. See
# bot.sweep_live_whale_events()'s docstring for the full design, including
# why this joins on token_registry rather than reading live_whale_event
# alone, and why an unmatched row is deliberately left unconsumed. --------

def get_unconsumed_whale_events():
    """INNER JOIN, not LEFT: a live_whale_event row with no token_registry
    match yet (token_sync_worker.py hasn't synced that token_id, or never
    will if the market closed before syncing) is not "fetched" here at all
    — its consumed_at stays NULL and it's naturally retried on the next
    sweep once (if) token_registry catches up, rather than being force-
    processed with a missing market_slug/outcome. Ordered by
    (block_number, log_index) — the on-chain order the trades actually
    happened in, not detection/insert order.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT le.id, le.wallet_address, le.direction, le.usdc_amount, le.price, "
            "le.share_amount, le.tx_hash, le.log_index, le.block_number, "
            "tr.market_slug, tr.outcome "
            "FROM live_whale_event le "
            "INNER JOIN token_registry tr ON le.token_id = tr.token_id "
            "WHERE le.consumed_at IS NULL "
            "ORDER BY le.block_number ASC, le.log_index ASC "
            "LIMIT ?",
            (config.WHALE_EVENT_SWEEP_BATCH_LIMIT,),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def mark_whale_event_consumed(event_id):
    """The idempotency safety net: called unconditionally — success, a risk
    gate blocked it, or an exception was raised — from
    sweep_live_whale_events()'s try/finally. Its own dedicated
    connection+commit, deliberately separate from whatever connection
    process_trade()'s own writes used, so this write can never be rolled
    back by anything else that happened while handling the event, and an
    event is never reprocessed once fetched here.
    """
    conn = _connect()
    try:
        # unixepoch(), NOT CURRENT_TIMESTAMP -- SQLite's CURRENT_TIMESTAMP
        # is a text ISO8601 string, while every other timestamp column in
        # this DB (including this one, consumed_at) is a unix-epoch
        # integer. Using _now_ts() here keeps that consistent rather than
        # writing a differently-typed value into the same column.
        conn.execute(
            "UPDATE live_whale_event SET consumed_at = ? WHERE id = ?",
            (_now_ts(), event_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_unconsumed_whale_events_without_registry_match():
    """The counterpart to get_unconsumed_whale_events()'s INNER JOIN: a LEFT
    JOIN with an explicit `tr.token_id IS NULL` filter, returning exactly
    the unconsumed events that DON'T have a token_registry match yet — the
    'unknown token' on-demand fallback's input (2026-07-25, Rule 30
    addendum). Also capped by WHALE_EVENT_SWEEP_BATCH_LIMIT, same reasoning
    as the matched-events query.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT le.id, le.wallet_address, le.direction, le.usdc_amount, le.price, "
            "le.share_amount, le.tx_hash, le.log_index, le.block_number, le.token_id, "
            "le.detected_at "
            "FROM live_whale_event le "
            "LEFT JOIN token_registry tr ON le.token_id = tr.token_id "
            "WHERE le.consumed_at IS NULL AND tr.token_id IS NULL "
            "ORDER BY le.block_number ASC, le.log_index ASC "
            "LIMIT ?",
            (config.WHALE_EVENT_SWEEP_BATCH_LIMIT,),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def upsert_token_registry_row(token_id, market_slug, outcome):
    """Single-row counterpart to token_sync_worker.py's own batch
    upsert_token_registry_rows() — used by bot.py's on-demand fallback
    (fetch_market_by_token_id() resolved exactly one token_id, not a page
    of many). Same ON CONFLICT upsert shape, same unixepoch() timestamp
    convention.
    """
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO token_registry (token_id, market_slug, outcome, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(token_id) DO UPDATE SET "
            "market_slug = excluded.market_slug, outcome = excluded.outcome, "
            "updated_at = excluded.updated_at",
            (str(token_id), market_slug, outcome, _now_ts()),
        )
        conn.commit()
    finally:
        conn.close()


# --- Bifurcated dynamic order pegging for SELL/exit execution (2026-07-26,
# "Priority 3") -------------------------------------------------------------

def compute_live_edge_pct(min_samples=None):
    """The SAME per-dollar-staked blended EV calculation as the 2026-07-25
    sizing research report (mean of pnl_usd/cost_basis_usd across every
    position_resolved/paper_sell_trailing_tp/paper_sell event) — now live
    in code, not an ad-hoc query, because the slippage-floor calculation
    needs to reference the ACTUAL current edge, not a hardcoded snapshot of
    a number already shown to move meaningfully within hours (+24.8% ->
    +20.9% inside one session).

    Returns None if fewer than min_samples trades have both pnl_usd and
    cost_basis_usd on record — too little data to trust; callers fall back
    to config.ORDER_PEG_FALLBACK_EDGE_PCT in that case (deliberately NOT
    handled inside this function, so a caller that wants the raw "do we
    have enough data" signal can still get it).
    """
    min_samples = min_samples if min_samples is not None else config.LIVE_EDGE_MIN_SAMPLES
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT payload_json FROM bot_event_log WHERE event_type IN "
            "('position_resolved', 'paper_sell_trailing_tp', 'paper_sell')"
        )
        returns = []
        for row in cur.fetchall():
            d = json.loads(row["payload_json"])
            pnl = d.get("pnl_usd")
            cb = d.get("cost_basis_usd")
            if pnl is not None and cb:
                returns.append(pnl / cb)
        if len(returns) < min_samples:
            return None
        return sum(returns) / len(returns)
    finally:
        conn.close()


def create_pending_exit_order(wallet_address, market_slug, outcome, position_key, shares,
                               init_price, floor_price, close_reason, bullpen_order_id=None):
    conn = _connect()
    try:
        row_id = _new_id()
        conn.execute(
            "INSERT INTO pending_exit_order (id, wallet_address, market_slug, outcome, "
            "position_key, shares, init_price, floor_price, current_price, bullpen_order_id, "
            "close_reason, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (row_id, wallet_address, market_slug, outcome, position_key, shares, init_price,
             floor_price, init_price, bullpen_order_id, close_reason, _now_ts()),
        )
        conn.commit()
        return row_id
    finally:
        conn.close()


def get_pending_exit_orders(status="pending"):
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM pending_exit_order WHERE status = ? ORDER BY created_at ASC", (status,)
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def update_pending_exit_order_price(order_id, current_price, bullpen_order_id=None):
    """Called on every reprice (cancel+replace) — bullpen_order_id changes
    each time since canceling and placing a new order gets a new id from
    bullpen; None leaves the existing id untouched (e.g. a price refresh
    that didn't actually require a real reprice yet)."""
    conn = _connect()
    try:
        if bullpen_order_id is not None:
            conn.execute(
                "UPDATE pending_exit_order SET current_price = ?, bullpen_order_id = ?, "
                "last_repriced_at = ? WHERE id = ?",
                (current_price, bullpen_order_id, _now_ts(), order_id),
            )
        else:
            conn.execute(
                "UPDATE pending_exit_order SET current_price = ?, last_repriced_at = ? WHERE id = ?",
                (current_price, _now_ts(), order_id),
            )
        conn.commit()
    finally:
        conn.close()


def close_pending_exit_order(order_id, status, filled_at=None):
    """Terminal transition: 'filled' | 'fallback_market_sell' | 'canceled'."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE pending_exit_order SET status = ?, filled_at = ? WHERE id = ?",
            (status, filled_at, order_id),
        )
        conn.commit()
    finally:
        conn.close()
