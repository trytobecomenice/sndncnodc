# Safety

**Audience note:** this document assumes zero prior context, same as `docs/RISK_MANAGEMENT.md`.
That document is the plain-numbered rules ledger — the single source of truth for "what rules
currently apply," each written as What it does / How it works mechanically / System costs &
trade-offs / Why it exists. **This document does not repeat that mechanical detail.** It covers
what `RISK_MANAGEMENT.md` deliberately leaves out: shared-database ownership boundaries,
migration/cutover procedures, and residual risks that no single rule fully closes. Where a topic
here is also a numbered rule there, this doc cross-references it by number instead of
duplicating it — the two must be updated together in the same commit when either changes, so
they never silently drift apart.

---

## 1. Paper trading only (technical implementation)

**What it does:** `config.LIVE_MODE = False` — see `docs/RISK_MANAGEMENT.md` Rule 1 for the full
mechanics.

**How it works mechanically:** `LIVE_MODE = True` places real orders with real funds via
`bullpen polymarket buy/sell --yes`. Per the Hermes build-prompt spec this project follows,
version one must not place real trades, must not ask for or store private keys, and must not
sign transactions. All Polymarket/Polygon auth and execution is delegated entirely to the
external `bullpen` CLI, which owns keys outside this codebase — neither `bot.py` nor the TS
research layer (`packages/copy-trading`, `packages/weather`) ever touches a private key
directly.

**System costs & trade-offs:** see Rule 1.

**Why it exists:** see Rule 1; see also "Why private keys never belong in this app" below.

---

## 2. Shared SQLite DB — ownership boundaries

**What it does:** `data/app.db` is written by multiple independent processes (`bot.py`, the
future `packages/copy-trading` operator loop, `apps/dashboard`'s one mutation route). This
section is the full ownership map that keeps them from silently fighting each other; see
`RISK_MANAGEMENT.md` Rules 8–9 for the plain-language summary.

**How it works mechanically:**
- **Schema DDL is TS/Drizzle-only.** `db.py` (and `migrate_to_sqlite.py`) only ever issue
  `SELECT`/`INSERT`/`UPDATE`/`DELETE` — never `CREATE`/`ALTER TABLE`.
- **`wallet_profile.status`** (`track`/`watch`/`ignore`) is owned exclusively by the TS
  leaderboard-scan/scoring layer (`scoreWallets.ts`'s `upsertWalletProfile`). `db.py` never
  writes it — `migrate_to_sqlite.py` sets it once, at first insert, as a one-time seed only.
- **`wallet_profile.circuit_breaker_muted`/`mute_reason`/`muted_at`/`consecutive_losses`/
  `recent_results_json`** are owned exclusively by `bot.py`'s circuit breaker, via `db.py`. The
  TS scoring layer must treat these as read-only — enforced structurally, not by convention: see
  Rule 9 for exactly how (`onConflictDoUpdate`'s explicit column list).
- **`bot_risk_state`** and **`bot_market_event`** (`risk_manager.py`'s persisted kill-switch
  latch and market→event cache — see Rule 6) are `bot.py`-owned rows. TS/Drizzle still owns the
  DDL (same rule as every other `bot_*` table), but the TS side must treat the row contents as
  read-only.
- Every writer sets `PRAGMA busy_timeout=5000` on its own connection; WAL mode is applied once
  at first migration. The Next.js dashboard server stays near-read-only — its only mutation
  route (bot start/stop) signals a PID file, it never touches `data/app.db` directly.

**System costs & trade-offs:** SQLite's limited `ALTER TABLE` support means Drizzle Kit often
does a rename-and-copy-table dance under the hood for a schema change — unsafe to run
concurrently with another process's open transaction on that table, which is why the cutover
runbook below always requires stopping `bot.py` (and any operator loops) first. `busy_timeout`
reduces, but does not eliminate, lock-contention failures under concurrent writes — a very hot
write path could still see a `database is locked` error surface to the caller.

**Why it exists:** with three independent processes able to touch one file, an unenforced
schema-ownership split would eventually let two processes fight over the same table's shape, or
one silently clobber a decision the other just made (the exact failure Rule 9 prevents for
wallet_profile specifically).

---

## Cutover runbook (JSON state → SQLite)

*This is a procedure, not a risk rule — included here because it's the concrete "how do I safely
change the database under these processes" playbook Rule 2 (above) describes in the abstract.*

This repo already went through this once (`bot.py`/`dashboard.py` moved off
`state.json`/`trades_log.json` onto `data/app.db`); the same steps apply to any future re-run or
to a fresh clone catching up:

1. Dry-run `migrate_to_sqlite.py` against **copies** of `state.json`/`trades_log.json` and a
   throwaway `--db` path first. Compare printed row counts against the source files' actual
   counts (see the script's own docstring for the exact commands).
2. Only once those match: stop `bot.py` and `dashboard.py` (the dashboard's stop button sends
   the same `SIGTERM` `bot.py`'s shutdown handler expects — it always finishes the trade in
   flight and persists state before exiting).
3. Run `python3 migrate_to_sqlite.py` for real (no `--state`/`--log`/`--db` overrides).
4. Restart both processes, watch a full poll cycle and at least one TTP sweep, and confirm the
   dashboard's stats match what they were pre-cutover.
5. `state.json`/`trades_log.json` are renamed to `*.pre-sqlite-backup` (gitignored, never
   deleted) — keep them for a rollback window before archiving elsewhere.

**Known pitfall** (hit and fixed during the first cutover): don't let demo/seed data
(`is_demo_data = 1` rows, inserted by `packages/db/src/seed.ts`) leak into `bot.py`'s live
position tracking — every query `db.py` runs against `paper_trade` for `bot.py`'s own state must
filter `is_demo_data = 0`, or the bot will try to manage a fake position that doesn't exist on
Polymarket.

---

## 3. Residual risks — what no single rule fully closes

*Every risk below has a partial mitigation somewhere in `RISK_MANAGEMENT.md`; none is fully
solved. This section exists so a gap doesn't get assumed away as "surely something already
covers that."*

**What it does / doesn't cover:**
- **Cross-event correlated exposure**: Rule 6's per-event cap only catches concentration WITHIN
  one Polymarket event. Two economically-correlated bets across DIFFERENT events (e.g. two
  related elections, or two markets that would resolve together on the same real-world outcome)
  are entirely invisible to any current control.
- **Leaderboard survivorship bias**: high all-time PnL can come from one lucky trade,
  survivorship bias, or a wallet too illiquid to actually follow. Rule 12's scorer penalizes
  one-hit-wonders and scores copyability separately from raw ROI specifically to counter this,
  but the underlying leaderboard data source itself still has no bias correction.
- **Latency-driven unfair mutes**: Rule 5's circuit breaker judges a trader purely on the bot's
  OWN copy performance, not the trader's raw on-chain performance. A trader who is genuinely
  profitable but whose edge the bot structurally can't capture in time (Rule 10's shortfall
  problem) can get muted for a latency issue that isn't really "their" fault.
- **Equity refresh lag**: Rule 6's drawdown kill switch only refreshes equity every ~5 minutes
  (tied to the TTP sweep interval) — a sharp intra-window drawdown that reverses before the next
  sweep is invisible to the kill switch entirely.
- **Whole-process freeze (e.g. machine sleep)**: on 2026-07-18 the Mac suspending overnight froze
  `bot.py` mid-subprocess-call, and it stayed silently wedged after wake. Defense-in-depth added
  2026-07-19: every bullpen subprocess call has a hard timeout
  (`config.BULLPEN_CALL_TIMEOUT_SECONDS`, 60s default), and the tracker-feed poll — the highest-
  frequency call and the one that froze — gets a tighter `config.FEED_POLL_TIMEOUT_SECONDS`
  (20s). Deliberately NOT tightened for buy/sell: a tight ceiling on a money-moving call
  manufactures `unknown_fill_state` outcomes (order submitted, response leg cut off), which is
  the worst failure mode available. A timeout cannot prevent an OS suspend itself — it only
  bounds how long a call can wedge the loop once the machine is awake again; the residual gap
  (nothing external restarts or alerts on a dead `bot.py`) remains open.

**How it works mechanically:** n/a — this section is a map of gaps, not a control.

**System costs & trade-offs:** the trade-off already made, across all four gaps above, is
**simplicity and reaction speed now vs. completeness later** — each of Rules 5/6/10/12 was sized
to catch the highest-value, lowest-complexity case first, with the acknowledged remainder logged
here rather than solved speculatively.

**Why it exists (as a section):** so a future reviewer — human or agent — checking "is X risk
handled?" gets an explicit, current answer instead of having to infer "no rule mentions it" from
silence, which is easy to misread as "solved" rather than "known and accepted."

---

## 4. Weather bot (planned): Wunderground scraping risk

**What it does:** the weather arbitrage bot's data pipeline (`packages/weather`, not yet built)
is planned to use Open-Meteo and NOAA/NWS (`api.weather.gov`) as primary, ToS-compliant, free
data sources, with Wunderground scraping as a secondary source.

**How it works mechanically:** when built, the Wunderground ingester (`ingestWunderground.ts`)
must live in its own isolated file, rate-limited and cache-first so a historical backfill is a
one-time pull rather than repeated hits, and easy to disable independently of the other two
ingesters.

**System costs & trade-offs:** Wunderground's ToS restricts scraping. This is used anyway, by
explicit product decision, reserved ONLY for stations/markets whose settlement source is a
Wunderground-only personal weather station not covered by either NOAA/NWS or Open-Meteo — i.e.
accepted narrowly, not as a general data-sourcing strategy.

**Why it exists:** flagged here explicitly so this is a knowingly-accepted compliance risk, not
an oversight discovered later. Isolating it to its own file/ingester is what keeps the risk
scoped and independently disable-able if the trade-off stops looking acceptable.

---

## 5. Why private keys never belong in this app

**What it does:** `bullpen` owns 100% of on-chain interaction and credential storage outside
this codebase.

**How it works mechanically:** nothing in `bot.py`, `db.py`, `packages/bullpen-client`, or any
future TS trading code accepts, stores, or logs a private key or seed phrase — there is no
parameter, config field, or code path for one.

**System costs & trade-offs:** any feature that seems to need direct key access (e.g. a
hypothetical future multi-wallet live-execution mode) cannot be built as a simple extension of
this codebase — it would have to be built into `bullpen` itself instead, which is a real
constraint on what's buildable here without that dependency changing first.

**Why it exists:** see Rule 1 — this is the structural enforcement of that same "no live funds,
no key handling" product requirement, stated here as an explicit invariant so it's checked
against for every future feature, not just at launch.
