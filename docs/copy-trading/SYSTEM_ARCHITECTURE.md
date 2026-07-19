# System Architecture — Master Reference Guide

**Audience note:** zero prior context assumed. This document explains, in plain language, what
exists in this repo right now: what was built, how the pieces fit together, and exactly what
happens when the bot runs. It's meant to be the one place you can come back to and re-orient
yourself. For the risk controls themselves (what's enforced, exact numbers, why), see
`docs/copy-trading/RISK_MANAGEMENT.md` — this document explains the *system*, that one explains the *rules*.

---

## 1. What's been built so far

### The starting point

Before any of this work, the repo was a single working Python bot (`bot.py`) that already did
real copy-trading logic: it watched 20 hand-picked wallets, paper-traded their buys/sells, had
a trailing take-profit exit, a circuit breaker that mutes underperforming wallets, and a
spread/liquidity safety check. It stored everything in two files, `state.json` (current
positions) and `trades_log.json` (a running history of every event). It also had a small
built-in web dashboard (`dashboard.py`) that read those same JSON files.

That bot worked, but it's not what a much bigger spec (the "Hermes" build prompt) calls for:
a full research system that scans Polymarket's leaderboard, scores wallets, keeps a "decision
journal" explaining every choice, and improves its own rules over time — all shown on a proper
Next.js dashboard. A second, separate project (a weather-market arbitrage bot) is also planned
to eventually share the same dashboard and database.

### What today's work actually did

Rather than rewrite the working Python bot from scratch, we **kept it and gave it a new
foundation to grow from**:

1. **Pulled the "talk to bullpen" logic out of `bot.py`** into its own file, `bullpen_client.py`
   — no behavior change, just a cleaner separation, verified by restarting the live bot and
   confirming identical output.
2. **Designed a real database** (21 tables) covering both the copy-trading research system and
   the future weather bot, written as a Drizzle (TypeScript) schema.
3. **Built `db.py`** — a small Python module that lets `bot.py` read and write that database
   using plain SQL, instead of JSON files.
4. **Wrote a one-time migration script** (`migrate_to_sqlite.py`) that copies everything from
   the old `state.json`/`trades_log.json` into the new database, and ran it for real after
   dry-running it first against copies.
5. **Cut the live bot over** to the new database. `bot.py` and `dashboard.py` have been running
   on SQLite ever since, verified stable, with stats matching exactly what they were before the
   switch.
6. **Started the Next.js dashboard** (`apps/dashboard`) with a working Overview page that reads
   live from the same database.
7. **Built the first piece of the new "research brain"**: `scanLeaderboard.ts`, a script that
   pulls real wallet data from Polymarket via the `bullpen` CLI and saves it to the database.
   Already run successfully against live data.
8. **Wrote `docs/copy-trading/SAFETY.md`**, documenting the safety rules and boundaries between the pieces.
9. **Built `scoreWallets.ts`**, the second research-brain script: it reads the candidates
   `scanLeaderboard.ts` found, scores each one (ROI + consistency + a one-hit-wonder penalty +
   copyability), and writes a `track`/`watch`/`ignore` verdict onto `wallet_profile.status`.
   Fixed an address-casing bug along the way (some `bullpen` commands return checksummed
   mixed-case addresses, others lowercase — every DB writer now normalizes to lowercase before
   insert, so the same real wallet can't silently end up as two different rows).

Nothing about how the bot actually trades changed. What changed is *where it remembers things*,
and a foundation was added for it to eventually get smarter about *who* it copies.

---

## 2. The monorepo: `apps/` vs `packages/`

The repo is now what's called a **monorepo** — multiple related projects living in one repo,
managed together by a tool called **pnpm** (a package manager, like npm but faster and better
at this specific job). Two folders matter:

```
polymarket-copybot/
├── bot.py, config.py, dashboard.py, db.py, bullpen_client.py   ← Python side (unchanged location)
├── data/app.db                                                  ← the shared database file
├── apps/
│   └── dashboard/            ← the ONE deployable web app (Next.js)
└── packages/
    ├── db/                   ← the database's blueprint (schema) + a ready-to-use client
    ├── bullpen-client/       ← TypeScript twin of bullpen_client.py
    ├── copy-trading/         ← the "research brain" scripts (scanLeaderboard.ts today)
    ├── weather/               ← empty scaffold, for the weather bot (not built yet)
    └── shared/                ← empty scaffold, for things like Telegram alerts (not built yet)
```

**`apps/` = things you actually run and deploy.** Right now there's exactly one: the Next.js
dashboard. If we ever build the weather bot's own web view, it would likely live here too, or
share this same app.

**`packages/` = shared building blocks that other things depend on, but that aren't
"deployed" by themselves.** Think of `packages/db` as the single, agreed-upon blueprint for
what the database looks like — both the dashboard and the future scoring scripts import it so
they're always talking about the same tables in the same way. `packages/bullpen-client` is a
small reusable tool (talk to the `bullpen` CLI safely) that both `copy-trading` and the future
`weather` package will use, so that logic only has to be written once.

This split matters because of `workspace:*` — inside each `package.json` in `apps/dashboard` or
`packages/copy-trading`, you'll see dependencies like `"@copybot/db": "workspace:*"`. That tells
pnpm "don't download this from the internet, just link directly to the `packages/db` folder
right here in this repo." One `pnpm install` at the repo root installs everything for every
piece at once.

---

## 3. How Next.js (TypeScript) and the Python bot share one database

This is the part that makes the whole system work as *one* system instead of two separate ones.

### It's the same physical file

`data/app.db` is a single SQLite database file on your disk. There's no server, no network call,
no syncing between "the Python database" and "the TypeScript database" — there is only one
database, and both languages open that exact file directly. It's the same idea as two people
having a shared spreadsheet file open on the same computer: whoever saves last, the other person
sees those changes the next time they look.

- **Python** (`bot.py`, via `db.py`) opens it using Python's built-in `sqlite3` module and plain
  SQL statements (`SELECT`, `INSERT`, `UPDATE`).
- **TypeScript** (the Next.js dashboard, and scripts like `scanLeaderboard.ts`) opens it using a
  library called **Drizzle**, which lets TypeScript code look like `db.select().from(paperTrade)`
  instead of writing raw SQL by hand.

Both are just two different ways of talking to the same file.

### Who's allowed to change the *shape* of the database vs. just the *data* in it

There's an important rule: **only the TypeScript side (Drizzle) is allowed to create or change
tables** (add a column, rename something, etc.). Python's `db.py` is only ever allowed to read
and write *rows* — `SELECT`, `INSERT`, `UPDATE`, `DELETE` — never `CREATE TABLE` or
`ALTER TABLE`. This avoids the two languages ever disagreeing about what the database is
supposed to look like. When the database's shape needs to change, you run `pnpm db:migrate`
(TypeScript), and `db.py` just picks up the new columns automatically.

### Who's allowed to change *which fields*

Because both `bot.py` and the scoring scripts write to the same `wallet_profile` table,
there's a second, more subtle rule about *which columns* each side is allowed to touch, so they
never silently overwrite each other's work:

| Field | Owned by | What it means |
|---|---|---|
| `wallet_profile.status`/`statusReason`/`statusChangedAt` + scoring sub-columns (`track` / `watch` / `ignore`) | The TypeScript scoring layer (`scoreWallets.ts`) | Decides which wallets are worth copying |
| `wallet_profile.circuit_breaker_muted`, `mute_reason`, `muted_at`, `consecutive_losses`, `recent_results_json` | `bot.py` (via `db.py`) | The circuit-breaker logic that mutes a losing-streak wallet |
| `bot_risk_state`, `bot_market_event` (whole tables) | `bot.py` (via `risk_manager.py`/`db.py`) | The portfolio-level kill-switch latch and market→event cache — TS/Drizzle owns the table *shape*, but treats the row *contents* as read-only |

`bot.py` checks *both* `wallet_profile` fields before copying a trade (muted OR not-tracked
blocks a copy), but it only ever *writes* the mute-related ones. This is why `db.py`'s code has
comments like *"status is deliberately never written here."* Full detail on why this split
exists and how it's structurally enforced (not just convention): `docs/copy-trading/RISK_MANAGEMENT.md`
Rule 9.

### Making it safe for multiple writers at once

SQLite has a setting called **WAL mode** (Write-Ahead Logging) that was turned on once, right
after the database was first created. In plain terms: it lets many things *read* the database at
the same time, while still only letting one thing *write* at a time — and if two writes happen
to land at the exact same instant, the second one just waits up to 5 seconds
(`busy_timeout=5000`) instead of immediately failing. Because the bot's own writes are quick,
small, and the Next.js dashboard mostly only *reads* (its one "write" button — start/stop the
bot — doesn't touch the database at all, it just signals a process ID), collisions are rare in
practice.

### Concrete example

When `bot.py` copies a trade, it calls `db.py`'s `append_log()`, which does an `INSERT` into the
`bot_event_log` table (and a couple of related tables). The instant that write finishes, if you
had the Next.js dashboard's Overview page open and refreshed it, you'd see that new trade —
because it's reading from the exact same file, not a copy, not a cache.

---

## 4. The exact workflow — what happens right now, step by step

There are currently **three separate things** that can run, and it's worth being precise about
which ones are automatic/continuous versus manual/one-off today.

### A. The live trading loop — `bot.py` (runs continuously, always on)

This is the same logic that existed before today, just reading/writing SQLite instead of JSON:

1. **Every 30 seconds**, `bot.py` asks the `bullpen` CLI: *"any new trades from my 20 tracked
   wallets?"* (`bullpen tracker feed`).
2. For every new trade found:
   - If it's a **BUY**: check the wallet isn't muted, check no other tracked wallet already
     holds this exact market+outcome and that this trader hasn't hit their same-outcome buy cap
     (avoids doubling up — `docs/copy-trading/RISK_MANAGEMENT.md` Rule 3), then check the portfolio-level
     exposure ceiling / per-event cap / drawdown kill switch (`risk_manager.py`, Rule 6). In LIVE
     mode only, also check the spread/liquidity guard and the pre-trade slippage ceiling
     (Rules 4 and 11) before an order is ever submitted. Only after every check passes does it
     simulate (or place) buying $5 worth.
   - If it's a **SELL**: figure out what fraction of their position they sold, and sell that
     same fraction of our simulated position — sells are never blocked by any risk gate.
   - Every decision (copy, or skip-and-why) gets written to the database: one row in
     `bot_event_log` (the raw record) and, for buy/skip decisions specifically, one row in
     `decision_journal` too. Paper copies also log a signed execution-shortfall measurement
     (Rule 10) — visibility only, it never changes the paper fill or PnL.
3. **Every 5 minutes**, a separate sweep checks every open position's current price: if a
   position peaked at +50% profit or more and has since pulled back 10 percentage points from
   that peak, it's automatically sold (the "trailing take-profit"). This same sweep also
   refreshes portfolio equity for the drawdown kill switch.
4. **Every hour**, another sweep checks whether any held market has resolved (settled); if so,
   the position is closed out at the final price.
5. Positions live in the `paper_trade` table; the running list of "who's been muted for losing
   too much" lives in `wallet_profile`; the kill-switch latch and market→event cache live in
   `bot_risk_state`/`bot_market_event`.

### B. The local built-in dashboard — `dashboard.py` (runs continuously, port 8787)

A simple web page (no build step, plain HTML/JS) that reads the same database every time you
load or refresh it, and shows PnL, win rate, open positions, and the recent event log. It also
has the Start/Stop button for `bot.py`.

### C. The research scan — `scanLeaderboard.ts` + `scoreWallets.ts` (both manual, one-off commands)

Neither runs automatically or continuously yet — you type a command and it does one pass. They're
meant to be run in order: `scanLeaderboard.ts` finds *candidates*, `scoreWallets.ts` decides which
of those candidates are worth copying.

**`scanLeaderboard.ts`** (`pnpm scan:leaderboard`):

1. Asks `bullpen` for the global top-50 wallets by profit (`discover traders`).
2. Asks `bullpen` for the highest-volume events right now, looks at each one's biggest market,
   and pulls that market's top position-holders — then resolves each holder's display name to
   an actual wallet address.
3. Saves everything it found into the `leaderboard_scan` table, tagged with *where* each row
   came from (so you can tell "global top-50" data apart from "holders of the World Cup Winner
   market" data).

**`scoreWallets.ts`** (`pnpm scan:wallets`), added after the above:

1. Reads every candidate address out of `leaderboard_scan`.
2. **Pass 1 (cheap, ~9.5s/wallet):** pulls each wallet's summary stats (lifetime PnL, 30-day ROI,
   trade count) from `bullpen` and immediately marks the obviously-too-thin/too-weak ones
   `ignore` — they never get a pass-2 call.
3. **Pass 2 (expensive, ~15s/wallet, survivors only):** pulls each remaining wallet's full
   `pnl-series` history to measure consistency and penalize "one lucky spike" wallets.
4. Both passes run several wallets at once via a small concurrency pool
   (`packages/shared/src/concurrency.ts`) instead of one at a time.
5. Writes a final `track` / `watch` / `ignore` verdict (plus the score and reasoning) onto
   `wallet_profile.status` — but only that column and its scoring-related siblings; it never
   touches the five circuit-breaker columns `bot.py` owns on that same table (see §3 above).

**Important — this doesn't feed back into trading yet.** Right now, `bot.py` still only trades
the 20 wallets hardcoded in `config.py` (`TRACKED_TRADERS_SOURCE = "static"`). Both scripts are
just *collecting and scoring* data at this point — running `scoreWallets.ts` today updates the
database but changes nothing about what `bot.py` actually copies. Only once the scoring is
trusted would `config.py` get flipped to `TRACKED_TRADERS_SOURCE = "db"` so `bot.py` starts
trading off the scored list instead of the hardcoded one.

### D. The Next.js dashboard — `apps/dashboard` (not running by default; start with `pnpm dev`)

The Overview page is built and reads real data (PnL, open positions, tracked wallets, recent
activity) straight from the database via Drizzle. The other 8 pages the full spec calls for
(Wallet Rankings, Trade Signals, Decision Journal, Performance, Rules, Reports, etc.) aren't
built yet.

### Putting it together

```
 bot.py (every 30s)         scanLeaderboard.ts ──► scoreWallets.ts
   │  polls bullpen           (manual, one-off)     (manual, one-off,
   │  for new trades           polls bullpen for      run after scan)
   ▼                           wallet/holder data      │ writes wallet_profile.status
 db.py  ───────────►  data/app.db  ◄────────────────────┘  (Drizzle, TypeScript)
                    (ONE shared file)
                        ▲        ▲
                        │        │
             dashboard.py   apps/dashboard (Next.js, Overview page)
             (port 8787,       (pnpm dev, not auto-started)
              always reads
              live)
```

---

## 5. Quick file map

| File / folder | What it is |
|---|---|
| `bot.py` | The live trading loop. Unchanged logic, new storage backend. |
| `config.py` | All tuning constants — tracked wallets, risk thresholds, live/paper switch. |
| `bullpen_client.py` | Talks to the `bullpen` CLI (the only thing that ever touches Polymarket/keys). |
| `db.py` | Python's bridge to the shared SQLite database. |
| `risk_manager.py` | Pure-function portfolio risk controls (exposure ceiling, event cap, drawdown kill switch) — see `docs/copy-trading/RISK_MANAGEMENT.md` Rule 6. |
| `reset_kill_switch.py` | Standalone script to clear a latched kill-switch halt after human review. Deliberately not a dashboard button. |
| `dashboard.py` | The original small built-in web dashboard (port 8787). |
| `migrate_to_sqlite.py` | One-time script that moved old JSON data into SQLite. Already run. |
| `data/app.db` | The actual shared database file. Not tracked in git. |
| `packages/db/src/schema.ts` | The database's blueprint — every table and column, in one place. |
| `packages/bullpen-client/` | TypeScript version of `bullpen_client.py`, for the new scripts to use. |
| `packages/copy-trading/src/scanLeaderboard.ts` | Pulls wallet/leaderboard candidate data from Polymarket into `leaderboard_scan`. |
| `packages/copy-trading/src/scoreWallets.ts` | Scores those candidates and writes `track`/`watch`/`ignore` onto `wallet_profile.status`. |
| `packages/shared/src/concurrency.ts` | Small "run N at once" concurrency pool used by `scoreWallets.ts`'s two passes. |
| `apps/dashboard/` | The new Next.js web dashboard (Overview page built so far). |
| `docs/copy-trading/RISK_MANAGEMENT.md` | Every risk rule currently enforced — what, how, cost, why. The rules ledger. |
| `docs/copy-trading/SAFETY.md` | Database ownership boundaries, migration runbooks, and known residual risks. |
| `test_risk_manager.py`, `test_bot_risk_checks.py` | Unit tests for the portfolio risk controls and the slippage ceiling (pure-function, no DB/network). |

## 6. What's not built yet

So you know exactly where things stand: `monitorTrades.ts` / `scoreTrades.ts` (the broader trade
observer and decision engine described in the spec), `paperUpdatePnl.ts`, `reviewOutcomes.ts`,
`updateRules.ts` (the self-improving rules engine), `dailyReport.ts` (Telegram summaries), the
remaining 8 dashboard pages, and the entire weather arbitrage bot — `packages/weather/src/` is a
literal empty folder right now, not even a `package.json` yet, so it isn't wired into the pnpm
workspace at all. Every one of these already has `package.json` script entries and/or database
tables waiting for it (the weather tables alone: `weather_station`,
`weather_historical_observation`, `weather_forecast_snapshot`, `weather_market_mapping`,
`weather_probability_estimate`, `weather_position`, `weather_pnl_snapshot` — all defined in
`packages/db/src/schema.ts`, none read or written by any code yet).
