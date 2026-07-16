#!/usr/bin/env python3
"""
One-time migration of state.json/trades_log.json into the shared SQLite DB
(packages/db owns the schema/migrations — this script only inserts/updates
rows, never creates tables).

Idempotent: every insert is either INSERT OR IGNORE on a natural key, or
checks for an existing row first, so re-running against the same source
files after a partial run just fills in whatever's still missing rather
than duplicating rows.

ALWAYS dry-run against copies first:

    cp state.json /tmp/state.json.copy
    cp trades_log.json /tmp/trades_log.json.copy
    python3 migrate_to_sqlite.py --state /tmp/state.json.copy \\
        --log /tmp/trades_log.json.copy --db /tmp/migration_dryrun.db
    # compare printed row counts against the source file's actual counts

Only once those match, stop bot.py/dashboard.py and run for real:

    python3 migrate_to_sqlite.py

See docs/SAFETY.md for the full cutover runbook (why bot.py/dashboard.py
must be stopped first, and how to roll back).
"""
import argparse
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import config

# event_type (+ side, for the ambiguous skip_wide_spread case) -> DecisionJournal
# decisionType. Mirrors db.py's _decision_type_for_event — kept as a literal
# duplicate here rather than imported, since this script must also run
# correctly against arbitrary --state/--log/--db paths that don't necessarily
# match config.py's live paths (a "from db import ..." would tie this script
# to config.SQLITE_PATH as an import-time side effect).
def _decision_type_for_event(event_type, side):
    if event_type in ("paper_buy", "live_buy"):
        return "copy"
    if event_type in ("skip_muted_trader", "skip_duplicate_position"):
        return "skip"
    if event_type == "skip_wide_spread" and side == "BUY":
        return "skip"
    return None


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


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def migrate(state_path, log_path, db_path):
    state = _load_json(state_path, {
        "seen_trade_ids": [], "positions": {}, "source_positions": {},
        "trader_performance": {}, "muted_traders": {},
    })
    log = _load_json(log_path, [])

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row

    counts = {"seen_trade_ids_source": len(state.get("seen_trade_ids", []))}

    # --- bot_seen_trade ---
    n = 0
    for tid in state.get("seen_trade_ids", []):
        cur = conn.execute(
            "INSERT OR IGNORE INTO bot_seen_trade (trade_id, seen_at) VALUES (?, ?)",
            (tid, _now_ts()),
        )
        n += cur.rowcount
    counts["bot_seen_trade_inserted"] = n

    # --- bot_source_position ---
    source_positions = state.get("source_positions", {})
    counts["source_positions_source"] = len(source_positions)
    n = 0
    for key, shares in source_positions.items():
        cur = conn.execute(
            "INSERT OR IGNORE INTO bot_source_position (key, shares) VALUES (?, ?)", (key, shares)
        )
        n += cur.rowcount
    counts["bot_source_position_inserted"] = n

    # --- paper_trade (open positions) ---
    positions = state.get("positions", {})
    counts["positions_source"] = len(positions)
    n = 0
    for key, pos in positions.items():
        parts = key.split("|")
        if len(parts) != 3:
            continue
        trader, market_slug, outcome = parts
        existing = conn.execute(
            "SELECT id FROM paper_trade WHERE wallet_address=? AND market_slug=? AND outcome=? "
            "AND status='open' AND strategy='bot_filtered'",
            (trader, market_slug, outcome),
        ).fetchone()
        if existing:
            continue
        cost_basis = pos.get("cost_basis_usd", 0.0)
        conn.execute(
            "INSERT INTO paper_trade (id, strategy, wallet_address, market_slug, outcome, our_size_usd, "
            "cost_basis_usd, our_shares, avg_entry_price, buy_count, status, opened_at, peak_profit_pct) "
            "VALUES (?, 'bot_filtered', ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (
                _new_id(), trader, market_slug, outcome, cost_basis, cost_basis,
                pos.get("shares", 0.0), pos.get("avg_entry_price", 0.0), pos.get("buy_count", 0),
                _now_ts(), pos.get("peak_profit_pct", 0.0),
            ),
        )
        n += 1
    counts["paper_trade_inserted"] = n

    # --- wallet_profile: one row per TRACKED_TRADERS entry (status='track'),
    # plus any trader that only appears in trader_performance/muted_traders,
    # backfilling circuit-breaker fields from both. `status` is set ONLY
    # here, on first insert — this script is the one-time seed; ongoing
    # writes from db.py/bot.py never touch status again (see db.py's
    # save_state docstring on that ownership boundary).
    trader_performance = state.get("trader_performance", {})
    muted_traders = state.get("muted_traders", {})
    counts["tracked_traders_source"] = len(config.TRACKED_TRADERS)
    all_traders = set(config.TRACKED_TRADERS) | set(trader_performance) | set(muted_traders)
    n_inserted = 0
    n_updated = 0
    for trader in all_traders:
        nickname = config.TRACKED_TRADERS.get(trader)
        perf = trader_performance.get(trader)
        mute = muted_traders.get(trader)
        is_tracked = trader in config.TRACKED_TRADERS
        # Ethereum addresses are case-insensitive, but config.py's
        # TRACKED_TRADERS uses checksummed (mixed-case) addresses while the
        # TS scoring layer normalizes to lowercase before writing to this
        # same shared table — verified this mismatch produces duplicate
        # rows for the same real wallet if left unnormalized (see
        # scoreWallets.ts's normalizeAddress). `trader` itself is left as-is
        # for the TRACKED_TRADERS/trader_performance/muted_traders lookups
        # above; only the value written to wallet_address is normalized.
        wallet_address = trader.lower()
        existing = conn.execute(
            "SELECT id FROM wallet_profile WHERE wallet_address=?", (wallet_address,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE wallet_profile SET nickname=COALESCE(?, nickname), circuit_breaker_muted=?, "
                "mute_reason=?, muted_at=?, consecutive_losses=?, recent_results_json=?, updated_at=? "
                "WHERE wallet_address=?",
                (
                    nickname, 1 if mute else 0, mute.get("reason") if mute else None,
                    _iso_to_ts(mute.get("muted_at")) if mute else None,
                    (perf or {}).get("consecutive_losses", 0),
                    json.dumps(perf.get("recent_results", [])) if perf is not None else None,
                    _now_ts(), wallet_address,
                ),
            )
            n_updated += 1
        else:
            conn.execute(
                "INSERT INTO wallet_profile (id, wallet_address, nickname, status, status_reason, "
                "status_changed_at, circuit_breaker_muted, mute_reason, muted_at, consecutive_losses, "
                "recent_results_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_id(), wallet_address, nickname,
                    "track" if is_tracked else "watch",
                    "seeded from config.TRACKED_TRADERS by migrate_to_sqlite.py" if is_tracked else None,
                    _now_ts() if is_tracked else None,
                    1 if mute else 0, mute.get("reason") if mute else None,
                    _iso_to_ts(mute.get("muted_at")) if mute else None,
                    (perf or {}).get("consecutive_losses", 0),
                    json.dumps(perf.get("recent_results", [])) if perf is not None else None,
                    _now_ts(), _now_ts(),
                ),
            )
            n_inserted += 1
    counts["wallet_profile_inserted"] = n_inserted
    counts["wallet_profile_updated"] = n_updated

    # --- bot_event_log + decision_journal, from trades_log.json ---
    # No field is a reliable unique key across every event type (source_trade_id
    # is absent on e.g. "error"/"bootstrap" events) — dedupe on the full
    # serialized payload instead, which is exact and cheap enough for a
    # one-time migration over a few thousand rows.
    counts["trades_log_source"] = len(log)
    n_events = 0
    n_decisions = 0
    for event in log:
        payload = json.dumps(event)
        existing = conn.execute(
            "SELECT id FROM bot_event_log WHERE payload_json = ?", (payload,)
        ).fetchone()
        if existing:
            continue
        market_slug = event.get("market_slug")
        outcome = event.get("outcome")
        et = event.get("event_type")
        epoch = _iso_to_ts(event.get("timestamp")) or _now_ts()
        conn.execute(
            "INSERT INTO bot_event_log (id, timestamp, event_type, trader_address, market_slug, "
            "outcome, side, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id(), epoch, et, event.get("trader_address"), market_slug, outcome,
                event.get("side"), payload,
            ),
        )
        n_events += 1

        decision_type = _decision_type_for_event(et, event.get("side"))
        if decision_type and event.get("trader_address") and market_slug and outcome:
            conn.execute(
                "INSERT INTO decision_journal (id, created_at, wallet_address, market_slug, outcome, "
                "side, decision_type, decision_reason, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_id(), epoch, event["trader_address"], market_slug, outcome,
                    event.get("side"), decision_type, event.get("reason") or et, "bot.py",
                ),
            )
            n_decisions += 1
    counts["bot_event_log_inserted"] = n_events
    counts["decision_journal_inserted"] = n_decisions

    conn.commit()
    conn.close()
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", default=config.STATE_PATH, help="path to state.json (default: real one)")
    parser.add_argument("--log", default=config.TRADE_LOG_PATH, help="path to trades_log.json (default: real one)")
    parser.add_argument("--db", default=config.SQLITE_PATH, help="path to app.db (default: real one)")
    args = parser.parse_args()

    is_real_run = (
        args.state == config.STATE_PATH and args.log == config.TRADE_LOG_PATH and args.db == config.SQLITE_PATH
    )
    if is_real_run:
        print(
            "WARNING: no --state/--log/--db overrides given — this will migrate the REAL "
            "state.json/trades_log.json into the REAL data/app.db.\n"
            "Make sure bot.py and dashboard.py are stopped first (see docs/SAFETY.md), and that "
            "you've already dry-run this against copies. Ctrl-C now to abort.\n"
        )
        time.sleep(5)

    print(f"Migrating:\n  state: {args.state}\n  log:   {args.log}\n  db:    {args.db}\n")
    counts = migrate(args.state, args.log, args.db)
    print("Row counts:")
    for k, v in counts.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
