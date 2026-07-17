# Safety

## Paper trading only

`config.LIVE_MODE = False` — every trade `bot.py` places is simulated. `LIVE_MODE = True`
places real orders with real funds via `bullpen polymarket buy/sell --yes`. Per the Hermes
build-prompt spec this project follows: version one must not place real trades, must not ask
for or store private keys, must not sign transactions. All Polymarket/Polygon auth and
execution is delegated to the external `bullpen` CLI, which owns keys outside this codebase —
neither `bot.py` nor the TS research layer (`packages/copy-trading`, `packages/weather`) ever
touches a private key directly.

## Shared SQLite DB — ownership boundaries

`data/app.db` is written by multiple independent processes (`bot.py`, the future
`packages/copy-trading` operator loop, `apps/dashboard`'s one mutation route). To keep them
from silently fighting each other:

- **Schema DDL is TS/Drizzle-only.** `db.py` (and `migrate_to_sqlite.py`) only ever issue
  `SELECT`/`INSERT`/`UPDATE`/`DELETE` — never `CREATE`/`ALTER TABLE`. Always stop `bot.py` and
  any running operator loops before running `pnpm db:migrate` — SQLite's limited `ALTER TABLE`
  support means Drizzle Kit often does a rename-and-copy-table dance under the hood, which is
  unsafe to run concurrently with another process's open transaction on that table.
- **`wallet_profile.status`** (`track`/`watch`/`ignore`) is owned exclusively by the TS
  leaderboard-scan/scoring layer. `db.py` never writes it (see `db.py`'s `save_state`
  docstring) — `migrate_to_sqlite.py` sets it once, at first insert, as a one-time seed only.
- **`wallet_profile.circuit_breaker_muted`/`mute_reason`/`muted_at`/`consecutive_losses`/
  `recent_results_json`** are owned exclusively by `bot.py`'s circuit breaker (`db.py`). The TS
  scoring layer must treat these as read-only.
- Every writer sets `PRAGMA busy_timeout=5000` on its own connection; WAL mode is applied once
  at first migration. The Next.js dashboard server should stay near-read-only — its only
  mutation route (bot start/stop) signals a PID file, it doesn't touch `data/app.db`.

## Cutover runbook (JSON state → SQLite)

This repo already went through this once (`bot.py`/`dashboard.py` moved off
`state.json`/`trades_log.json` onto `data/app.db`); the same steps apply to any future re-run
or to a fresh clone catching up:

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

Known pitfall (hit and fixed during the first cutover): don't let demo/seed data
(`is_demo_data = 1` rows, inserted by `packages/db/src/seed.ts`) leak into `bot.py`'s live
position tracking — every query `db.py` runs against `paper_trade` for `bot.py`'s own state
must filter `is_demo_data = 0`, or the bot will try to manage a fake position that doesn't
exist on Polymarket.

## Portfolio-level risk controls (risk_manager.py, added 2026-07-18)

Three controls gate every new BUY (paper AND live — paper stays representative);
sells, trailing-TP exits, and closeouts are NEVER blocked — a risk layer that
traps you in positions adds risk instead of removing it:

1. **Total exposure ceiling** (`config.MAX_TOTAL_EXPOSURE_USD`): sum of open
   cost basis + the new trade may not exceed the ceiling.
2. **Per-event exposure cap** (`config.MAX_EVENT_EXPOSURE_USD`): same check
   scoped to one Polymarket event (market→event resolved via
   `bullpen polymarket market`, memoized in `bot_market_event`; resolution
   failure fails CLOSED — the buy is skipped, exposure never bypasses the cap
   by being unattributable). Only same-event concentration is caught;
   economically-correlated bets across different events are not.
3. **Drawdown kill switch**: equity = `PAPER_BANKROLL_USD` + realized +
   unrealized PnL, refreshed by the TTP sweep (~5 min reaction time, not
   per-trade). Two independent triggers latch one halt: an absolute equity
   floor (`EQUITY_FLOOR_USD`) and a max drawdown from the equity high-water
   mark (`MAX_DRAWDOWN_FROM_PEAK_USD`). The latch persists in
   `bot_risk_state` across restarts and is cleared only by
   `python3 reset_kill_switch.py` (then restart bot.py) — a breached limit
   means a human reviews before risk resumes. The reset is deliberately a
   standalone script, not a dashboard button: dashboard.py's documented
   boundary is that it never writes to `data/app.db`.

`bot_risk_state` and `bot_market_event` are bot.py-owned rows (TS/Drizzle
still owns the DDL, same rule as every other `bot_*` table); the TS side must
treat them as read-only. Decision logic lives in `risk_manager.py` as pure
functions with stdlib-unittest coverage (`python3 -m unittest
test_risk_manager`).

## Risks of copy trading itself

- **Stale/thin liquidity**: a copy can fill at a much worse price than the source trader's own
  fill if the book has moved or is too thin — `check_spread_tolerance` in `bot.py` rejects live
  copies above `config.SPREAD_TOLERANCE` relative spread, but paper-mode fills always use the
  source trade's reported price, which won't reflect this in paper PnL. Since 2026-07-18 this
  gap is at least *measured*: every paper copy also calls `bullpen polymarket preview` and logs
  the executable price + signed `shortfall_pct`/`shortfall_usd` into its bot_event_log payload
  (`measure_paper_shortfall` in bot.py — measurement only, deliberately never fed back into
  paper fills, so paper PnL stays optimistic but the implementation-shortfall cost of being
  last to every trade is now quantified before any live-mode decision).
- **Leaderboard wallets can be misleading**: high all-time PnL can come from one lucky trade,
  survivorship bias, or a wallet that's simply too illiquid to actually follow — this is why
  the wallet scorer (`packages/copy-trading`, in progress) penalizes one-hit-wonders and scores
  copyability separately from raw ROI, rather than just mirroring the raw leaderboard.
- **Circuit breaker is per-trader, not portfolio-wide**: `bot.py`'s circuit breaker
  (`config.MUTE_CONSECUTIVE_LOSS_STREAK`/`MUTE_WIN_RATE_THRESHOLD`) mutes individual
  underperforming traders; it does not cap total dollar exposure or drawdown across the whole
  book.

## Weather bot (planned): Wunderground scraping risk

The weather arbitrage bot's data pipeline (`packages/weather`, not yet built) is planned to use
Open-Meteo and NOAA/NWS (`api.weather.gov`) as primary, ToS-compliant, free data sources.
Wunderground scraping is included as a secondary source, by explicit product decision, **despite
Wunderground's ToS restricting scraping** — reserved only for stations/markets whose settlement
source is a Wunderground-only PWS not covered by the other two. When built, that ingester must
live in its own isolated file (`ingestWunderground.ts`), rate-limited and cache-first so a
historical backfill is a one-time pull rather than repeated hits, and easy to disable without
touching the other two ingesters. This is a knowingly-accepted compliance risk, not an
oversight — flagged here so it stays visible.

## Why private keys never belong in this app

`bullpen` owns 100% of on-chain interaction and credential storage outside this codebase.
Nothing in `bot.py`, `db.py`, `packages/bullpen-client`, or any future TS trading code should
ever be modified to accept, store, or log a private key or seed phrase. If a future feature
seems to need one, that's a signal the feature belongs in `bullpen` itself, not here.
