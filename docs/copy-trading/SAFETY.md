# Safety

**Audience note:** this document assumes zero prior context, same as `docs/copy-trading/RISK_MANAGEMENT.md`.
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

**What it does:** `config.LIVE_MODE = False` — see `docs/copy-trading/RISK_MANAGEMENT.md` Rule 1 for the full
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
  bounds how long a call can wedge the loop once the machine is awake again. **Investigated
  properly 2026-07-21** (not assumed): checked `bot_event_log` timestamp gaps directly and found
  this is actually TWO distinct failure modes that look similar but aren't. (1) Genuine long
  silences (10-17 hour gaps, zero log activity of any kind) — consistent with the laptop actually
  being asleep; low-stakes, since the bot isn't running at all so it can't act on stale data, it
  simply misses trades that happen while off. (2) Continuous same-cadence (~28s) error loops
  lasting 1-2+ hours — NOT sleep (a suspended process can't execute anything, so sleep produces
  silence, not steady failed attempts); this was the bullpen auth session dying and never
  recovering, and it's what actually produced the worst entries in Rule 10's shortfall data. Mode
  (2) is now closed by Rule 13 (auth-failure isolation) — a dead session halts cleanly and recovers
  automatically instead of error-looping for hours. Mode (1) and the fully general "nothing
  external restarts a dead `bot.py` process at all" gap both remain open — see the cloud-hosting
  discussion (2026-07-21) for the planned fix to mode (1): moving off a personal laptop entirely.

**How it works mechanically:** n/a — this section is a map of gaps, not a control.

**System costs & trade-offs:** the trade-off already made, across all four gaps above, is
**simplicity and reaction speed now vs. completeness later** — each of Rules 5/6/10/12 was sized
to catch the highest-value, lowest-complexity case first, with the acknowledged remainder logged
here rather than solved speculatively.

**Why it exists (as a section):** so a future reviewer — human or agent — checking "is X risk
handled?" gets an explicit, current answer instead of having to infer "no rule mentions it" from
silence, which is easy to misread as "solved" rather than "known and accepted."

---

## 4. Auth-failure isolation (Rule 13 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 13 — see
that rule for the incident and the full reasoning; this section is file/function-level detail.

**How it works mechanically:**
- `bullpen_client.py`: `BullpenAuthError(RuntimeError)` — raised by `_run_bullpen_json_once()`
  when the subprocess exits with code 2 (`bullpen`'s own documented "Authentication failure"
  code, confirmed via `bullpen --help`). A plain `RuntimeError` is still raised for every other
  non-zero exit code, unchanged. `BullpenAuthError` is a `RuntimeError` subclass, so the existing
  retry loop in `run_bullpen_json()` (bare `except RuntimeError`) still retries it up to the
  caller's configured `retries` before it ever reaches `bot.py` — only `bot.py`'s explicit
  `except BullpenAuthError` branch treats it specially.
- `bot.py`: `fetch_feed_with_auth_recovery()` (called from `main()`'s poll loop in place of a
  direct `run_bullpen_json(["tracker", "feed", ...])` call). On `BullpenAuthError`: logs
  `event_type: "auth_halted"` once, `print()`s to console, calls `set_risk_value("auth_halt",
  {...})` (same `bot_risk_state` table Rule 6's kill switch uses), then loops on
  `config.AUTH_RECHECK_INTERVAL_SECONDS` (120s) re-attempting only the feed call. A module-level
  `_auth_halt_rechecks` counter throttles repeat log lines to every 30th recheck (~hourly),
  mirroring `_closeout_fetch_failures`'s existing throttle pattern exactly. On success: logs
  `event_type: "auth_recovered"`, `clear_risk_value("auth_halt")`, returns the fresh feed to the
  normal loop body — no process restart involved, no code path duplicated (the normal
  trade-processing logic downstream is completely unchanged, it just now gets its `feed` from
  this wrapper instead of the raw call).
- `db.py`: `bot_risk_state` gained a new key, `"auth_halt"` (dict when set, absent when not) —
  same table, same `get_risk_value`/`set_risk_value`/`clear_risk_value` functions Rule 6 already
  uses, no schema change.
- Tests: `test_bullpen_client.py` (new file, 4 tests) — verifies exit code 2 specifically (and
  only exit code 2) raises `BullpenAuthError`, mocking `subprocess.run` rather than calling the
  real CLI.

**System costs & trade-offs:** see Rule 13's own costs section — the short version is: no
alerting channel beyond console/log/persisted-flag exists yet, and `dashboard.py` doesn't
currently surface `bot_risk_state` at all (this flag or the pre-existing kill switch), so noticing
a halt today still means looking directly at one of those three places.

**Why it exists:** see Rule 13.

---

## 5. Weather bot (planned): Wunderground scraping risk

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

## 6. Why private keys never belong in this app

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

---

## 7. Direct Polymarket tracking feed (Rule 14 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 14 — see
that rule for the full incident/reasoning; this section is file/function-level detail. **LIVE as
of 2026-07-22** — wired into `bot.py`'s poll loop, `bullpen tracker feed` no longer called there.

**How it works mechanically:**
- `polymarket_data_api.py` (new file): `fetch_wallet_trades(wallet_address, limit, timeout)` — one
  wallet, over a thread-local persistent `http.client.HTTPSConnection` (see `_get_connection`/
  `_reset_connection`, keyed via `threading.local()` — `http.client.HTTPSConnection` is not
  thread-safe to share across threads, so this must stay per-thread, never a single module-level
  connection). `normalize_activity_record(record, wallet_address)` — the field-mapping adapter;
  see its docstring for the exact field-by-field mapping, confirmed against a live side-by-side
  `bullpen tracker feed` call, not guessed. `fetch_all_wallets_concurrent(wallet_addresses, ...,
  executor=None)` — fans out via a `ThreadPoolExecutor`; accepts an externally-provided, long-
  lived `executor` (see `make_persistent_executor()`) so the caller controls whether worker
  threads (and their persistent connections) survive across repeated calls or not. A single
  wallet's fetch failing is caught per-wallet and recorded in the returned `errors` list — never
  aborts the batch, mirroring `bot.py`'s own `run_closeout_sweep` philosophy.
- `REQUEST_HEADERS` sets a plainly self-identifying `User-Agent`
  (`polymarket-copybot/1.0 (+personal research bot)`) — required: the API 403s on Python's
  default `urllib`/`http.client` User-Agent string specifically, confirmed live by testing both
  back to back against the identical URL. This is a truthful header, not browser impersonation —
  same line already held for Wunderground (`docs/weather/WEATHER_RISK_MANAGEMENT.md` Rule 5).
- `validate_direct_feed.py` (new file): joins `bullpen tracker feed` output against
  `fetch_all_wallets_concurrent()` output by `transaction_hash` (confirmed live to be the literal
  same value in both sources — an exact key, not a fuzzy heuristic). Read-only: never calls any
  `bot.py` state-mutating function, never touches `seen_trade_ids`/`positions`/`bot_event_log`.
- Tests: `test_polymarket_data_api.py` (13 tests) — field-mapping correctness, the
  multi-fill-per-transaction `trade_id` collision case, per-wallet error isolation, the
  connection-reuse/reset logic, and the `start` time-filter param (mocking
  `http.client.HTTPSConnection`, no real network calls).
- **`bot.py` wiring (2026-07-22 cutover)**: `main()` creates one `make_persistent_executor()`
  ONCE (`direct_feed_executor`, sized `max(len(wallet_addresses), 10)`), passed into every
  `fetch_direct_feed(executor, wallet_addresses)` call — both the bootstrap block and the main
  poll loop now call this instead of `run_bullpen_json(["tracker","feed",...])`.
  `fetch_direct_feed()` wraps `fetch_all_wallets_concurrent()`, logs any per-wallet errors as a
  normal `error` event (never raises), and returns the identical `{"trades": [...]}` shape the old
  bullpen response had — `process_trade()` and every other downstream line needed zero changes.
  `fetch_feed_with_auth_recovery()` (Rule 13) is no longer called from the main loop but stays in
  the file, still tested, for bullpen's other remaining call sites (execution, previews, closeout).
- **Trade-ID migration, executed as a deliberate one-time step, not automated**: bullpen's
  `trade_id` (a UUID) and the direct API's (a composite `tx_hash:asset:side:timestamp` string) are
  incompatible formats for the same trade. Cutover procedure: stop `bot.py` (graceful SIGTERM) →
  `DELETE FROM bot_seen_trade` (clears ~1,233 old-format IDs) → restart. Reuses the existing,
  already-tested `bootstrap = not state["seen_trade_ids"]` logic rather than adding new format-
  detection code to `bot.py` — the fresh bootstrap re-seeds `bot_seen_trade` using the new feed
  source's IDs. Live-verified: 400 trades baseline-skipped on restart, zero errors.
- `test_bot_risk_checks.py` gained `TestComputeShortfall`, `TestMeasurePaperShortfall` (the fee-
  capture fix), and `TestFetchDirectFeed` (shape compatibility + per-wallet error isolation).
- **Rate-limit backoff + pagination (2026-07-22)**: `polymarket_data_api.py`'s single
  `fetch_wallet_trades()` split into three layers — `_make_request()` (one HTTP call, one
  connection-level retry on a stale keep-alive connection, unchanged from before), `_fetch_one_page()`
  (retries `RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}` with exponential backoff —
  `RATE_LIMIT_BACKOFF_BASE_SECONDS = 1.0`, doubling, up to `RATE_LIMIT_MAX_RETRIES = 4`; any other
  non-200 raises immediately, not retried), and `fetch_wallet_trades()` itself (loops over
  `offset`-based pages, up to `MAX_PAGES_PER_FETCH = 10`, stopping the first time a page returns
  fewer than `limit` records). `test_polymarket_data_api.py` gained `TestPagination` (3 tests) and
  `TestRateLimitBackoff` (4 tests), all mocking `http.client.HTTPSConnection` — no real network
  calls in the test suite itself.

**System costs & trade-offs:** see Rule 14's own costs section. One implementation note: this
Python installation's SSL certificate bundle needed the standard python.org
`Install Certificates.command` fix before any of this would work at all (`urllib`/`http.client`
couldn't verify `data-api.polymarket.com`'s certificate chain) — a one-time, machine-local fix,
not something this code depends on going forward, but worth knowing if this is ever run on a
different machine (e.g. the eventual cloud server) for the first time.

**Why it exists:** see Rule 14.

---

## 8. Direct order-book simulator (Rule 10 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 10's
2026-07-22 cutover entry — see that rule for the full reasoning; this section is file/function-
level detail. **LIVE as of 2026-07-22** — `measure_paper_shortfall()` calls
`polymarket_simulator.simulate_fill()` instead of `bullpen polymarket preview`. Paper trading no
longer depends on bullpen at all. This does NOT touch execution — see §6 above; bullpen still owns
100% of signing for both paper's live-mode spread checks and any real order.

**How it works mechanically:**
- `polymarket_simulator.py` (new file): `fetch_market_info(market_slug)` calls
  `gamma-api.polymarket.com/markets?slug=...`, parses `outcomes`/`clobTokenIds` (same-order JSON
  arrays — outcome name maps 1:1 to a CLOB token ID) and the market's real fee rate
  (`feeSchedule.rate` if `feesEnabled`, else `0.0` — verified live across 5 markets that this
  varies, not a fixed category constant). `fetch_order_book(token_id)` calls
  `clob.polymarket.com/book?token_id=...` and **explicitly re-sorts both sides itself** (bids
  descending, asks ascending, best price at index 0) rather than trusting the API's own ordering —
  live testing found bids come back ascending and asks descending, an inconsistency that isn't
  documented either way.
- `_walk_asks_for_usd(asks, usd_budget, fee_rate)` / `_walk_bids_for_shares(bids, shares, fee_rate)`
  — the book-walkers. Consume price levels best-first until the requested size is filled or the
  book runs out; apply `fee = shares × feeRate × price × (1-price)` PER LEVEL and sum (not once
  against the average price — the formula is nonlinear in price, so this matters for orders
  spanning multiple levels). Return `exhausted=True` alongside a partial fill if the visible book
  couldn't cover the full requested size.
- `simulate_fill(market_slug, outcome, side, amount)` — the entry point `measure_paper_shortfall()`
  calls; same input convention as the old bullpen call (`amount` = USD for BUY, shares for SELL)
  and same output keys (`price`, `spread`, `trading_fee`, `network_fee`) so the call site is a
  one-line swap. Returns `{}` (no `price` key) when a side of the book is empty — the caller's
  existing `not preview.get("price")` check already treats that as `no_executable_price`, so no
  new branch was needed there. Raises on a genuine fetch/lookup failure (bad slug, network error,
  unmatched outcome name), which the existing `try/except` in `measure_paper_shortfall()` already
  converts to `preview_unavailable` — same fail-soft contract as before, just against a different
  upstream. `network_fee` is always `0.0`: this module never broadcasts a transaction, so there is
  no real gas cost to simulate (a genuinely different, no-longer-applicable quantity from bullpen's
  gas-estimate figure, not a placeholder).
- Connection/retry pattern (thread-local per-host `HTTPSConnection`, 429/5xx exponential backoff,
  other 4xx never retried) intentionally duplicates `polymarket_data_api.py`'s Rule 14 pattern
  rather than sharing code with it — that module is the LIVE tracking feed; refactoring it to share
  code here would risk it for a nice-to-have this task didn't ask for.
- `measure_paper_shortfall()` gained one new field: `insufficient_liquidity` (+ `shares_filled`),
  surfaced whenever the book couldn't fill the requested size in full — the price/fee reported are
  still real for the partial amount, just flagged rather than silently presented as a full fill.
- Tests: `test_polymarket_simulator.py` (17 tests) — market-info parsing (including the
  fees-disabled and mismatched-array-length cases), outcome→token-ID matching, book re-sorting,
  both book-walkers' multi-level and insufficient-depth math, and 4 end-to-end `simulate_fill()`
  cases (BUY, SELL, empty book, insufficient liquidity). `test_bot_risk_checks.py`'s
  `TestMeasurePaperShortfall` gained 2 more cases (empty-book handling, `insufficient_liquidity`
  surfacing) and its existing 3 were repointed from mocking `bot.run_bullpen_json` to mocking
  `bot.polymarket_simulator.simulate_fill`. All 78 tests across the suite pass.
- Live-verified against a real market (`new-rhianna-album-before-gta-vi-926`): a $50 BUY simulated
  to price 0.51, fee $1.225 (`50/0.51 shares × 0.05 × 0.51 × 0.49`, matching the formula exactly);
  a second call (SELL, walking the bid side, warm connection) completed in ~0.7s vs. ~1.8s cold.
  `bot.py` was restarted to deploy this; the live process picked it up cleanly with zero errors.

**System costs & trade-offs:** two HTTP calls per shortfall measurement (market info + order book)
instead of one bullpen subprocess call — cold ~2.7s, warm ~0.7s (persistent connections, same as
Rule 14). This is pure per-measurement overhead, same as before; still fails soft, so a slow or
failed simulation never blocks or delays the actual paper copy.

**Extended 2026-07-22 to Trailing Take-Profit pricing (Rule 7)**: `get_market_prices()` migrated
off `bullpen polymarket price` onto a new shared helper,
`polymarket_simulator.fetch_order_book_for_outcome(market_slug, outcome)` (resolves the outcome to
a token ID via `fetch_market_info`, then calls `fetch_order_book` — the exact composition
`simulate_fill()` already did, now factored out since two call sites needed it). `fetch_order_book`
also gained a `last_trade_price` field, read from the same `/book` response (confirmed live: it's
a real top-level field on that endpoint, not a second call) — needed since TTP's indicative-price
fallback chain (`best_bid` -> midpoint -> `last_trade`) already relied on a last-trade figure.
`token_id_for_outcome` was made a public function (dropped its leading underscore) since it's now
a legitimate cross-call-site dependency, not a private implementation detail of one function.
5 new tests in `test_bot_risk_checks.py` (`TestGetMarketPrices`) and 3 more in
`test_polymarket_simulator.py` cover the fallback chain, including the degenerate-zero-bid edge
case. Live-verified: a real call returned `best_bid=0.5, indicative=0.5, err=None` against the
same test market. All 87 tests across the suite pass. `get_market_prices()` was the last read-only
bullpen call site outside execution/closeout — bullpen is now used **only** for actual signing
(buy/sell) and the closeout sweep, exactly matching §6's boundary.

**Why it exists:** see Rule 10's 2026-07-22 cutover entry, and §6 above — this is the "safe half"
of phasing bullpen out (read-only market data, no signing), deliberately kept separate from
execution, which stays on bullpen per §6's non-negotiable boundary.

---

## 9. Confidence-weighted position sizing (Rule 15 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 15 — see
that rule for the full reasoning; this section is file/function-level detail. **LIVE as of
2026-07-22.**

**How it works mechanically:**
- `config.py`: `FIXED_TRADE_USD = 5.0` replaced with `BASE_TRADE_USD = 5.0`, `MIN_TRADE_USD = 3.0`,
  `MAX_TRADE_USD = 10.0`.
- `bot.compute_trade_size_usd(composite_score)` (new, pure function): `None` -> `BASE_TRADE_USD`;
  otherwise linearly interpolated between `MIN_TRADE_USD` (score 0) and `MAX_TRADE_USD` (score 1),
  clamping the input to `[0, 1]` first as a defensive measure against a future scoring-formula
  change producing an out-of-range value.
- `db.get_wallet_composite_scores()` (new): `SELECT wallet_address, composite_score FROM
  wallet_profile`, returned as `{address_lower: score_or_None}`. Deliberately independent of
  `config.TRACKED_TRADERS_SOURCE` — sizing and tracker-membership are two different gates that
  happen to read the same table.
- `bot.py`'s `main()` calls this once at startup (`wallet_scores = get_wallet_composite_scores()`),
  same restart-to-pick-up-changes design as `get_tracked_traders()`. `process_trade()` gained a
  `wallet_scores` parameter and computes `trade_usd = compute_trade_size_usd(wallet_scores.get(trader.lower()))`
  as its first step; every one of the 9 internal `config.FIXED_TRADE_USD` references (share-count
  math, the risk-manager call, the spread/slippage checks, the live buy call, the paper-shortfall
  measurement, cost-basis accounting, and the logged event) now reads this one local variable
  instead, so every downstream consumer of trade size is consistent by construction rather than
  needing individually reconciled.
- `dashboard.py`'s status JSON field renamed from `fixed_trade_usd` (a single number) to
  `trade_size_usd: {min, max, base_if_unscored}`, matching the new range-based reality rather than
  silently reporting a now-fictional flat number.
- Tests: 6 new cases in `test_bot_risk_checks.py` (`TestComputeTradeSizeUsd`) — unscored-wallet
  fallback, floor/ceiling exactness, linear interpolation, and clamping both directions. No new
  test for `get_wallet_composite_scores()` itself (a thin SQL wrapper) — same precedent as
  `get_tracked_traders()`, which also has no dedicated unit test; verified live instead (see Rule
  15's live-verification note). All 93 tests across the suite pass.

**System costs & trade-offs:** every portfolio-level gate that previously received
`config.FIXED_TRADE_USD` as a parameter (`risk_manager.check_buy()`, `check_spread_tolerance()`,
`measure_paper_shortfall()`) already took it as an argument rather than reading the constant
internally, so passing a variable `trade_usd` through instead required zero changes to those
functions — the risk-gate boundary was already correctly parameterized before this change, it just
hadn't been given anything but a constant to work with yet.

**Why it exists:** see Rule 15.

---

## 10. Order-book staleness check (Rule 16 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 16 — see
that rule for the full reasoning; this section is file/function-level detail. **LIVE as of
2026-07-22.**

**How it works mechanically:**
- `polymarket_simulator.py`: new `MAX_BOOK_AGE_SECONDS = 15` constant.
- `fetch_order_book()`: reads `data.get("timestamp")` (ms since epoch, a real field on the /book
  response — confirmed live, previously fetched for nothing) and computes
  `age_seconds = time.time() - (timestamp_ms / 1000.0)`. Raises `RuntimeError` if
  `age_seconds > MAX_BOOK_AGE_SECONDS`; leaves the book untouched (no check at all) if `timestamp`
  is absent, a deliberate lenient default since every real response observed included it.
- No changes needed in `simulate_fill()`, `get_market_prices()`, `measure_paper_shortfall()`, or
  `fetch_order_book_for_outcome()` — the raise propagates up to each caller's existing
  try/except, converting to `shortfall_status: "preview_unavailable"` or a `"price check failed:
  ..."` string exactly as any other `fetch_order_book()` failure already did. Verified directly
  (not just asserted) by mocking a stale-book `RuntimeError` at the `polymarket_simulator` call
  site and confirming both `bot.measure_paper_shortfall()` and `bot.get_market_prices()` degrade
  the same way they already did for any other fetch failure.
- Tests: `TestOrderBookStaleness` (4 new: fresh accepted, stale rejected, right-at-the-boundary
  accepted, missing-timestamp lenient). Every existing HTTP-mocked book fixture across
  `test_polymarket_simulator.py` (`TestFetchOrderBook`, `TestFetchOrderBookForOutcome`,
  `TestSimulateFill` — 8 fixtures total) was updated via a new `_book_body()` test helper that
  injects a realistic, fresh `timestamp` — otherwise those tests would have been silently
  bypassing the very check this change adds. 97 tests pass across the suite.
- Live-verified: a real fetch against the same test market succeeded cleanly (34 bids, 16 asks,
  `last_trade_price=0.500`), confirming the check doesn't false-positive against a genuinely fresh
  response.

**System costs & trade-offs:** one float subtraction and comparison per book fetch — negligible.
`MAX_BOOK_AGE_SECONDS=15` is an explicit judgment call, not a verified-safe number (documented as
such in the constant's own comment) — worth revisiting if it ever produces a false-positive reject
in production logs.

**Why it exists:** see Rule 16.

---

## 11. Disk-exhaustion hardening (Rule 17 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 17 — see
that rule for the full reasoning; this section is file/function-level detail. **LIVE as of
2026-07-22.**

**How it works mechanically:**
- `config.py`: `BOT_LOG_PATH`, `DASHBOARD_LOG_PATH`, `LOG_MAX_BYTES` (50MB), `LOG_BACKUP_COUNT` (5),
  `EVENT_LOG_RETENTION_DAYS` (180), `PRUNE_INTERVAL_SECONDS` (86400) — all new.
- `bot.py`: module-level `logger = logging.getLogger("copybot")` with a `RotatingFileHandler` on
  `config.BOT_LOG_PATH` plus a `StreamHandler`. Every one of its 11 `print()` calls replaced with
  the matching `logger` level — `info` for routine status, `warning` for a muted trader / latched
  kill switch, `error` for a dead bullpen session, `critical` for the kill switch actually firing.
- `dashboard.py`: same pattern, its own `logging.getLogger("dashboard")`, `config.DASHBOARD_LOG_PATH`.
  `BOT_LOG_PATH` now imported from `config` rather than redefined locally (was drifting risk,
  now one source of truth). `start_bot()`'s `subprocess.Popen` no longer redirects the child's
  stdout to a file handle it opens itself — redirects to `DEVNULL` instead, since `bot.py`'s own
  logger is now the sole writer of `bot.out.log` (see Rule 17 for the rotation-blindness bug this
  fixes).
- `db.py`: `logging.getLogger("copybot.db")`, **no handlers attached here** — deliberately relies
  on propagation to the "copybot" logger's handlers (set up by `bot.py`) rather than a second
  independent `RotatingFileHandler` on the same file. `append_log()`'s `print()` (the highest-
  volume one in the whole codebase — fires on every event) now goes through this logger.
- `db.prune_event_log(retention_days=None)`: `DELETE FROM bot_event_log WHERE timestamp < cutoff`,
  defaulting to `config.EVENT_LOG_RETENTION_DAYS`. Returns the row count deleted. Called from
  `bot.py`'s main loop as a new interval-gated sweep, same shape as the existing TTP/closeout
  sweeps (own try/except, own `persist()`, logs an `event_log_pruned` event when it deletes
  anything).
- Tests: `test_db_prune.py` (3 new, isolated — a temporary SQLite file, never the real
  `data/app.db`, given this function has real destructive potential unlike the read-only `db.py`
  functions this suite otherwise doesn't unit-test). 100 tests pass across the whole suite.
- A rotation smoke test (a throwaway script, tiny `maxBytes`, not part of the committed suite)
  confirmed the mechanics directly: `bot.out.log`, `.log.1`, `.log.2`, `.log.3` all created and
  capped at the configured size, oldest content correctly aged out.
- Deployed via `dashboard.py`'s own `/api/toggle` endpoint (not a manual `nohup`) specifically to
  keep the pidfile authoritative and avoid repeating the duplicate-`bot.py`-process incident from
  earlier the same day.

**System costs & trade-offs:** `bot.out.log`/`dashboard.out.log` are now hard-capped at 300MB each
(50MB × 6 copies: current + 5 backups) — a real, bounded ceiling replacing unbounded growth.
`bot_event_log`'s 180-day retention is a judgment call; `prune_event_log()`'s real DELETE potential
is the reason it got dedicated test coverage other `db.py` functions don't have.

**Why it exists:** see Rule 17.

---

## 12. Category-specific wallet scoring (Rule 18 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 18 — see
that rule for the full reasoning; this section is file/function-level detail. **LIVE as of
2026-07-23.**

**How it works mechanically:**
- `packages/db/src/schema.ts`: two new nullable columns, both additive Drizzle migrations —
  `botMarketEvent.category` and `walletProfile.categoryScoresJson`.
- `packages/copy-trading/src/polymarketDataApi.ts` (new): TS twin of `polymarket_data_api.py`.
  `fetchWalletTrades(address, {limit, startEpochSeconds})` — same pagination/backoff shape
  (429/5xx retried with exponential backoff, other 4xx not retried). **`MAX_PAGES=10` is load-
  bearing, not arbitrary**: `MAX_PAGES × DEFAULT_LIMIT` (500) must stay at or under Polymarket's
  documented 5000-total-offset ceiling — an earlier `MAX_PAGES=20` draft was caught live during
  testing with an actual `HTTP 400 "max historical activity offset of 5000 exceeded"`, not found
  by reading docs alone.
- `packages/copy-trading/src/polymarketCategories.ts` (new): TS twin of
  `polymarket_simulator.py`'s `resolve_market_category()` — `fetchMarketEventSlug()` (Gamma
  `/markets?slug=`) then `resolveCategoryForEvent()` (Gamma `/events?slug=`, tags bucketed against
  `CATEGORY_TAG_SLUGS`). This TS constant must be **kept in sync by hand** with `config.py`'s
  `CATEGORY_TAG_SLUGS` — there is no shared config module across the two languages.
- `packages/copy-trading/src/scoreWalletCategories.ts` (new): `reconstructRealizedCloses()` walks
  a wallet's raw trades chronologically, tracking `{shares, costBasisUsd}` per (market, outcome)
  with the same arithmetic `bot.py`'s `process_trade()` uses; `aggregateCategoryScores()` groups
  realized closes by category, requiring `DEFAULT_RULES.hardMinTrades` (5) closes before writing a
  score, computed as `0.5 * computeRoiScore(roi) + 0.5 * computeWinRateScore(winRate)` (both reused
  directly from `scoreWallets.ts`, not reimplemented). Wallet selection
  (`getWalletsToScore()`) accepts explicit `0x...` addresses via argv, falling back to
  `status IN ('track','watch')` only when none are given — **deliberately not
  `status='track'` alone**: that column is this scorer's own forward-looking recommendation,
  disconnected from what `bot.py` actually trades whenever `TRACKED_TRADERS_SOURCE="static"` (the
  current real setting — confirmed live: only 2 of the 20 statically-configured wallets happened
  to carry `status='track'`, most were `'ignore'`).
- `db.py`: `load_market_categories()`/`save_market_category()` (new, separate functions from the
  existing `load_market_events()`/`save_market_event()` — same table, deliberately different
  functions so `risk_manager`'s existing `{market_slug: event_slug}` shape never has to change
  just because a column was added). `get_wallet_composite_scores()`'s return shape changed from a
  bare float to `{composite, categories: {category: score}}` — its one call site
  (`compute_trade_size_usd()`) updated in the same change.
- `bot.py`: `compute_trade_size_usd(wallet_score_entry, category)` — two-tier fallback (category
  score → global composite → `BASE_TRADE_USD`). `process_trade()` resolves category via
  `polymarket_simulator.resolve_market_category()`, reusing the `event_slug` it already resolves
  for `risk_manager`'s exposure cap (no second lookup) — cached in a new
  `risk_state["market_to_category"]` dict, loaded at startup from `load_market_categories()`.
- Tests: 14 new in `test_bot_risk_checks.py` (fallback logic), 7 new in `test_db_categories.py`
  (isolated temp DB, not the real `data/app.db`), 6 new in `test_polymarket_simulator.py`
  (`resolve_market_category`), plus 8 (`polymarketDataApi.test.ts`), 11
  (`polymarketCategories.test.ts`), and 14 (`scoreWalletCategories.test.ts`) on the TS side — 121
  Python tests and 79 TS tests passing.
- Live-verified end to end: `resolve_market_category()` correctly bucketed real markets to
  `"crypto"` and `"politics"`; a real wallet's reconstructed history produced 1646 genuine
  realized closes across two categories with sensible win-rates (70.5%/96.9%) and scores (0.76/0.55)
  — checked by hand before trusting the pipeline, per the plan's own verification step. A second,
  extremely high-frequency wallet correctly produced zero realized closes (5000 records spanned
  only ~3 days for that wallet — mostly still-open, unresolved positions, correctly excluded, not
  a bug).

**System costs & trade-offs:** category scores are a leading indicator (the wallet's own
reconstructed edge, not our limited copy experience) but come with a real, honestly-documented
gap: an extremely high-frequency wallet may never accumulate enough *resolved* closes within
`MAX_PAGES`'s reachable depth to clear the minimum-sample gate, and will simply keep using its
global score — the visible cost of never guessing at an unresolved position's value.

**Why it exists:** see Rule 18.

**Holding-rewards field (added 2026-07-23, point 3.1) — technical detail:**
- `packages/db/src/schema.ts`: `botMarketEvent.holdingRewardsEnabled` (new nullable boolean
  column, Drizzle migration `0009_nostalgic_amazoness.sql`, applied to the real `data/app.db` —
  `ALTER TABLE bot_market_event ADD holding_rewards_enabled integer`).
- `bot.py`'s `resolve_market_event()`: return type changed from a bare `event_slug` string to
  `(event_slug, holding_rewards_enabled)` — both read off the SAME `bullpen polymarket market`
  response (verified live 2026-07-23: the response's real top-level `holdingRewardsEnabled` field,
  confirmed `False` on a real market), zero extra API calls. Non-boolean/missing values coerce to
  `None` rather than being trusted blindly (this endpoint's other fields have arrived
  JSON-string-encoded before). Failure path returns `(None, None)` — the caller still fails CLOSED
  on the event slug (skips the buy); the lost holding-rewards reading on that same failure is
  inconsequential since the buy is skipped anyway.
- `db.py`'s `save_market_event(market_slug, event_slug, holding_rewards_enabled=None)` — the new
  parameter defaults to `None` so it's backward compatible with any caller that only cares about
  `event_slug`; the upsert's `ON CONFLICT` clause now also updates `holding_rewards_enabled`.
- **This column feeds NOTHING** — not `compute_trade_size_usd()`, not any scoring formula. It
  exists solely so the Category Score's immunity-to-holding-reward-contamination claim (see Rule
  18's addendum in RISK_MANAGEMENT.md) is independently auditable per-market, not asserted only in
  prose.
- Tests: 4 new in `test_db_categories.py` (`save_market_event` round-trip: `True`/`False`/omitted-
  defaults-to-`None`/upsert-updates-on-conflict), 6 new in `test_bot_risk_checks.py`
  (`resolve_market_event`'s extraction, non-boolean coercion, and both failure paths). 140 Python
  tests passing total (was 130).

---

## 13. Hard skip on statistically significant category harm (Rule 19 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 19 — see
that rule for the full reasoning; this section is file/function-level detail. **LIVE as of
2026-07-23.**

**How it works mechanically:**
- `scoreWalletCategories.ts`: `aggregateCategoryScores()` computes `pnl_t_stat` per category — a
  one-sample t-statistic (`mean / (sampleStdDev / sqrt(n))`, sample stddev via Bessel's correction,
  n-1 denominator) testing whether mean realized PnL is significantly less than zero. Zero-variance
  categories (every close identical) use a finite `±1e6` sentinel, not `Infinity` —
  **`JSON.stringify(Infinity)` was confirmed live to silently serialize to `null`**, which would
  have turned "maximally strong evidence of harm" into "no evidence" the moment it round-tripped
  through `category_scores_json`. `CategoryScoreDetail` gained the `pnl_t_stat` field.
- `config.py`: `CATEGORY_SKIP_Z_CRITICAL = 1.645` — the conventional one-tailed 95%-confidence
  critical value for this exact test, not an invented number.
- `db.get_wallet_composite_scores()`: `categories` values changed shape from a bare
  `score_or_None` float to `{"score": ..., "pnl_t_stat": ...}` — both fields bot.py's sizing and
  skip decisions need. This is a breaking change to that function's return shape; every call site
  (`compute_trade_size_usd()`) and every existing test using the old flat-float shape were updated
  in the same change, not left half-migrated.
- `bot.py`: new `should_skip_category(wallet_score_entry, category)` — returns `True` only when
  `pnl_t_stat <= -config.CATEGORY_SKIP_Z_CRITICAL`. Checked in `process_trade()` immediately after
  category resolution, BEFORE `compute_trade_size_usd()` is called — a skipped copy never computes
  a size at all, logged as `skip_poor_category_performance` with the t-statistic and critical value
  in the reason string.
- Tests: 9 new in `test_bot_risk_checks.py` (`TestShouldSkipCategory`), 4 new in
  `scoreWalletCategories.test.ts` (`pnl_t_stat`, including the zero-variance sentinel and its
  JSON-roundtrip survival), plus the existing `TestComputeTradeSizeUsd`/`test_db_categories.py`
  tests updated for the new nested category shape. 130 Python tests, 83 TS tests passing.
- Live-verified: re-ran `scoreWalletCategories.ts` against all 20 tracked wallets to populate
  `pnl_t_stat` for the first time (previously-persisted category data predated this field).

**System costs & trade-offs:** the two-tier separation (a statistical significance bar for hard
skips, stricter than the bar for score-based sizing) means a wallet is never blacklisted from a
category off a small, noisy sample — only off evidence strong enough to survive a real
significance test, avoiding both false negatives (missing real harm) and false positives
(overreacting to noise).

**Why it exists:** see Rule 19.

---

## 14. Category-specialist discovery (Rule 20 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 20. **LIVE as
of 2026-07-23.** Unlike every other rule in this document, this one never touches `bot.py`'s
trading path at all — it's a standalone reporting script.

**How it works mechanically:**
- `packages/copy-trading/src/discoverCategorySpecialists.ts` (new): queries `wallet_profile` for
  candidates (`composite_score >= --min-composite-score`, default 0.2; `--all` bypasses; `--exclude`
  accepts a comma-separated address list — no hardcoded duplicate of `config.TRACKED_TRADERS` in
  TS, since that would drift against the Python source of truth).
- `rankSpecialistsByCategory()` (pure, exported, unit-tested): filters to `pnl_t_stat >= 1.645`
  (positive-evidence mirror of `config.CATEGORY_SKIP_Z_CRITICAL`), groups by category, sorts by
  `pnl_t_stat` DESCENDING — not by `score`. Live-verified this produces a genuinely different
  ordering than score-sorting would (a 1.000-score/1.82-t-stat wallet ranked below a
  0.781-score/6.03-t-stat wallet in a real run).
- Reuses `scoreWalletCategories.ts`'s exported `fetchWalletTrades`/`resolveMarketCategory`/
  `reconstructRealizedCloses`/`aggregateCategoryScores` directly — no duplicated reconstruction
  logic. `fetchMarketResolutionForDiscovery` is a small, deliberately separate copy of that file's
  private resolution-fetch helper (not exported/shared), matching the project's existing "small,
  separate copy on purpose" precedent (`polymarket_simulator.py`'s own docstring) rather than
  coupling two scripts' internals over a few lines.
- Runs via `mapWithConcurrency` (limit 5), matching `scanLeaderboard.ts`'s existing
  `PASS1_CONCURRENCY` precedent.
- Output is `console.log` only — no `wallet_profile` write, no `config.TRACKED_TRADERS` write.
- Tests: 7 new in `discoverCategorySpecialists.test.ts` (significance filtering, t-stat-not-score
  ordering, topN capping, category independence). 90 TS tests passing total.
- Live-verified against 6 real candidates (`--min-composite-score 0.6`): correctly surfaced
  category specialists across other/sports/politics, correctly ranked by evidence strength.

**System costs & trade-offs:** the default `composite_score >= 0.2` filter is a real, stated
speed-vs-completeness trade-off (107 candidates vs. 470+ unfiltered) — documented as tunable, not
hidden. A real, honest finding from the first live run: several statistically significant results
showed 100% win rates with near-zero average profit per trade (high-frequency, thin-margin
patterns) — significance confirms the pattern is real, not that it's safely copyable. **Refined by
Rule 21 (TCA filter, below)**, which showed dollar-amount was the wrong lens for this concern.

**Why it exists:** see Rule 20.

---

## 15. Transaction Cost Analysis / TCA filter (Rule 21 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 21. **LIVE as
of 2026-07-23.** Same standalone-script scope as §14 — does not touch `bot.py`'s trading path.

**How it works mechanically:**
- `scoreWalletCategories.ts` (`RealizedClose`, plumbing): each realized close now also carries
  `sharesClosed` (shares closed in that fill/resolution) and `feeRate` (the market's real
  `feeSchedule.rate` at lookup time, 0 when `feesEnabled=false`). `feeRate` is fetched via a new
  `fetchMarketFeeRate()` — a separate Gamma `/markets?slug=` call from `fetchMarketResolution`'s
  (not merged), because most closes (ordinary sells) never call `fetchMarketResolution` at all —
  only still-open positions marked to a resolution payout do — while every close needs a fee rate.
  Both category and fee-rate lookups are cached per market slug inside `reconstructRealizedCloses`
  (verified via a dedicated "resolves once per distinct market" test) to avoid redundant fetches
  across a wallet's many trades in the same market.
- `aggregateCategoryScores()` now also returns, per category: `roi` (total realized PnL / total
  cost basis — the same quantity `computeRoiScore()` already consumed internally, now surfaced),
  `avg_entry_price` (cost-basis-weighted: `totalCostBasis / totalSharesClosed`, null if no shares
  were ever closed), and `avg_fee_rate` (cost-basis-weighted average `feeRate`).
- `discoverCategorySpecialists.ts` (new logic): `estimatedRelativeSpread(price)` — pure, exported,
  unit-tested piecewise-linear interpolation (4% anchor at distance-from-0.5 ≤ 0.1, i.e. price in
  [0.4, 0.6]; 15.5% anchor at distance ≥ 0.4, i.e. price < 0.10 or > 0.90; linear between; symmetric
  around 0.5). `passesTcaFilter({roi, avgEntryPrice, avgFeeRate}, safetyBufferPct)` — pure, exported,
  unit-tested — implements the strict inequality from Rule 21 (`roi > spread/2 + feeRate*(1-price) +
  buffer`; a `null` `avgEntryPrice` fails closed, not passes). Wired into `main()` AFTER
  `rankSpecialistsByCategory()`, filtering each category's already-significant survivors further;
  rejected candidates are logged separately (`--- Rejected on TCA grounds ---`), not silently
  dropped.
- `--tca-safety-buffer` CLI flag (default 0.02 = 2%) makes the buffer configurable per the explicit
  requirement that it scale as a percentage of price, not a flat USD amount.
- Tests: 5 new in `scoreWalletCategories.test.ts` (fee-rate-cached-once-per-market, `roi`,
  `avg_entry_price`, `avg_fee_rate` computation) + 10 new in `discoverCategorySpecialists.test.ts`
  (`estimatedRelativeSpread`'s anchors/interpolation/symmetry, `passesTcaFilter`'s strict inequality
  including an exactly-at-the-bar case, a longshot-price rejection case, and a configurable-buffer
  case). 104 TS tests passing total; 130 Python tests unaffected (this is TS-only, discovery-script
  work — no `bot.py` change).
- Live-verified against the same 6 real candidates as §14's original run
  (`--min-composite-score 0.6`): all 4 category entries that cleared Rule 20's significance bar also
  cleared the TCA bar in this batch (no live example of a TCA rejection yet — the pre-filtered,
  composite_score >= 0.6 candidate pool is already fairly high-quality — but the rejection path
  itself is directly unit-tested against a constructed thin-margin-longshot case). Confirmed the
  $0.03-avg-profit wallet flagged as a possible red flag in §14 in fact carries a real 28.3% ROI —
  see Rule 21's "refines, not contradicts" note.

**System costs & trade-offs:** deliberately does not model Perold's delay/opportunity-cost terms
(Cd/Co) — our copy trades execute within seconds at $3-$10 size, so this is a stated simplification,
not a gap expected to matter in practice. Slippage is an ESTIMATE grounded in Polymarket-specific
published spread research (not exact historical order-book data, which the public API cannot
provide), sized generously (uses the full historical-order-size spread estimate rather than scaling
it down further for our smaller order) as a deliberately conservative, not optimistic, assumption.

---

## 16. Decision-outcome review prerequisites (Rule 22 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 22. **LIVE as
of 2026-07-23** — prerequisites shipped and `bot.py` restarted via dashboard's `/api/toggle` to
pick them up (confirmed live: fresh `decision_journal` rows now carry a real `rule_set_version`,
previously always NULL); `reviewOutcomes.ts` Stage 1 also shipped and live-verified this same day
(see below). Stages 2/3 (Brier calibration, structural-break test) remain a formally approved plan,
not yet implemented.

**How it works mechanically:**
- `db.py`'s module docstring updated to note one deliberate exception to its "same shapes as the
  old JSON files" invariant: `append_log()` now returns the new `decision_journal` row's id
  (`None` when the event isn't a copy/skip decision) — there's no JSON-file equivalent to preserve
  parity with, since `decision_journal`/`outcome_review` are SQLite-only concepts.
- `append_log(event)` reads two new OPTIONAL keys off the event dict — `score_breakdown` (a dict,
  `json.dumps()`'d into `decision_journal.score_breakdown_json`) and `rule_set_version` (int,
  written to `decision_journal.rule_set_version`) — both simply absent (NULL) when not given; no
  existing call site needed to change.
- `db.get_active_rule_set_version()` (new): `SELECT version FROM rule_set WHERE is_active = 1
  LIMIT 1`. Read-only — `rule_set` stays exclusively TS-owned (`scoreWallets.ts`'s
  `getActiveRuleSet()`), matching the existing `wallet_profile.status` cross-layer write boundary.
  Returns `None` on an empty/no-active-row table, never guessed.
- `bot.py`'s `main()`: `risk_state["active_rule_set_version"] = get_active_rule_set_version()` —
  read once at startup (same restart-to-pick-up-changes convention as `wallet_scores`/
  `tracked_traders`), stamped into `base_event["rule_set_version"]` so every `decision_journal` row
  `process_trade()` produces carries it, with zero extra per-trade DB reads.
- `bot.py`'s `process_trade()` (BUY path, right after `compute_trade_size_usd()`): builds
  `score_breakdown` by DUPLICATING that function's two-tier fallback logic at the call site
  (`category` tier if `wallet_score_entry["categories"][category]["score"]` is not None, else
  `composite` tier if `wallet_score_entry["composite"]` is not None, else `base`) — a deliberate
  small duplication rather than changing `compute_trade_size_usd()`'s return type from a bare float
  to a tuple, which 14+ existing tests in `test_bot_risk_checks.py::TestComputeTradeSizeUsd` assert
  against directly. Passed into the `paper_buy`/`live_buy` `append_log()` call; the returned
  `decision_journal_id` is stashed onto the position dict as `last_decision_journal_id`.
- `db.py`'s `save_state()` (the existing per-position INSERT/UPDATE branch): reads
  `pos.get("last_decision_journal_id")`. On INSERT (position open), also writes
  `paper_trade.decision_journal_id` from it. Either way (INSERT or UPDATE — i.e. an average-up
  buy), immediately follows with `UPDATE decision_journal SET linked_paper_trade_id = ? WHERE id =
  ?` using the resolved row id — many-to-one by design (`paper_trade.decision_journal_id` = opening
  decision only; every decision that touches the position gets its own `linked_paper_trade_id`).
  Re-running this UPDATE on a `persist()` call with no fresh decision (a TTP/closeout sweep) is a
  harmless no-op, not treated as an error.
- Tests: 12 new in `test_db_decision_journal.py` (`get_active_rule_set_version`, `append_log`'s new
  return value + optional fields, `save_state`'s linkage including the multi-buy
  average-up-doesn't-overwrite-the-opening-decision case), 3 new in `test_bot_risk_checks.py`
  (`TestProcessTradeScoreSnapshot` — drives a full paper BUY through `process_trade()` with
  `market_to_event`/`market_to_category` pre-populated to avoid mocking the network-facing
  resolution calls, asserting the category/composite/base tier snapshot matches
  `compute_trade_size_usd()`'s own logic in each case). 155 Python tests passing total (was 140).

**System costs & trade-offs:** the score-snapshot duplication (bot.py mirroring
`compute_trade_size_usd()`'s tier logic rather than that function returning it directly) is a real,
accepted drift risk — if that function's fallback logic ever changes, this call site must be
updated to match, and nothing enforces that mechanically today beyond the two now sitting a few
lines apart in the same file. Flagged here rather than hidden. Historical `paper_trade`/
`decision_journal` rows predating this change have no score snapshot or FK link — by design, not
backfilled (see Rule 22's "honest limitation" note) — `reviewOutcomes.ts` will need to treat their
absence as "no data," never reconstruct one from `wallet_profile`'s current (since-overwritten)
scores.

**`reviewOutcomes.ts` Stage 1 (`outcome_review` generator) — technical detail, LIVE as of
2026-07-23:**
- `packages/copy-trading/src/reviewOutcomes.ts` (new). Pure decision logic is split out for direct
  unit-testing, matching this package's existing convention (`aggregateCategoryScores`,
  `rankSpecialistsByCategory`, `estimatedRelativeSpread`/`passesTcaFilter`) of testing extracted
  pure functions rather than the DB-touching orchestration around them:
  - `buildOutcomeReviewRow(trade, scoreFactors)` — pure, exported: turns one closed `paper_trade` +
    its already-fetched score factors into either `{skipped: "missing_pnl"}` (when
    `realized_pnl_usd` is `null` — never coerced into a guessed boolean, retried on the next
    incremental run instead) or the full `outcome_review` insert values.
  - `filterUnreviewedTrades(closedTrades, alreadyReviewedIds)` — pure, exported: set-difference
    logic separated from the query that produces `alreadyReviewedIds`.
  - `fetchContributingScoreFactors(decisionJournalId)` — DB-touching, not unit-tested directly
    (same precedent as `scoreOneWallet`/`scoreOneCandidate` in the sibling scripts): joins via
    `paper_trade.decision_journal_id` specifically (the OPENING decision, 1:1) rather than the
    reverse `decision_journal.linked_paper_trade_id` (many-to-one once a position has averaged up
    more than once) — a deliberate refinement of the originally-approved plan text, documented in
    the file's own module docstring, not silently substituted.
  - `generateOutcomeReviews()` — orchestrates the above against the real DB; `main()` is a thin
    argv-free wrapper (this script takes no arguments) that logs a one-line summary.
- Tests: 13 new in `reviewOutcomes.test.ts` (`buildOutcomeReviewRow`'s skip/win/loss/fallback/
  copy-through cases, `filterUnreviewedTrades`'s set-difference cases). 117 TS tests passing in
  `@copybot/copy-trading` (was 104); 85 in `@copybot/weather` unaffected; `tsc --noEmit` clean.
- **Live-verified against the real DB**: 16 pre-existing closed `paper_trade` rows (all from before
  the score-snapshot prerequisites shipped) all got an `outcome_review` row on the first run, all
  correctly showing `contributing_score_factors_json = NULL` — the exact "honest limitation"
  documented in Rule 22, confirmed rather than assumed. `was_correct_call`/`pnl_usd`/`final_outcome`
  spot-checked by hand against the underlying `paper_trade` rows and matched. A second run wrote 0
  new rows, confirming the incremental/idempotent design actually behaves that way in practice, not
  just in the unit tests.

**System costs & trade-offs (Stage 1):** none of the 16 live-verified rows have a score snapshot —
Stage 2 (Brier calibration) and Stage 3 (structural break) will report "insufficient data" until
enough NEW decisions (made after the prerequisites shipped) close and accumulate real snapshots —
expected, not a bug, and already called out in Rule 22.

**`reviewOutcomes.ts` Stages 2 & 3 — technical detail, LIVE as of 2026-07-24:**
- `db.py`'s `get_wallet_composite_scores()` extended to surface `win_rate`/`trade_count` per
  category, not just `score`/`pnl_t_stat` — a real prerequisite gap found while building Stage 2:
  the score snapshot needs a win-rate FORECAST to calibrate against, which wasn't being captured at
  all. Backward compatible (additive dict keys only); `test_db_categories.py`'s two affected tests
  updated to the new shape (both renamed, not silently left asserting a stale shape).
- `packages/copy-trading/src/reviewOutcomes.ts`, Stage 2: `computeBrierScore(inputs)` (pure —
  mean squared forecast error), `bucketCalibration(inputs)` (pure — quintile buckets, half-open
  except the final closed bucket `[0.8, 1.0]`), `computeBrierCalibration(inputs, groupBy, minSample)`
  (pure — groups by wallet or wallet+category, gates at `MIN_BRIER_SAMPLE =
  DEFAULT_RULES.hardMinTrades`). `fetchEnrichedOutcomeReviews()` (DB-touching, shared by both
  stages) parses `contributing_score_factors_json` once, extracting `category`/`sizing_tier`/
  `forecastWinRate` (only populated when `sizing_tier === "category"` AND
  `category_score_detail.win_rate` is a number — malformed JSON degrades to "no snapshot," never
  crashes the report).
- Stage 3: `welchTTest(early, recent)` (pure — Welch-Satterthwaite df, zero-variance-both-sides
  handled with the same finite `EXTREME_T_STAT_SENTINEL` pattern `aggregateCategoryScores` already
  uses, for the same JSON-round-trip-safety reason), `tCriticalValue(degreesOfFreedom)` (pure — a
  tabulated Student's-t lookup for df 9-18, the exact range `STRUCTURAL_BREAK_WINDOW=10` fixed on
  both sides can produce, falling back to the 1.96 normal approximation only for df>18),
  `detectStructuralBreak(chronologicalPnl, windowSize)` (pure — splits into fixed early/recent
  windows, returns `null` — not a guess — when there isn't enough history for two full windows),
  `computeStructuralBreaks(inputs, groupBy, windowSize)` (pure — sorts chronologically before
  windowing, regardless of input order; `"wallet"` grouping pools every `outcome_review` row for
  that wallet including ones with no score snapshot at all, since only
  `walletAddress`/`resolvedAt`/`pnlUsd` are needed — only `"wallet_category"` grouping requires a
  snapshot, to know the category).
- `main()` runs both stages after Stage 1, printing "insufficient data" (not crashing, not
  fabricating a number) when nothing clears the relevant sample gate — the expected state today.
- Tests: 29 new in `reviewOutcomes.test.ts` (Brier: perfect/maximally-wrong/textbook-0.25/
  realistic-0.21 reference cases, bucket boundary inclusive/exclusive behavior, sample-gate
  pooling-vs-splitting between the two groupings; structural break: exact tabulated critical
  values, large-df fallback, sub-9-df conservative fallback, fractional-df rounding, a hand-
  verified Welch t-stat/df pair, sign/direction correctness, the zero-variance sentinel case,
  insufficient-data `null`, exact-boundary success, clear improvement/decline flagging, a
  no-real-difference non-flag, chronological sorting regardless of input order, wallet-vs-
  wallet_category pooling/exclusion, cross-wallet/category independence). 153 TS tests passing in
  `@copybot/copy-trading` (was 124); 155 Python tests (test count unchanged — 2 existing tests
  updated in place, not added); `tsc --noEmit` clean.
- **Live-verified end to end**: ran `pnpm review:outcomes` against the real DB — 0 new rows
  (idempotency reconfirmed), Stage 2 and Stage 3 both printed their "insufficient data" messages
  correctly rather than crashing or reporting a fabricated 0/null as if it were a real result.

**System costs & trade-offs (Stages 2/3):** two documented, deliberate refinements over the
originally-approved plan text — (1) Brier calibration is scoped to `sizing_tier === "category"`
decisions only, since no composite-level win-rate is tracked anywhere today (a future addition
could track `wallet_profile.win_rate`, the raw column `scoreWallets.ts` already populates, for the
composite tier too — not done tonight, to keep this addition scoped to what was approved); (2) the
structural-break critical value uses a proper small-df Student's-t lookup instead of the plan's
flat 1.96, because 1.96 would have systematically over-flagged at this design's actual sample
sizes. Both are documented here and in the code, not silently substituted.

**Why it exists:** see Rule 22.

---

## 17. Wash-trading suspicion screen (Rule 23 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 23. **LIVE as
of 2026-07-24.**

**How it works mechanically:**
- `packages/copy-trading/src/discoverCategorySpecialists.ts`: `flagsWashTradingSuspicion(candidate,
  thresholds)` — pure, exported, sits next to `passesTcaFilter` with the identical shape/testing
  convention. `WashTradingThresholds` interface + `DEFAULT_WASH_TRADING_THRESHOLDS` (`minWinRate:
  0.9, minTradeCount: 20, maxRoi: 0.1`) — each number's grounding documented in Rule 23.
- `CategoryCandidateResult` gained a `washTradingSuspect: boolean` field, computed in
  `scoreOneCandidate()` right alongside the existing TCA-related fields already pulled off
  `CategoryScoreDetail` — no new fetches, no new `RealizedClose` fields.
- `main()`: the flag is a REPORT ANNOTATION only — appended to a flagged wallet's console line
  (`⚠ WASH-TRADING SUSPECT`) after the existing TCA-viability line, but the wallet stays in
  `tcaViable` either way; nothing is removed, nothing is written anywhere. `parseArgs()` gained
  three CLI overrides (`--wash-min-win-rate`, `--wash-min-trades`, `--wash-max-roi`), same pattern
  as `--tca-safety-buffer`.
- Tests: 7 new in `discoverCategorySpecialists.test.ts` (insufficient-sample non-flag, the
  flagged signature itself, a genuinely-large-roi non-flag, a moderate-win-rate non-flag, all four
  threshold boundaries inclusive/exclusive exactly as documented, custom-threshold override,
  `DEFAULT_WASH_TRADING_THRESHOLDS` value assertion). 124 TS tests passing in `@copybot/copy-trading`
  (was 117); `tsc --noEmit` clean.
- **Live-verified against the real DB, zero extra API cost**: ran the flag function directly against
  the 20 currently-tracked wallets' already-computed `category_scores_json` (`wallet_profile`, from
  `scoreWalletCategories.ts`'s last run — a DB read + pure-function call, no re-fetch, no new
  network traffic). Result: 0 of 19 (wallet, category) entries flagged — a real, checked outcome,
  not a formality: the human-curated tracked list doesn't match the wash-trading signature by this
  measure, which is itself useful confirmation the screen isn't just noisy against wallets already
  known to be legitimate.

**System costs & trade-offs:** deliberately scoped to the wash-trading signal only (win rate/trade
count/roi, all pre-existing data) — a separate research finding from the same pass (wallet-age /
single-market-concentration as an insider-trading signature) needs genuinely new data not fetched
today (first-seen timestamp, position concentration) and is explicitly deferred, not silently
folded in. A near-100%-win-rate small-edge wallet flagged here could still be genuinely skilled —
this is a warning for human review, not a proven-bad classification, consistent with every other
discovery/analysis tool in this codebase never auto-deciding a tracking outcome.

**Why it exists:** see Rule 23.

---

## 18. Category quota discovery (Rule 24 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 24. **LIVE as
of 2026-07-24.**

**How it works mechanically:**
- `packages/copy-trading/src/discoverCategorySpecialists.ts`: `rankSpecialistsByCategory` (Rule 20)
  is REPLACED by two smaller pure functions — `filterSignificantByCategory(results, zCritical,
  targetCategories)` (filters + groups by category, UNCAPPED, returns `{byCategory,
  outsideTargetCategories}`) and `rankAndCapCategory(entries, topN)` (sorts by `(washTradingSuspect
  asc, pnlTStat desc)`, caps). Splitting these two concerns is the actual fix for a real bug: the
  old single function capped at top-N BEFORE TCA ran, so a TCA-rejected top-t-stat candidate could
  silently cost a category a slot instead of falling through to a lower-t-stat-but-TCA-viable one.
- New constants: `DEFAULT_QUOTA_PER_CATEGORY = 5` (renamed from `TOP_N_PER_CATEGORY`),
  `DEFAULT_TARGET_CATEGORIES = CATEGORY_TAG_SLUGS` (imported from `./polymarketCategories`, the
  same constant `resolveMarketCategory`/`resolveCategoryForEvent` already use — not a new,
  parallel category list to drift out of sync).
- `main()`'s new flow: `filterSignificantByCategory` → per-category TCA filter on the FULL
  significant pool (collecting `tcaRejected`, same as before) → `rankAndCapCategory` on each
  category's TCA-survivors → print with per-category fill counts (`politics (2/5 filled):`), an
  outside-quota `"other"` section, and a total-slots-filled summary line.
- CLI: `--quota-per-category <n>` (default 5), `--categories <comma-list>` (default the four real
  tags) — same `parseArgs()` pattern as `--tca-safety-buffer`/`--wash-*`.
- Tests: the 6 existing `rankSpecialistsByCategory` tests were rewritten (not deleted) across the
  two new functions' contracts, plus new coverage: uncapped grouping (10 significant candidates in
  one category all survive `filterSignificantByCategory`), the outside-target-categories routing
  (both the significant and insignificant cases), wash-trading-suspect sorting to the bottom of
  `rankAndCapCategory` (including the "still fills a slot when it's the only option" case), and a
  dedicated regression test reproducing the exact pre-fix bug (a TCA-rejected top-t-stat candidate
  correctly gets replaced by a lower-t-stat-but-TCA-viable one in the final capped list — proven by
  running the OLD order against synthetic data first to confirm it would have failed). 161 TS tests
  passing in `@copybot/copy-trading` (was 153); 155 Python tests unaffected (TS-only change);
  `tsc --noEmit` clean.
- **Live-verified** against the same 6-wallet batch used for Rules 20/21/23
  (`--min-composite-score 0.6`): `politics (2/5 filled)`, `sports (1/5 filled)`, `crypto (0/5
  filled) — no qualifying candidates found`, `pop-culture (0/5 filled) — no qualifying candidates
  found`, one wallet correctly routed to the outside-quota `"other"` section, total `3/20`. An
  honest partial result from a deliberately small verification batch — the real per-category fill
  rates at full candidate-pool scale (`--min-composite-score 0.2`, ~107 candidates) remain a
  genuinely open empirical question this run doesn't answer.

**System costs & trade-offs:** no cross-category backfill means a genuinely thin category (say,
`pop-culture` if Polymarket's real candidate pool has few liquid, well-covered pop-culture markets)
could permanently sit under-filled rather than the total ever reaching a full 20 — an explicit,
accepted trade-off in favor of diversification integrity over hitting a round number, matching the
user's own standing "never lower the bar just to hit a number" instinct from an earlier session.

**Why it exists:** see Rule 24.

---

## 19. Half-Kelly position sizing (Rule 25 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 25. **Built and
tested as of 2026-07-24; `bot.py` not yet restarted to pick it up** (a real, live-trading-relevant
change — restart deliberately deferred to a moment the desk confirms, same discipline as every
other change that alters real sizing behavior).

**How it works mechanically:**
- `config.py`: `KELLY_SHRINKAGE_PSEUDO_COUNT = 25`, `KELLY_FRACTION_MULTIPLIER = 0.5` — both named
  constants (not hardcoded in `bot.py`), with the `k=25` derivation (weight ≈89% at n=200, ≈17% at
  n=5) spelled out in the comment, not asserted without work shown. The whole sizing comment block
  rewritten — it used to explain why this ISN'T Kelly; that's no longer true.
- `db.py`'s `get_wallet_composite_scores()` extended a third time this week: SELECT now also pulls
  `wallet_profile.win_rate`/`trade_count_all_time` (real, already-populated columns, never
  previously selected by this function), surfaced as top-level `composite_win_rate`/
  `composite_trade_count` — distinct from any single category's `win_rate`/`trade_count` inside
  `"categories"`.
- `bot.py`: two new pure functions, split apart (not inlined) so each is independently
  unit-testable — `compute_shrunk_win_rate(observed_win_rate, trade_count, market_price,
  pseudo_count=config.KELLY_SHRINKAGE_PSEUDO_COUNT)` and `compute_kelly_fraction(win_rate,
  market_price)` (the `price ≤ 0` / `≥ 1` degenerate guard lives here, returning `0.0` rather than
  raising). `compute_trade_size_usd(wallet_score_entry, market_price, category=None)` — signature
  change: `market_price` is now a required parameter (not defaulted — price is always available at
  the real call site in `process_trade()`, and silently defaulting risked masking a real bug rather
  than failing loudly). Internally selects a `(win_rate, trade_count)` pair via the same two-tier
  fallback shape as before, instead of a single `score`.
- `process_trade()`'s call site updated to `compute_trade_size_usd(wallet_score_entry, price,
  category)`. Its `score_breakdown` tier-detection logic (Rule 22) — which duplicates
  `compute_trade_size_usd()`'s fallback logic by design, flagged in §16 as an accepted, watched
  drift risk — was updated in this SAME change to match the new win_rate/trade_count-based tiering,
  not left stale; also now records `shrunk_win_rate`/`kelly_fraction` in the snapshot.
- `should_skip_category()` — untouched. Verified by full-suite pass, not just by inspection.
- Tests: `test_bot_risk_checks.py` — 5 new (`TestComputeShrunkWinRate`: n=0 identity, identical-
  win-rate-and-price invariance, small-sample/large-sample shrinkage-weight checks, negative-n
  degrade), 6 new (`TestComputeKellyFraction`: zero-edge-at-p=price identity across three prices,
  positive/negative edge sign correctness, both degenerate-price guards), `TestComputeTradeSizeUsd`
  fully rewritten (14→14 tests, new `_entry()` shape, hand-verified formula values including the
  half-Kelly-can-never-reach-the-full-ceiling property — two of my own first-draft test assertions
  were themselves wrong until checked against the actual formula output, corrected rather than
  loosened to pass), `TestProcessTradeScoreSnapshot`'s 3 tests updated for the new tier logic and
  snapshot fields. `test_db_categories.py` — 1 new (`composite_win_rate`/`composite_trade_count`
  round-trip), 2 updated for the extended `wallet_profile` test schema and full-dict-equality
  assertions. 166 Python tests passing total (was 155); TS suite (161+85) unaffected — this is a
  Python-only change.

**System costs & trade-offs:** half-Kelly's fraction is mathematically bounded above by 0.5 (raw
Kelly ≤ `p_shrunk` ≤ 1) — under default settings the practical sizing ceiling is $6.50, not the
full $10 `MAX_TRADE_USD`, a deliberate consequence of always halving (see Rule 25's own "real,
intentional mathematical property" note) rather than something to silently work around. The
composite-tier fallback barely shrinks at all (lifetime trade counts are typically in the
thousands) — intentional, not a gap, since it only fires when there's no category-specific
evidence to prefer instead.

**Why it exists:** see Rule 25.

---

## 20. Per-wallet exposure cap (Rule 26 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 26. **Built
and tested as of 2026-07-24; `bot.py` not yet restarted to pick it up** — same held-for-go-ahead
status as Rule 25, since this also changes real trading behavior (a BUY that would have gone
through can now be skipped on a new gate).

**How it works mechanically:**
- `config.py`: `MAX_WALLET_EXPOSURE_USD = 50.0`, placed alongside `MAX_TOTAL_EXPOSURE_USD`/
  `MAX_EVENT_EXPOSURE_USD` in the existing "Portfolio-level risk controls" section, same
  `None`-disables convention.
- `risk_manager.py`: new `wallet_exposure_usd(positions, wallet_address)` — structurally identical
  to `event_exposure_usd()` (parses the same `trader|market_slug|outcome` position keys, sums
  `cost_basis_usd` for matches). Trader casing compared with plain `==`, matching
  `bot.find_cross_trader_position()`'s existing convention for this exact positions dict — not a
  new casing rule invented for this feature. `check_buy()` gained a `wallet_address=None` keyword
  parameter and a fourth check (after kill switch / total / event, before returning success) — new
  `skip_risk_wallet_cap` event type, same "would exceed" strict-inequality semantics as the other
  two caps.
- `bot.py`: the one real call site (`process_trade()`, already calling `risk_manager.check_buy()`
  right after `compute_trade_size_usd()`) now passes `wallet_address=trader` — no reordering, this
  slots into the exact call that already runs after sizing.
- Backward compatible by construction: `wallet_address` defaults to `None`, and the new check is
  skipped whenever it's `None` — no other call site in the codebase needed to change, and none
  exists besides the one in `process_trade()`.
- Tests: `test_risk_manager.py` — 3 new for `wallet_exposure_usd` (matching-trader-only summation
  across different markets, empty book, other-wallets-ignored), 5 new for `check_buy`'s wallet gate
  (blocks concentration across DIFFERENT events for the same wallet — the exact scenario a per-event
  cap alone would miss, ignores other wallets, explicit backward-compatibility case confirming no
  `wallet_address` argument means the check is silently skipped rather than erroring, lands-exactly-
  on-the-cap allowed, `None` disables). 173 Python tests passing total (was 166).

**System costs & trade-offs:** none beyond the three caps now needing to be reasoned about together
— a wallet could theoretically clear the per-event and per-wallet caps individually while still
being a fraction of `MAX_TOTAL_EXPOSURE_USD`; no new "share of total portfolio" cap was added on
top, since the three independent caps already bound this indirectly and a fourth explicit
ratio-based control wasn't requested. **Note (2026-07-25):** at the time this was written,
`MAX_WALLET_EXPOSURE_USD=$50` was ~20% of `MAX_TOTAL_EXPOSURE_USD=$250` — a meaningful single-wallet
concentration bound. `MAX_TOTAL_EXPOSURE_USD` has since been raised to $1250 while
`MAX_WALLET_EXPOSURE_USD` was deliberately left at $50 (see Rule 6's "Why it exists" for the
rationale), so that ratio is now ~4%, tighter in relative terms than originally reasoned about
here — a side effect worth knowing about, not a bug, and not rebalanced back to 20% since a
tighter per-wallet concentration bound is arguably the more conservative direction to drift in.

**Why it exists:** see Rule 26.

---

## 21. TCA entry-price floor (Rule 27 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 27. **LIVE as
of 2026-07-24.**

**How it works mechanically:**
- `packages/copy-trading/src/discoverCategorySpecialists.ts`: `DEFAULT_TCA_MIN_ENTRY_PRICE = 0.02`
  constant, alongside the existing TCA constants. `passesEntryPriceFloor(avgEntryPrice,
  minEntryPrice)` — pure, exported, sits next to `passesTcaFilter`. Returns `false` for `null`
  (same "insufficient data" convention `passesTcaFilter` uses) and `false` when `avgEntryPrice` is
  within `minEntryPrice` of either extreme, using STRICT inequality (`>`/`<`, not `>=`/`<=`) —
  landing exactly on the floor doesn't clear it, matching `passesTcaFilter`'s own strict-inequality
  convention (caught by a test that initially asserted the opposite and had to be corrected against
  the actual, intentionally-strict implementation).
- `main()`'s flow: a new filtering stage runs on `filterSignificantByCategory`'s output, BEFORE the
  existing TCA loop — each category's significant candidates are split into entry-price-floor
  survivors and rejects; only survivors ever reach `passesTcaFilter`/`rankAndCapCategory`. Rejects
  are collected separately (`entryPriceRejected`) and printed in their own report section, distinct
  from `tcaRejected`.
- `--tca-min-entry-price <n>` CLI flag, same `parseArgs()` pattern as `--tca-safety-buffer`.
- No changes to `passesTcaFilter`, `estimatedRelativeSpread`, `flagsWashTradingSuspicion`,
  `rankAndCapCategory`, `scoreWalletCategories.ts`, `bot.py`, or sizing — contained entirely to the
  discovery report's filtering stage.
- Tests: 7 new in `discoverCategorySpecialists.test.ts` (null rejected, boundary-inclusive
  rejection, below-floor rejection, comfortably-above passes, the symmetric near-1.0 case, the
  default-threshold case, and a dedicated regression test using the real `0xc21ea96b`-shaped numbers
  confirming `passesTcaFilter` alone would have passed this candidate while
  `passesEntryPriceFloor` correctly rejects it — proving the new gate does independent work, not
  something already caught). 168 TS tests passing in `@copybot/copy-trading` (was 161); `tsc
  --noEmit` clean.
- **Live-verified against the real `0xc21ea96b` data** (pulled via a one-off inspection script
  during the investigation, not left as a permanent file in the repo): all 4 categories now
  correctly rejected (`passesEntryPriceFloor` returns `false` in every case), while `passesTcaFilter`
  independently still returns `true` for the same data — confirming the rejection comes from the
  new gate specifically.

**System costs & trade-offs:** `TCA_MIN_ENTRY_PRICE = 0.02` is an explicit judgment call (stated as
such in Rule 27, not derived from further published research the way the TCA spread anchors were) —
a genuine specialist trading in the $0.001-$0.02 range, if one exists, would now be rejected
alongside settlement-snipers; this is treated as an acceptable, low-probability false-positive cost
given how strong the settlement-sniping signature is (100% win rate at the literal platform floor,
every trade, every category) — not a false-positive-free design.

**Why it exists:** see Rule 27.

## 22. Strategy-Tiered Discovery & Tracked-Trader Curation (Rule 28 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 28. **LIVE as
of 2026-07-24** — `config.TRACKED_TRADERS` was edited directly and the bot restarted to pick it up.

**How it works mechanically:**
- `packages/copy-trading/src/_wallet_deep_dive.ts`: new `classifyStrategyTiers(input:
  StrategyTierInput)` — pure, exported, six independent boolean rules (see Rule 28 for the exact
  thresholds), returns a `string[]` (never forced to a single tier). `StrategyTierInput` takes
  `category`, `winRate`, `tradeCount`, `avgPnlUsd`, `avgEntryPrice`, `entryPriceCv`,
  `categoryCount`, `top3ConcentrationPct` — all already computed elsewhere in the deep-dive script
  (`stats()` for entry-price CV, `marketConcentration()` for the top-3 figure), nothing new fetched.
  `deepDive(wallet)` now returns `Promise<DeepDiveRow[]>` with tier labels attached and prints them
  inline; a `main()` (guarded by the standard `import.meta.url` check) prints a final
  `STRATEGY-TIER SUMMARY` grouped by tier across all wallets passed on the CLI.
- `packages/copy-trading/src/_wallet_deep_dive.test.ts` (new file): 17 tests covering every tier's
  positive/negative boundary, the multi-tier-match case, and the empty-array case — a deliberate
  exception to this repo's "underscore files aren't tested" convention, justified because the
  classification logic is pure and worth getting right independent of the rest of the script's
  network-bound body. Required a guard fix: importing the module for testing was executing the
  script's top-level CLI-arg-check code and calling `process.exit(1)`, since no argv is present
  during test import — fixed by wrapping the script body in `async function main()` and gating its
  call behind `if (import.meta.url === \`file://${process.argv[1]}\`)`, matching every other script
  in this package.
- No changes to `discoverCategorySpecialists.ts`, `passesTcaFilter`, `passesEntryPriceFloor`,
  `rankAndCapCategory`, or any live filtering/sizing code — strategy-tier classification is a
  reporting label attached AFTER a candidate has already cleared every existing gate (20/21/23/24/27
  all ran unmodified against this round's candidate pool).
- `config.py`: `TRACKED_TRADERS` dict edited directly — 7 entries removed (`strict-3`, `strict-6`,
  `strict-10`, `expand-2`, `expand-3`, `geo-anon-4`, `fed-559b`), 7 added (`quant-generalist-1`,
  `political-whale-1`, `quant-generalist-2`, `yield-farmer-1`, `generalist-weak-1`,
  `sports-scalper-1`, `crypto-specialist-1`), net count unchanged at 20. Each new entry carries an
  inline comment with its specific qualifying stats. No changes to `bot.py`'s tracked-trader-loading
  logic (`get_tracked_traders()`/`TRACKED_TRADERS_SOURCE` untouched) — this is a data-only edit to
  the existing static dict.
- Confirmed via direct code read (`bot.py`'s `run_closeout_sweep()`) that de-tracking a wallet does
  not orphan its open positions: the sweep iterates every key in the in-memory `positions` dict
  unconditionally, with no filter against `tracked_by_lower`/current `TRACKED_TRADERS` membership —
  a de-tracked wallet's existing open trades still get checked for resolution and closed out
  normally; only new BUY copying stops.
- Tests: 173 Python tests still passing (no Python logic changed, `config.py` is data only); 185 TS
  tests in `@copybot/copy-trading` (168 + 17 new `_wallet_deep_dive.test.ts` cases).

**System costs & trade-offs:** the drop/add decision leaned on `wallet_profile.composite_score` for
the existing 20, which mostly reflects an older scoring run (`TRACKED_TRADERS_SOURCE` has never been
flipped to `"db"` — see the stale item in project memory) rather than a fully fresh score for every
existing wallet; cross-checked against each wallet's REAL `paper_trade` history in this bot's own
database (closed-trade PnL, win rate, open exposure) as a second, independent signal before dropping
anything, but a few of the kept wallets (e.g. `geo-pako`, `expand-1`) still have `composite_score` of
`None`/thin real-trade samples and are being kept on watch-tier trust rather than hard evidence.

**Why it exists:** see Rule 28.

## 23. "Dip & Rebound" resting paper orders (Rule 29 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md` Rule 29. **LIVE as
of 2026-07-24**, scoped to `config.LIMIT_ORDER_TRACKED_WALLETS = {strict-4}` only.

**Schema (`packages/db/src/schema.ts`, migration `0010_omniscient_lord_hawal.sql`):**
- New table `pending_execution` (Drizzle name `pendingExecution`): `id`, `walletAddress`,
  `marketSlug`, `outcome`, `sourceTradeId`, `category`, `anchorPrice`, `lowestSeenPrice` (nullable
  until the first dip below anchor), `whaleSharesAtCreation`, `targetUsd`, `status` (`pending` |
  `filled` | `expired` | `invalidated`, default `pending`), `createdAt`, `expiresAt`, `filledAt`,
  `invalidatedReason`. Indexed on `(status, walletAddress, marketSlug, outcome)`. Owned exclusively
  by `bot.py`, same ownership rule as the other `bot_*` plumbing tables — TS owns the DDL, only
  `bot.py`/`db.py` read/write rows.
- `bot_source_position` gained a `costBasisUsd` column (`real`, default 0) — the whale-side VWAP
  cost basis, maintained with the identical weighted-average-on-buy /
  proportional-reduce-on-sell model `positions[key]["cost_basis_usd"]` already used, just mirrored
  onto the tracked wallet's own holdings. Maintained for EVERY wallet's trades, not just
  `LIMIT_ORDER_TRACKED_WALLETS` ones (cheap, and keeps the door open for widening the pilot without
  a backfill gap).

**`db.py` additions:**
- `load_state()`/`save_state()` extended: `source_cost_basis` is now a third dict alongside
  `positions`/`source_positions`, read/written from `bot_source_position.cost_basis_usd` in the same
  wholesale-replace pass `source_positions` already used (one extra column in the same
  DELETE-then-INSERT loop, not a second pass).
- New CRUD, mirroring the existing `save_market_event`/`load_market_events` pattern:
  `create_pending_execution(...)` (returns the new row's id), `get_pending_execution(wallet_address,
  market_slug, outcome, status="pending")` (the single active row for a key, or `None`),
  `get_pending_executions(status="pending")` (all rows in a status, oldest first — the sweep's
  input), `update_pending_execution_anchor(id, anchor_price)`, `update_pending_execution_lowest_seen(id,
  lowest_seen_price)`, `close_pending_execution(id, status, invalidated_reason=None, filled_at=None)`
  (terminal transition to `filled`/`expired`/`invalidated`).
- Tests: `test_db_pending_execution.py` (new file, 10 tests) — a temporary-SQLite-file integration
  test (same precedent as `test_db_categories.py`/`test_db_prune.py`, never the real `data/app.db`),
  covering create/get/list/update/close and the `source_cost_basis` round-trip through
  `load_state()`/`save_state()`, including the "column existed before this row was written" default-
  to-zero case.

**`bot.py` changes:**
- New pure functions (all unit-tested in `test_bot_risk_checks.py`, no DB/network involved):
  `compute_anchor_price(existing_anchor, observed_price)` (ratchet-down-only `min()`),
  `compute_rebound_threshold(lowest_seen_price, ...)` (the hybrid tick-floor/percentage formula —
  see Rule 29 for the reasoning), `has_rebounded(current_price, lowest_seen_price)`,
  `whale_still_holding(current_whale_shares, whale_shares_at_creation, min_fraction=None)`.
- `get_market_ask_price(market_slug, outcome)` — new, buy-side counterpart to the existing
  `get_market_prices()` (which returns bid/indicative, the right pair for the SELL-side TTP check).
  Reuses `polymarket_simulator.fetch_order_book_for_outcome` directly — no bullpen/auth call, same
  no-custody read `get_market_prices()` already relies on. Returns `(best_ask, error)`.
- `_execute_buy(base_event, key, trader, market_slug, outcome, price, trade_usd, event_slug,
  score_breakdown, positions, risk_state)` — new function, extracted from what used to be inline in
  `process_trade`'s BUY branch (risk_manager.check_buy through the LIVE_MODE spread/slippage/order
  logic through the paper-mode shortfall measurement through the position-ledger write and
  `decision_journal` append — verbatim logic, only the container changed). Returns the new
  `decision_journal` id on success, `None` if any gate blocked the buy (already logged by the time it
  returns). Now the SOLE fill path for both the immediate-copy flow (every wallet not in
  `LIMIT_ORDER_TRACKED_WALLETS`) and `sweep_pending_executions()`'s rebound-confirmed fire — critical
  because `risk_manager.check_buy`'s portfolio-level gates (exposure ceilings, kill switch) are
  time-sensitive and must be evaluated fresh at the actual moment of execution, not frozen at signal
  time, which for a resting order can be hours earlier.
- `process_trade`'s BUY branch: `source_cost_basis[key]` updated unconditionally alongside the
  existing `source_positions[key]` update (same weighted-average model, mirrored). Immediately after
  the existing sizing/score-snapshot block (`trade_usd`/`score_breakdown` computed exactly as
  before), a new branch checks `trader.lower() in {w.lower() for w in
  config.LIMIT_ORDER_TRACKED_WALLETS}`: if so, computes the whale's current VWAP
  (`source_cost_basis[key] / source_positions[key]`, falling back to the raw trade price on a
  wallet's very first observed fill), then either ratchets an existing `pending` row's anchor down
  via `compute_anchor_price()` or creates a new `pending_execution` row (`expires_at = now +
  LIMIT_ORDER_TTL_SECONDS`), logs `limit_order_anchor_updated`/`limit_order_tracked`, and returns —
  never reaching `_execute_buy` at signal time at all. Every wallet NOT in the tracked set falls
  through to `_execute_buy` exactly as before Rule 29 (dead code from the old inline flow — an
  unused `our_shares = trade_usd / price` — removed as part of this same change, since `_execute_buy`
  now owns that computation).
- `process_trade`'s SELL branch reordered: `source_positions[key]`/`source_cost_basis[key]` are now
  updated UNCONDITIONALLY, before the "do we hold a position ourselves" check (previously, a SELL
  from a wallet we never actually copied returned early at `skip_sell_no_position` without ever
  touching `source_positions[key]` at all — a real, previously-latent gap that `whale_still_holding()`
  now depends on being correct: a `pending_execution` waiting on a wallet must see that wallet's SELL
  even though we hold no copy of our own yet).
- New `sweep_pending_executions(positions, source_positions, source_cost_basis, tracked_by_lower,
  risk_state, wallet_scores)` — see Rule 29 for the four-guard sequence. Wired into `main()`'s poll
  loop UNGATED (runs every cycle, unlike the TTP/closeout/prune sweeps which are gated by an elapsed-
  interval check) — deliberate: the entire point is catching a rebound or a whale exit as soon as
  possible, so any added staleness here directly works against the feature; cheap in practice since
  only wallets in `config.LIMIT_ORDER_TRACKED_WALLETS` (currently one) ever have rows to check.
- Tests: `TestComputeAnchorPrice`, `TestComputeReboundThreshold`, `TestHasRebounded`,
  `TestWhaleStillHolding`, `TestExecuteBuyExtraction` — 18 new tests in `test_bot_risk_checks.py`,
  plus the existing `TestProcessTradeScoreSnapshot` suite re-verified passing unchanged (proof the
  extraction didn't alter the immediate-copy path's behavior). One float-precision note from writing
  these: `compute_rebound_threshold()`'s boundary test computes the expected threshold via the
  function itself rather than a hand-typed value — `0.05 * 0.40` doesn't land on exactly `0.02` in
  IEEE 754, so a hand-typed boundary is one float-precision hair off the real threshold.
- 201 Python tests passing (173 before this change + 18 new pure-function/`_execute_buy` tests + 10
  new `test_db_pending_execution.py` tests). 185 TS tests unaffected (only `schema.ts` touched on the
  TS side, no logic).

**System costs & trade-offs:**
- **Adverse selection is mitigated, not eliminated.** The dip-and-rebound design is a real
  improvement over a bare-touch limit order, but a rebound can still be a temporary bounce inside a
  larger adverse move rather than a genuine reversal — `LIMIT_ORDER_REBOUND_PCT`/
  `LIMIT_ORDER_REBOUND_TICK_FLOOR` are explicit judgment calls (stated as such, not derived from
  Polymarket-specific backtested data) at the same confidence level as `TCA_MIN_ENTRY_PRICE`'s own
  framing.
- **`LIMIT_ORDER_WHALE_HOLD_MIN_FRACTION = 0.5` is a real trade-off**, not a guess dressed up as
  rigorous: too strict (e.g. requiring 100% still held) would invalidate on routine partial profit-
  taking that isn't actually informative; too loose (e.g. 0.1) would let the order fire well after a
  majority exit. 0.5 was chosen as the point where "still holding" plausibly means "still believes
  in the position," not tuned against real strict-4 data (none exists yet at this price-action
  granularity).
- **Pilot scope is intentional, not a placeholder for "roll out to everyone soon."** Every other
  tracked wallet keeps the original immediate-copy behavior; widening this requires a deliberate,
  separate decision per wallet, not a config default flip.

**Why it exists:** see Rule 29.

## 24. `wss_listener.py` — real-time on-chain trade detection (Producer half only)

**What it does:** technical companion to the Rule 29 roadmap note in
`docs/copy-trading/RISK_MANAGEMENT.md`. **NOT wired into `bot.py` at all as
of 2026-07-24** — a standalone script, run as a separate OS process, that
only ever writes into the new `live_whale_event` table. `bot.py` does not
read that table yet; there is no consumer.

**Schema (`packages/db/src/schema.ts`, migration `0011_rich_morlocks.sql`):**
new `live_whale_event` table (Drizzle name `liveWhaleEvent`) — `walletAddress`,
`contractAddress`, `eventType` (`"TransferSingle"` only, today), `direction`
(`"buy"`/`"sell"`), `tokenId`/`shareAmount` (both decimal STRINGS — a
uint256 can exceed `Number.MAX_SAFE_INTEGER`), `usdcAmount`/`price`
(nullable, never fabricated — see below), `txHash`/`logIndex`/`blockNumber`,
`detectedAt`, `consumedAt` (NULL until a future consumer processes the row).
Unique index on `(txHash, logIndex)` for idempotency — a re-delivered log
(e.g. after a reconnect re-processes a block it already saw) upserts to a
silent no-op via `INSERT OR IGNORE`, never a duplicate row.

**Why a second OS process, not asyncio bolted onto `bot.py`:** confirmed by
direct code read (same check done for Rule 29) that `bot.py`'s main loop is
a plain synchronous `while` loop with no asyncio anywhere — mixing a
long-running asyncio WebSocket client into that would risk exactly the
instability asyncio-in-a-sync-app is known for. The two processes only ever
communicate through the shared SQLite DB, safe here specifically because
that DB already runs in WAL mode with a `busy_timeout` (confirmed live via
`PRAGMA journal_mode` — `wal`).

**How trade detection works:** Polymarket trades settle on-chain as CTF
(Conditional Tokens Framework, Gnosis's ERC1155 standard) `TransferSingle`
events, not a simple DEX swap — `TransferSingle(operator, from, to, id,
value)` carries no price on its own. `wss_listener.py` additionally fetches
the full transaction receipt for every detected transfer and looks for a
paired ERC20 `Transfer` in the SAME transaction (the collateral/USDC-family
leg) to derive `price`/`usdcAmount`; if none is found, both stay `NULL`
rather than guessed (same "measurement can fail soft, never blocks
ingestion" philosophy as `bot.py`'s `measure_paper_shortfall`).

**A deliberate design choice, not a shortcut:** Polymarket's CTF Exchange
contract also emits its own `OrderFilled` event, which — with the right
ABI — would give price directly without the paired-transfer correlation.
This script does NOT use that path: its exact field layout/indexing could
not be independently confirmed against the deployed contract's real ABI in
this session (found via web search only), and a subtly-wrong event decode
is a silent-failure risk. `TransferSingle` + paired-ERC20-Transfer only
depends on the ERC1155/ERC20 standards themselves — fixed and unambiguous
forever, unlike a project-specific event schema. Flagged in the script's own
module docstring as a legitimate v2 if the real `OrderFilled` ABI is later
confirmed (e.g. against Polymarket's own `ctf-exchange` GitHub repo).

**Contract addresses — explicitly flagged as needing human verification,
not silently trusted:** CTF (`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`),
CTF Exchange V2 (`0xE111180000d2663C0091e4f400237545B87B996B`), CTF Exchange
V1 (`0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`) — sourced from a web
search plus a fetch of `docs.polymarket.com/resources/contracts`; the two
Exchange addresses were cross-checked against independent PolygonScan
listings and matched exactly, the CTF address was not independently
cross-checked. A "Neg Risk CTF Exchange" address and a "pUSD collateral
token" address also surfaced during research but were deliberately NOT
used — their format looked suspicious (unusually repetitive digit
patterns, and "pUSD" doesn't match Polymarket's well-known USDC collateral),
consistent with a possible fetch/summarization hallucination rather than a
transcription of the real page. **Confirm all three addresses above on
PolygonScan yourself before running this against anything you intend to
act on.**

**Event topics computed, not hardcoded:** `TRANSFER_SINGLE_TOPIC`/
`ERC20_TRANSFER_TOPIC` are both derived via `Web3.keccak(text=<human-
readable signature>)` at import time, not hand-typed 32-byte hex constants
— eliminates transcription-error risk on values where a single wrong hex
digit fails silently (an RPC subscription with a mistyped topic simply
never matches anything, no error raised). Verified live against the
installed `web3==7.16.0`: the computed `Transfer(address,address,uint256)`
hash matches the well-known, independently-verifiable
`0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef`.

**Resilience:** the outer loop in `main()` never lets a connection drop, a
decode error, or an unrecognized payload shape crash the process — every
failure is caught, logged with a full traceback, and followed by a
reconnect with exponential backoff (`RECONNECT_BACKOFF_INITIAL_SECONDS=2`
doubling to `RECONNECT_BACKOFF_MAX_SECONDS=60`); subscriptions do not
survive a dropped connection and are always re-established from scratch.
`KeyboardInterrupt`/`asyncio.CancelledError` are explicitly NOT swallowed —
a real shutdown signal still stops the process cleanly. Verified via a
simulated-failure harness (not a live connection): 3 consecutive forced
`ConnectionError`s were caught and retried with correctly doubling backoff,
and a subsequent `KeyboardInterrupt` propagated through cleanly.

**What WAS and was NOT verified before handing this over:**
- Verified by actually running code against the installed `web3==7.16.0`
  package (not just reading documentation): the `AsyncWeb3`/
  `WebSocketProvider`/`eth.subscribe`/`eth.unsubscribe`/
  `socket.process_subscriptions`/`keccak` calls used all exist and behave
  as expected; the `TransferSingle`/`Transfer` topic computations;
  `eth_abi.decode` round-tripping `(tokenId, shareAmount)` from a
  simulated log's data field; the address↔topic conversion helpers
  round-tripping correctly; the reconnect/backoff loop under simulated
  failures.
- NOT verified: the exact runtime payload shape of a live `eth_subscription`
  notification over a real socket (no WSS credentials or live whale wallet
  available in this environment) — `_unwrap_subscription_payload()` handles
  the shapes documented/observed in web3.py's own examples defensively, but
  the first live run against a real Alchemy/QuickNode endpoint is the real
  test of that piece specifically.

**System costs & trade-offs:** requires `pip install web3` — the project's
first actual third-party Python dependency (`requirements.txt` updated,
pinned to `7.16.0`, the version verified against). `COLLATERAL_TOKEN_DECIMALS`/
`OUTCOME_TOKEN_DECIMALS` both default to 6 (the USDC/Polymarket-CTF norm)
but are unverified assumptions, not confirmed against the deployed token
contracts — overridable via env if verification finds otherwise. A brief
blocking SQLite write happens directly inside the async loop rather than
being offloaded to an executor — a conscious simplicity trade-off given
events are processed one at a time, never fanned out concurrently, and
whale trade frequency is low; noted, not hidden.

**Why it exists:** direct follow-through on Rule 29's own roadmap note —
building toward a real-time Polygon RPC websocket feed to eventually
replace polling for trade detection, while keeping `bot.py` itself
untouched and stable.

## 25. `token_sync_worker.py` — token_id -> market_slug/outcome registry

**What it does:** closes the resolution gap §24 flagged (`live_whale_event.
token_id` has no `market_slug`/`outcome` without a further lookup). A
standalone script, same Producer-role precedent as `wss_listener.py`, that
pages through Polymarket's Gamma API and upserts every active market's
`(token_id, market_slug, outcome)` triples into a new `token_registry`
table. **`bot.py` does not read this table yet** — populating the registry
and consuming it are still two separate steps, same as `live_whale_event`.

**Schema (`packages/db/src/schema.ts`, migration `0012_wealthy_terrax.sql`):**
`token_registry` — `tokenId` (TEXT primary key, deliberately a string: a CTF
token_id is a uint256 up to 78 decimal digits, would silently lose precision
in a numeric column — confirmed live, real token_ids stored are up to 78
characters long), `marketSlug`, `outcome`, `updatedAt`. One row per
token_id, upserted in place on every sync (`ON CONFLICT(token_id) DO
UPDATE`) — not an append-only history table.

**A real course-correction, not a literal implementation of the original
ask.** Requested endpoint was `GET /markets?active=true` with offset/limit
pagination. Checked live before building anything: that endpoint's response
headers show `deprecation: true`, `sunset: Fri, 01 May 2026 00:00:00 GMT`
(already past as of this build, 2026-07-24), and `warning: 299 - "use
/markets/keyset"`. It still responds today, but building new code against a
past-sunset endpoint would be poor engineering advice — used `GET
/markets/keyset` instead (confirmed live to carry none of those deprecation
headers), Polymarket's own documented replacement.

**Pagination — cursor-based, and the correct parameter name required live
trial and error, not just reading docs.** The keyset endpoint returns
`{"markets": [...], "next_cursor": "..."}`, but neither the response schema
nor a first read of the docs page named the REQUEST parameter for feeding
`next_cursor` back in. Tried `cursor` and `after` first — both silently
ignored, the API just re-returned page 1 both times (confirmed live: 3
identical-first-page responses across 3 different guessed param names). The
correct parameter, `after_cursor`, was found via a web search that also
surfaced a GitHub issue (Polymarket/agents#227) reporting this exact cursor
as broken/ignored server-side for other users — worth knowing about, but it
worked correctly in this session's own live testing: verified across 3
consecutive real pages (300 real distinct markets, no repeats). Flagged in
the script's own module docstring in case it regresses/is intermittent by
the time this actually runs on a schedule.

**Field shapes, verified against real API responses, not assumed:**
`outcomes` and `clobTokenIds` are both JSON-*stringified* arrays (e.g. the
literal string `'["Yes", "No"]'`, not a real JSON array) — `extract_token_rows()`
parses both and zips them by index, and explicitly checks the two parsed
arrays are the same length before pairing, logging a warning and skipping
(never crashing or mispairing) a market where they don't match, are missing,
or fail to parse. `limit` silently caps at 100 server-side regardless of
what's requested — confirmed live (`limit=500` returned exactly 100 markets,
no error) — `PAGE_LIMIT = 100` set to match rather than requesting more than
the server will ever honor.

**Resilience:** each page fetch retries up to `MAX_PAGE_RETRIES=3` times
with linear backoff on `aiohttp.ClientError`/timeout before the worker gives
up on that specific page (logged, sync continues to completion regardless —
never crashes the whole run over one bad page) and reports it in the final
summary (`pages_failed`). Each page's rows are upserted immediately as that
page completes, not batched to the end of the whole run — a mid-run failure
still leaves every page synced so far intact, and a full re-run is always
safe (idempotent upserts, not appends).

**Live-tested end-to-end while building this, not just syntax-checked:**
ran a real (bounded to 3 pages) sync against the live Gamma API and the real
`token_registry` table — 300 real markets, 600 real token rows (2 outcomes
each, all binary Yes/No markets in this sample) correctly inserted,
`token_id` values confirmed stored at full precision (up to 78 characters,
no truncation). Re-ran the same page afterward to confirm idempotency: row
count stayed at 600 (no duplicates), `updated_at` correctly refreshed to a
later timestamp on the same `token_id`s.

**System costs & trade-offs:** `aiohttp==3.14.3` added to `requirements.txt`
(already present transitively via `web3`'s async provider, now a direct,
pinned dependency of this script specifically). `REQUEST_DELAY_SECONDS=0.25`
between pages is a simple fixed delay, not a real rate limiter — sufficient
for a periodic sync job hitting a public read-only API, not tuned against
any documented Gamma API rate limit (none was found). Syncing ALL active
markets (likely several thousand, based on the ~100/page pace observed) on
every run, not incrementally by `updatedAt` — acceptable for a periodic
batch job, but worth revisiting if this ever needs to run more than a few
times a day.

**Why it exists:** direct follow-through on closing `live_whale_event`'s
`token_id` resolution gap — the same gap flagged as unsolved in Rule 29's
roadmap note and §24 above.

## 26. Consumer sweep for on-chain trade detection (Rule 30 technical detail)

**What it does:** technical companion to `docs/copy-trading/RISK_MANAGEMENT.md`
Rule 30. **LIVE as of 2026-07-24** once `bot.py` is restarted to pick it up
— wires `wss_listener.py`/`token_sync_worker.py`'s producer tables into the
actual trading pipeline for the first time.

**Deviation from the original request, and why:** asked to route extracted
events "directly into the existing `_execute_buy` method." Did not build it
that way. `_execute_buy()` is only the risk-gate-plus-ledger-write step —
the Dip & Rebound / VWAP-ratchet logic (Rule 29) lives entirely in
`process_trade()`'s branching and `sweep_pending_executions()`, not in
`_execute_buy()`. A direct call would have bypassed Dip & Rebound
completely for `strict-4` (the pilot wallet it was built for) and skipped
`process_trade()`'s other gates (muted trader, duplicate position,
`MAX_BUYS_PER_TRADER_OUTCOME`, category hard-skip) that apply regardless of
detection source. Built instead: reshape each event into the same `trade`
dict the polling feed already produces, call `process_trade()` itself.

**`db.py` additions:**
- `get_unconsumed_whale_events()` — `SELECT le.id, le.wallet_address,
  le.direction, le.usdc_amount, le.price, le.tx_hash, le.log_index,
  le.block_number, tr.market_slug, tr.outcome FROM live_whale_event le
  INNER JOIN token_registry tr ON le.token_id = tr.token_id WHERE
  le.consumed_at IS NULL ORDER BY le.block_number ASC, le.log_index ASC
  LIMIT ?` (`config.WHALE_EVENT_SWEEP_BATCH_LIMIT`, default 50). INNER JOIN
  deliberately, not LEFT — an unmatched row is absent from the result
  entirely (not returned with a NULL `market_slug`), so it's naturally
  retried on a later sweep rather than force-processed with missing data.
- `mark_whale_event_consumed(event_id)` — `UPDATE live_whale_event SET
  consumed_at = ? WHERE id = ?` with `_now_ts()` (unix epoch int), NOT the
  originally-specified `CURRENT_TIMESTAMP` (a text ISO8601 string in
  SQLite — would have written a differently-typed value into a column
  every other row in this table stores as an integer). Its own dedicated
  `_connect()`/commit, independent of whatever connection `process_trade()`
  used for its own writes.
- `config.WHALE_EVENT_SWEEP_BATCH_LIMIT = 50` — new constant, caps how many
  events one sweep cycle processes so a large backlog (e.g. after
  `wss_listener.py` was down for a while) can't monopolize a single 30s
  poll cycle; the remainder is simply picked up on the next one.

**`bot.py` additions:**
- `sweep_live_whale_events(positions, source_positions, source_cost_basis,
  trader_performance, muted_traders, tracked_by_lower, risk_state,
  wallet_scores)` — new function, same file location/grouping as
  `sweep_pending_executions()`. Per event, in order: skip (but still mark
  consumed) if `direction != "buy"`; skip (still consumed) if `price` or
  `usdc_amount` is `NULL`; skip (still consumed) if the wallet isn't in
  `tracked_by_lower` (mirrors the exact gate `main()`'s own polling loop
  applies before ever calling `process_trade()` — that function itself does
  not enforce this boundary); otherwise build a `trade` dict (`user_address`,
  `market_slug`, `outcome`, `side="BUY"`, `price`, `size_usd=usdc_amount`,
  `trade_id=f"onchain:{tx_hash}:{log_index}"`) and call `process_trade()`.
  The entire per-event body is wrapped in `try/finally`, with
  `mark_whale_event_consumed(event["id"])` as the `finally` — reached
  whether `process_trade()` succeeded, was blocked by a risk gate, deferred
  to a `pending_execution`, or raised (caught and logged as an `error`
  event, re-raising nothing).
- Wired into `main()`'s poll loop immediately after Rule 29's
  `sweep_pending_executions()` call, same ungated "run every cycle" pattern
  and the same reasoning (staleness here directly works against the
  zero-latency goal this whole chain was built for).
- No changes to `process_trade()`, `_execute_buy()`, or any existing risk
  gate — this sweep is purely a new caller of the existing pipeline.

**Tests:**
- `test_db_whale_events.py` (new file, 6 tests, real temporary SQLite file —
  not mocked): the INNER JOIN returning joined `market_slug`/`outcome`,
  excluding an unmatched `token_id`, excluding already-consumed rows,
  ordering by `(block_number, log_index)`, respecting the batch limit, and
  `mark_whale_event_consumed()` writing a real integer (not a string) that
  correctly removes the row from a subsequent `get_unconsumed_whale_events()`
  call.
- `TestSweepLiveWhaleEvents` (7 new tests in `test_bot_risk_checks.py`,
  `process_trade`/`mark_whale_event_consumed`/`append_log` mocked): correct
  `trade` dict shape and field mapping (`usdc_amount` -> `size_usd`,
  synthesized `trade_id`), each skip condition (sell direction, null price,
  null usdc_amount, untracked wallet) still calling
  `mark_whale_event_consumed`, multiple events processed/consumed
  independently, and — the core guarantee — `mark_whale_event_consumed` is
  still called when `process_trade()` raises.
- 214 Python tests passing (201 before this change + 6 + 7).

**Verification note:** unlike `wss_listener.py`/`token_sync_worker.py`, no
synthetic `live_whale_event` row was inserted into the real `data/app.db`
to smoke-test this end-to-end — deliberately avoided, since a fake trade
event in the production trading-history database is exactly the kind of
thing that could confuse a later investigation into real trade history.
Verification instead relied on the real-SQLite-engine integration tests
above (not mocked) plus the mocked control-flow tests — real query
correctness and real Python routing logic were both checked, just never
against the live production DB with fabricated data.

**System costs & trade-offs:** the Rule 30 §4 double-detection risk (same
wallet watched by both this path and the polling loop) is unresolved by
design — flagged in both the code's own docstring and Rule 30's write-up,
not silently accepted or silently "fixed" by a guess at what the right
wallet-exclusion list should be. `direction="sell"` on-chain events are
consumed without any action taken (not yet wired to reduce an existing
position) — a real, stated scope limit, not an oversight.

**Why it exists:** see Rule 30.

## 27. 2026-07-25 event-driven expansion: global WSS coverage, faster sync, on-demand token resolution

**What it does:** technical companion to Rule 30's 2026-07-25 update. Four changes, code-complete,
**`bot.py`'s polling loop deliberately unchanged** — see that section for the full reasoning.

**`token_sync_worker.py`:**
- `main()` restructured into a persistent daemon: the original one-shot logic moved intact into
  `run_one_sync()`, and `main()` now loops `run_one_sync()` -> `asyncio.sleep(SYNC_INTERVAL_SECONDS)`
  forever, matching `wss_listener.py`'s structure. `SYNC_INTERVAL_SECONDS = 15 * 60`, env-overridable.
  A sync cycle raising an exception `sync_all_active_markets()` didn't already catch is logged
  (`exc_info=True`) and followed by the next scheduled run, not a crash — same resilience contract
  as `wss_listener.py`'s reconnect loop.
- Tests: new `test_token_sync_worker.py` (8 tests) — `extract_token_rows()`'s parsing/validation
  (binary market, string-not-int token_id preservation, missing slug, missing clobTokenIds, length
  mismatch, malformed JSON, already-parsed-list input) plus a simulated-failure harness for `main()`'s
  loop (3 forced `RuntimeError`s survived before a final `KeyboardInterrupt` propagates through
  cleanly) — same verification approach as `wss_listener.py`'s reconnect-loop test, since this had
  none before.

**`wss_listener.py`:**
- `WHALE_WALLETS` now defaults to `{addr.lower() for addr in config.TRACKED_TRADERS}` when
  `WHALE_WALLET_ADDRESSES` is unset (still an explicit override when set) — every tracked wallet is
  watched by default, not just `strict-4`, with no separate address list to keep in sync.
- New mint/burn filter in `_handle_log()`: before treating a `TransferSingle` as a trade, checks
  whether the relevant counterparty (`from` for a "buy," `to` for a "sell") is the zero address
  (`0x000...000`) — Gnosis Conditional Tokens' `splitPosition`/`mergePositions`/`redeemPositions`
  commonly emit `TransferSingle`-shaped events with the zero address as mint source/burn
  destination, which are not market trades. A match is logged and skipped, never inserted into
  `live_whale_event`. **Not verified against a real on-chain example** — reasoned from ERC1155
  mint/burn convention while evaluating whether this feed is complete enough to ever be the sole
  detector, not discovered via an observed failure.
- Tests: new `test_wss_listener.py` (8 tests) — address/topic round-trip, `TransferSingle` decode,
  the mint-filtered/burn-filtered/genuine-buy/genuine-sell cases for the new zero-address check
  (mocking `_find_collateral_transfer`/`insert_live_whale_event`, no network), and
  `WHALE_WALLETS`'s default-to-`config.TRACKED_TRADERS`-vs-explicit-override behavior (via
  `importlib.reload` against a patched `os.environ`).

**`polymarket_simulator.py` (the on-demand fallback):**
- New `fetch_market_by_token_id(token_id, timeout=...)` — `/markets/keyset?clob_token_ids=<id>` (NOT
  `/tokens/{token_id}`, confirmed live to 404), reusing the module's existing `_fetch_json`/
  `GAMMA_API_HOST` infrastructure rather than new HTTP client code. Matches the specific outcome
  within the resolved market whose `clobTokenIds` entry equals the queried token_id (not just the
  market's first outcome) — returns `(market_slug, outcome)` or `None` (not found; Gamma's own
  keyset endpoint returns 200 with an empty `markets` array for an unknown token_id, not an error —
  treated the same way here). Raises only on an actual HTTP/network failure, so callers can tell
  "not found yet" apart from "the lookup itself broke."
- Tests: `TestFetchMarketByTokenId` (5 new tests in `test_polymarket_simulator.py`) — resolves the
  correct outcome (not just the first one) by token_id, int-vs-string token_id matching, unknown
  token_id returns `None` not an exception, outcomes/clobTokenIds length mismatch returns `None`,
  and the defensive case where the matched market's own token_ids don't actually contain the query.

**`db.py` additions:**
- `get_unconsumed_whale_events_without_registry_match()` — the LEFT JOIN counterpart to
  `get_unconsumed_whale_events()`'s INNER JOIN: `... LEFT JOIN token_registry tr ON le.token_id =
  tr.token_id WHERE le.consumed_at IS NULL AND tr.token_id IS NULL`, same
  `WHALE_EVENT_SWEEP_BATCH_LIMIT` cap, includes `detected_at` for the TTL check below.
- `upsert_token_registry_row(token_id, market_slug, outcome)` — single-row counterpart to
  `token_sync_worker.py`'s own batch `upsert_token_registry_rows()`, same `ON CONFLICT` upsert shape.
- `config.WHALE_EVENT_FALLBACK_MAX_AGE_SECONDS = 3600` — new constant; past this age, an
  unresolvable event is given up on (marked consumed with an `error` log) rather than retried
  forever.
- Tests: 9 new tests in `test_db_whale_events.py` (real temporary SQLite file) — the LEFT JOIN
  query's four cases (no registry row, already matched so excluded, already consumed so excluded,
  `detected_at` present for the TTL check), the upsert's insert/overwrite behavior, and an
  end-to-end check within the test module that resolving a fallback makes the SAME event visible to
  `get_unconsumed_whale_events()`'s fast INNER-JOIN path afterward.

**`bot.py`:**
- `sweep_live_whale_events()` extended with a second loop (after the existing INNER-JOIN-matched
  one) over `get_unconsumed_whale_events_without_registry_match()`. Per event: call
  `polymarket_simulator.fetch_market_by_token_id()`; on a resolution, `upsert_token_registry_row()`
  then hand off to the same per-event handling as the fast path; on no resolution (or the fetch
  itself raising — treated identically to "not found," never crashes the sweep), leave the event
  unconsumed for retry unless it's already past `WHALE_EVENT_FALLBACK_MAX_AGE_SECONDS`, in which
  case mark it consumed and log an `error` explaining the give-up.
- The per-event trade-dict-building/`process_trade()`-calling logic (previously inline in the main
  loop) was extracted into `_handle_matched_whale_event()`, now shared by BOTH loops — avoids a
  second, divergence-prone copy of that logic for the fallback path. Deliberately does NOT own
  `consumed_at` itself (still each caller's job), since the two callers have different retry/give-up
  semantics.
- Tests: `TestSweepLiveWhaleEventsUnknownTokenFallback` (5 new tests in `test_bot_risk_checks.py`) —
  resolved-token upserts and processes, unresolved-but-young is left unconsumed for retry,
  unresolved-past-max-age gives up and marks consumed with an `error` log, a fetch exception is
  treated like "not found" rather than crashing the sweep (both the still-young and past-max-age
  sub-cases). Also fixed a real test-isolation gap while adding these: the existing
  `TestSweepLiveWhaleEvents` tests were not mocking the new
  `get_unconsumed_whale_events_without_registry_match()` call, meaning they were silently hitting
  the REAL `data/app.db` (harmlessly, since `live_whale_event` has zero rows there today, but
  fragile) — now mocked like every other DB call in that test class.
- 247 Python tests passing (was 231; +8 `test_token_sync_worker.py`, +8 `test_wss_listener.py`).

**Why `bot.py`'s polling loop was NOT deleted, despite being explicitly asked to:** see Rule 30's
2026-07-25 update, problem 4. Summary: `wss_listener.py` has never processed a live socket payload,
its price derivation is best-effort (some genuine trades produce no usable signal today), and
evaluating this exact request surfaced a real, previously-undocumented gap (CTF mint/burn events
could be mistaken for trades) that's now fixed but wasn't known about until asked. Deleting the only
proven detection mechanism in favor of one with those three open questions, at the same moment the
exposure cap went 5x, was judged a worse risk than the double-spend problem being solved — flagged
for Joey's decision, not silently done and not silently skipped without explanation.

**Why it exists:** direct follow-through on Joey's 2026-07-25 event-driven-architecture request —
built everything that safely could be, explicitly declined the one piece that couldn't be yet.

## 28. 'Dual-Track' CQRS: WSS executes, polling reconciles (Rule 30 2026-07-25 update, technical detail)

**What it does:** technical companion to Rule 30's Dual-Track update. Code-complete.

**`config.py`:** `OUTCOME_TOKEN_DECIMALS_ASSUMPTION = 6` — bot.py's side of the same decimals
assumption `wss_listener.py`'s own `OUTCOME_TOKEN_DECIMALS` env var makes; a real, flagged drift
risk between the two separate processes if one is ever overridden without the other.

**`db.py`:** both `get_unconsumed_whale_events()` and `get_unconsumed_whale_events_without_registry_match()`
extended to also `SELECT le.share_amount` — needed for the fallback-pricing math below, which the
original two queries didn't carry.

**`bot.py` — `_handle_matched_whale_event()`:**
- When `event["price"]`/`event["usdc_amount"]` are both present (the collateral leg was found
  on-chain): unchanged, `price_source="wss_derived"`.
- When either is `None`: calls `get_market_ask_price(market_slug, outcome)` for OUR OWN current
  execution price. If that also fails, skips (logged) — genuinely nothing to execute against. If it
  succeeds: `shares_human = share_amount / 10**OUTCOME_TOKEN_DECIMALS_ASSUMPTION`, `size_usd =
  shares_human * execution_price`, `price_source="wss_estimated"`. A missing `share_amount` also
  skips (logged) rather than guessing.
- `trade["detected_by"] = "wss"` always set here.

**`bot.py` — `_execute_buy()`:** gained a `price_source=None` keyword parameter, stamped onto
`pos["price_source"]` when provided (never overwrites with `None` — a position opened via a call
site that doesn't pass it, e.g. Rule 29's rebound fills, is simply never a reconciliation candidate,
which is correct: those paths never had a `price=None` case to begin with).

**`bot.py` — `process_trade()`:**
- `detected_by`/`price_source` extracted from the trade dict at the top (`trade.get("detected_by",
  "polling")`/`trade.get("price_source", "polling")` — untagged calls, i.e. every existing test and
  the polling feed itself, default to `"polling"`), and `detected_by` added to `base_event` (logged
  on every event this trade produces, not just the eventual buy).
- **New reconciliation branch, inserted at the very top of the BUY section — before the existing
  unconditional `source_positions[key] +=`/`source_cost_basis[key] +=` lines.** Placement here is
  the actual safety property under test: if `existing_pos.get("price_source") == "wss_estimated"`
  AND `detected_by == "polling"`, the function corrects `source_cost_basis[key] =
  source_positions[key] * price` (the share count is READ, never incremented, in this branch),
  flips `positions[key]["price_source"]` to `"reconciled"`, logs `whale_price_reconciled`
  (`old_estimated_cost_basis`/`reconciled_cost_basis`/`reconciled_whale_price`), and returns —
  never reaching the normal share-increment lines below, and never attempting `_execute_buy`.
- `_execute_buy()`'s call site inside `process_trade()` now passes `price_source=price_source`
  through.

**`bot.py` — `main()`'s polling loop:** tags `trade["detected_by"] = "polling"` on every trade
pulled from `fetch_direct_feed()`, before the untracked-wallet gate and before `process_trade()` is
called — the polling feed always carries the whale's real price/size, so it never needs to set
`price_source` itself; `process_trade()`'s own default (`"polling"`) is correct for it.

**Tests — 254 Python tests passing (was 247; +5 net after fixing one pre-existing test's changed
behavior and removing one now-obsolete assertion):**
- `TestSweepLiveWhaleEvents` rewritten: `test_null_price_skips_process_trade_but_still_consumes` no
  longer describes real behavior (a `NULL` price now falls back to `get_market_ask_price()`, not an
  automatic skip) — replaced with three tests covering the fallback succeeding (asserts `price`,
  `size_usd`, `price_source` on the resulting trade dict), the fallback also failing (still skips,
  still consumes), and a missing `share_amount` (skips, still consumes). `_call_sweep()`'s helper
  now mocks `bot.get_market_ask_price` by default — **fixes a real test-isolation bug found while
  making this change**: the OLD null-price test was, after this behavior change, making an
  UNMOCKED real network attempt via `get_market_ask_price` -> `polymarket_simulator.
  fetch_order_book_for_outcome`, which corrupted `polymarket_simulator.py`'s module-level
  `_thread_local` connection cache badly enough to make an UNRELATED test in
  `test_polymarket_simulator.py` fail, but only when both files ran in the same pytest session —
  invisible running either file alone. Caught by deliberately running combinations of test files
  together, not by chance.
- New `TestProcessTradeDualTrackReconciliation` (4 tests) — the core property under test:
  `source_positions[key]` is provably unchanged across a reconciliation (asserted directly, not just
  inferred from cost basis), `source_cost_basis[key]` corrected to the real price, our own
  `positions[key]` untouched, `price_source` flips to `"reconciled"`, no `_execute_buy` attempted
  (`risk_manager.check_buy` mock asserted never called). Plus three negative cases: an already
  `wss_derived` position falls through to the normal duplicate-position gate instead; a
  WSS-sourced event (not polling) never triggers reconciliation even against a wss_estimated
  position; no existing position at all never triggers it.
- `TestExecuteBuyExtraction` gained 2 tests: `price_source` is stamped when passed, left entirely
  absent from the position dict when not (never a stray `None` key).

**System costs & trade-offs:** the multi-buy-before-reconciliation apportionment gap (Rule 30's
problem 3) is real and undocumented anywhere else — flagged here again for visibility. `detected_by`/
`price_source` are in-memory position-dict fields only, NOT persisted to `paper_trade`'s actual
columns (no new migration was made for this) — a bot restart between a WSS-estimated buy and
polling's reconciliation would silently lose the "needs reconciling" flag, leaving that one
position's cost basis at its estimate permanently. Acceptable given the reconciliation window is
normally seconds to tens of seconds (well within one bot run), but a real limitation, not
overlooked.

**Why it exists:** see Rule 30's Dual-Track update.

## 29. Institutional-standard upgrades (Rule 31 technical detail)

**What it does:** technical companion to Rule 31. All three pieces below are OPT-IN, default OFF.

**Sell-first ordering (`bot.py`, `main()`):** `new_trades.sort(key=lambda t: (0 if
t.get("side","").upper()=="SELL" else 1, t.get("timestamp","")))` — a stable two-key sort, SELL
before BUY, chronological order preserved within each side. No new config, no opt-in needed (a
pure reordering with no behavior-changing side effect beyond same-cycle capital visibility).

**Patient exit pegging (`config.ENABLE_PATIENT_EXIT_PEGGING`, default `False`):**
- Schema: new `pending_exit_order` table (migration `0013_sharp_susan_delgado.sql`) —
  `walletAddress`/`marketSlug`/`outcome`/`positionKey`/`shares`, `initPrice`/`floorPrice`/
  `currentPrice`, `bullpenOrderId`, `closeReason`, `status` (`pending`/`filled`/
  `fallback_market_sell`/`canceled`), `createdAt`/`lastRepricedAt`/`filledAt`.
- `db.py`: `compute_live_edge_pct(min_samples=None)` — the exact per-dollar-staked blended EV
  calculation from the 2026-07-25 sizing report, now live in code (mean of
  `pnl_usd/cost_basis_usd` across `position_resolved`/`paper_sell_trailing_tp`/`paper_sell`
  events), returns `None` below `config.LIVE_EDGE_MIN_SAMPLES` (20) rather than trusting a thin
  sample. `create_pending_exit_order`/`get_pending_exit_orders`/`update_pending_exit_order_price`/
  `close_pending_exit_order` — CRUD mirroring `pending_execution`'s established shape.
- `bullpen_client.py`: `extract_order_id`/`extract_order_status` — new, same "plausible field-name
  candidates, honestly UNVERIFIED against a real response" status as the existing
  `extract_fill_price` (no live limit order has ever been placed by this bot).
- `bot.py`: `get_market_bid_ask()` (one order-book read, bid+ask together, for spread_ratio/mid —
  avoids the two separate calls `get_market_prices()`+`get_market_ask_price()` would cost).
  `compute_slippage_floor_price(mid_price, live_edge_pct, protection_fraction=None)` —
  `mid * (1 - protection_fraction * edge)`, `edge` falling back to `config.ORDER_PEG_FALLBACK_EDGE_PCT`
  (0.05) when `live_edge_pct` is `None`, clamped so a currently-negative measured edge floors at
  mid rather than inverting above it. `compute_reprice_interval_seconds(spread_ratio)` — 120s
  above `config.ORDER_PEG_LOW_LIQUIDITY_SPREAD_RATIO_THRESHOLD` (0.05), 30s at/below,
  `None` (price check failed) fails toward the slower interval. `compute_pegged_price(init_price,
  floor_price, elapsed_seconds, reprice_interval_seconds, tick_decrement=None)` — `max(init -
  floor(elapsed/interval)*tick, floor)`.
- `bot.py`: `start_patient_exit()` — places the first `limit-sell` (`--price` = current best ask,
  a legitimate maker-join price), creates the `pending_exit_order` row; returns `None` (never
  raises) on any failure (no bid/ask, `limit-sell` call fails, response has no recognizable order
  id) so the caller falls through to the existing immediate-sell path unchanged.
  `sweep_pending_exit_orders()` — per pending order: `poll-order --timeout 2` for a quick status
  check; `FILLED`/`MATCHED` books the exit via `_book_completed_exit()` (shared ledger-write
  helper, mirrors `close_position_trailing_tp`'s own write, always a full not proportional close);
  past `config.ORDER_PEG_MAX_TOTAL_WAIT_SECONDS` (600) cancels and fires an immediate market sell
  (`require_filled` + `extract_fill_price`, same as the existing immediate-sell path) — if that
  ALSO fails, logs a loud `error` ("needs manual review"), never silently retries into a possible
  double-cancel; otherwise, if a reprice is due (elapsed since `last_repriced_at` >= the current
  spread-appropriate interval) and the decayed price differs from current, cancels and replaces.
- Wired into `close_position_trailing_tp`'s `LIVE_MODE` branch only, behind the flag — `positions[key]`
  is deliberately left untouched when a patient exit starts (the sweep owns closing it later, same
  as Rule 29's `pending_execution` never touching `positions` before a confirmed fill). NOT wired
  into `process_trade()`'s proportional whale-mirroring SELL branch this round — scope limit, not
  an oversight (see Rule 31 for why).
- Tests: 20 new — `TestComputeSlippageFloorPrice` (3), `TestComputeRepriceIntervalSeconds` (3),
  `TestComputePeggedPrice` (3), `TestSweepPendingExitOrders` (5, including the two safety-critical
  cases: unfilled-past-max-wait correctly falls back to a market sell rather than staying resting,
  and a failed fallback sell logs a loud error AND leaves the position in the ledger rather than
  silently dropping it), plus `test_db_pending_exit_order.py` (6, `compute_live_edge_pct` +
  `pending_exit_order` CRUD, real temporary SQLite).

**Theta-decay TP activation (`config.ENABLE_THETA_DECAY_TP_ACTIVATION`, default `False`):**
- Schema: `bot_market_event` gained `end_date_iso` (migration `0014_tearful_roxanne_simpson.sql`) —
  no new table, extending the existing per-market memo table, same precedent as
  `holdingRewardsEnabled`.
- `db.py`: `load_market_end_dates()`/`save_market_end_date()` — mirror `load_market_categories()`/
  `save_market_category()` exactly, including the UPDATE-not-upsert semantics (safe because
  `resolve_market_end_date()` is only ever called on an already-open position, whose market
  already has a row from `process_trade()`'s BUY-time event resolution).
- `bot.py`: `resolve_market_end_date(market_slug)` — its own `bullpen polymarket market` call
  (kept separate from `resolve_market_event()`, same reasoning as `resolve_market_category`'s own
  separation: called at a different point in the lifecycle), reading `endDateIso` — verified live
  against a real market (`new-rhianna-album-before-gta-vi-926` -> `"endDateIso": "2026-07-31"`)
  before writing this. `compute_days_remaining(end_date_iso, now=None)` — whole days, floored at 0
  for a market past its own end date, `None` on unparseable input. `compute_theta_decay_activation_pct(
  days_remaining)` — the linear-interpolation formula exactly as specified, `None` days_remaining
  (end date unresolvable) falls back to the original static `config.TRAILING_TP_ACTIVATION_PCT`.
- `check_trailing_take_profit()` gained a `market_to_end_date=None` parameter; when the flag is on,
  resolves (and caches, both in-memory and via `save_market_end_date`) each market's end date on
  first need and substitutes the dynamic threshold for the static one at the activation check —
  `main()`'s one call site now passes `risk_state["market_to_end_date"]` (loaded at startup via
  `load_market_end_dates()`, same pattern as `market_to_event`/`market_to_category`).
- Tests: 11 new — `TestComputeDaysRemaining` (4), `TestComputeThetaDecayActivationPct` (4),
  `TestCheckTrailingTakeProfitThetaDecayWiring` (3 — flag off never even looks up an end date and
  behaves identically to before; flag on can arm at a lower peak than the static 50% bar would
  allow; a resolved end date gets cached and persisted for reuse), plus `test_db_market_end_date.py`
  (3, real temporary SQLite).

**288 Python tests passing total (was 254 at the start of this round).**

**Why it exists:** see Rule 31.

## 30. Paper accounting grounded in real fillable prices (Rule 32 technical detail)

**`polymarket_simulator.fetch_market_info`:** on an empty first response
from `/markets?slug=<slug>`, retries once against
`/markets?slug=<slug>&closed=true` before raising `RuntimeError`. Verified
live: `curl "https://gamma-api.polymarket.com/markets?slug=nhl-car-las-2026-06-14"`
returns `[]`; the same slug with `&closed=true` returns the real, resolved
market. This is the fix for the dominant historical `preview_unavailable`
failure mode in `measure_paper_shortfall` (141 of 586 buy events) —
`measure_paper_shortfall` was already called unconditionally on every buy;
the gap was in `fetch_market_info` silently treating "already resolved" the
same as "genuinely unknown slug."

**`bot._execute_buy`:** `actual_cost_usd = trade_usd` initialized alongside
the existing `our_shares = trade_usd / price` default. In the paper-mode
(`else`) branch, `measure_paper_shortfall`'s result is captured into a
local (`shortfall`) instead of only merged into `base_event`; when
`shortfall["shortfall_status"] == "ok"`:
```
our_shares = trade_usd / shortfall["executable_price"]
actual_cost_usd = trade_usd + shortfall.get("trading_fee_usd", 0.0) \
    + shortfall.get("network_fee_usd", 0.0)
```
`new_cost = pos["cost_basis_usd"] + actual_cost_usd` (was `+ trade_usd`
unconditionally) — the only other line touched. Any other `shortfall_status`
(`preview_unavailable`, `no_executable_price`) leaves both variables at
their pre-set defaults, i.e. exactly the old behavior, and that fallback is
still visible per-trade since `base_event.update(shortfall)` still runs
first.

**`bot.process_trade`'s SELL branch:** `exit_fee_usd = 0.0` initialized
alongside the existing `effective_price = price`. Same
`shortfall_status == "ok"` check sets `effective_price =
shortfall["executable_price"]` and `exit_fee_usd = trading_fee_usd +
network_fee_usd`; `proceeds_usd = shares_closed * effective_price -
exit_fee_usd` (was `shares_closed * effective_price`). Realized `pnl_usd`
flows from `proceeds_usd`, so this is where exit-side PnL fidelity actually
lands — the entry-side fix alone would have left half the ledger
optimistic.

**Deliberately unchanged:** LIVE_MODE's `fill_price`/`require_filled()`
path on both BUY and SELL — that path already books a real, verified fill;
today's fix only closes the equivalent gap on the paper side. No
retroactive correction of historical paper trades — the live order book at
a past moment isn't reconstructable, only checkable going forward.

**Tests:** `TestExecuteBuyExtraction::test_ok_shortfall_wires_real_price_and_fees_into_ledger`,
`::test_unmeasurable_shortfall_falls_back_to_source_price`; new
`TestProcessTradeSellShortfallWiring` (2, mirror cases on the exit side);
`test_polymarket_simulator.py::TestFetchMarketInfo::test_closed_market_found_via_retry`
(existing `test_no_market_found_raises_rather_than_silently_returning_nothing`
updated for the new two-request retry sequence). **293 Python tests passing
(was 288).**

## 31. `wss_listener.py` credential loading via `.env` (2026-07-26)

**Context:** connecting `wss_listener.py` to a real Polygon WSS endpoint
(Joey's request, to move real-time on-chain detection off the never-yet-run
placeholder) needs a credential-bearing URL — e.g. Alchemy's Polygon
Mainnet WSS endpoint, `wss://polygon-mainnet.g.alchemy.com/v2/<API_KEY>`.
The existing `POLYGON_WSS_URL = os.environ.get("POLYGON_WSS_URL")` (see
Rule 24) required a real shell-exported env var — this codebase had no
`.env`-file loading anywhere in Python (`config.py`'s
`PRIVATE_POLYGON_RPC_URL` has the same gap), unlike the TS/Next side, which
already reads `.env` per the existing `.env.example`.

**Mechanism:** `wss_listener.py` now calls `load_dotenv()` (from
`python-dotenv`, already present in the environment; pinned in
`requirements.txt` as `python-dotenv==1.0.1`) before any `os.environ.get`
call. `load_dotenv()` never overrides a variable already set in the real
shell environment — a real `export POLYGON_WSS_URL=...` still wins, `.env`
only fills the gap. `.env` is git-ignored (`.gitignore:27`) and
`.env.example` gained `POLYGON_WSS_URL=` and `WHALE_WALLET_ADDRESSES=`
entries with setup notes. The actual credential is not typed into chat or
committed anywhere — it goes directly into a local `.env` file.

**Not yet done:** actually running `wss_listener.py` against a live
`POLYGON_WSS_URL` — this needs Joey's own Alchemy (or equivalent) API key,
which only she can obtain and place in `.env`. Once live, this remains the
Producer half only (writes `live_whale_event`, consumed by `bot.py`'s
`sweep_live_whale_events()` per Rule 30/28) — no change to that consumer
wiring today.

**Update, same day:** connected for real, on a freshly-rotated key (the
first key was pasted into chat and immediately decommissioned by Joey in
the Alchemy dashboard). Verified live: 40/40 subscriptions acknowledged
(20 wallets × buy+sell), a real `eth_subscription` payload arrived from a
tracked wallet mid-verification and correctly wrote to `live_whale_event`
with `price=NULL` (transaction receipt not yet indexed at query time — the
designed degrade-not-drop behavior, not a dropped packet; Dual-Track
reconciliation backfills the price via polling). Also found and fixed a
second credential leak independent of the chat paste: `web3`'s own
`WebSocketProvider`/`PersistentConnectionProvider` loggers log the raw
endpoint URL (API key included) at INFO via their own class-attribute
loggers, which our shared root `logging.basicConfig` handler would
otherwise write straight into `wss_listener.out.log` in plaintext. Both
silenced to WARNING at import time. 2 new tests
(`TestCredentialLeakPrevention`).

## 32. Entry-side Marketable Limit Orders / FAK (Rule 33 technical detail)

**`config.py`:** `ENABLE_ENTRY_SLIPPAGE_CEILING_FAK = False` (opt-in,
default off — existing `buy --max-price` behavior unchanged unless
flipped). `ENTRY_SLIPPAGE_CEILING_CAP_PCT = 0.30`.

**`bot.compute_entry_slippage_ceiling_pct(live_edge_pct=None, protection_fraction=None)`:**
pure function, mirrors `compute_slippage_floor_price`'s construction
exactly (same negative-edge-floors-at-zero-before-scaling behavior, same
`ORDER_PEG_FALLBACK_EDGE_PCT` fallback when `live_edge_pct` is `None`):
```
edge = max(live_edge_pct if live_edge_pct is not None else ORDER_PEG_FALLBACK_EDGE_PCT, 0.0)
raw = protection_fraction * edge
return max(SLIPPAGE_TOLERANCE, min(ENTRY_SLIPPAGE_CEILING_CAP_PCT, raw))
```

**`bullpen_client.extract_filled_shares(response)`:** new, mirrors
`extract_fill_price`'s best-effort/UNVERIFIED status. Tries `filled_shares`,
`shares_filled`, `matched_shares`, `shares`, `size` in order. Returns `None`
(not `0`) when nothing matches, so a genuinely-zero real fill stays
distinguishable from "response shape doesn't tell us" — the caller must not
treat the two the same way.

**`bot._execute_buy`'s `LIVE_MODE` branch:** forks on
`config.ENABLE_ENTRY_SLIPPAGE_CEILING_FAK` before the existing spread/
slippage-ceiling pre-checks resolve into an actual order:
- **On:** `live_edge_pct = compute_live_edge_pct()` →
  `ceiling_pct = compute_entry_slippage_ceiling_pct(live_edge_pct)` →
  `ceiling_price = round(price * (1 + ceiling_pct), 4)` →
  `target_shares = round(trade_usd / ceiling_price, 2)` → `bullpen
  polymarket limit-buy <slug> <outcome> --price <ceiling_price> --shares
  <target_shares> --expiration fak --yes`. Worst-case spend is capped at
  exactly `trade_usd` (fully filled at the ceiling); any better fill price
  spends less, never more.
  - `fill_price` AND `filled_shares` both present: `our_shares =
    filled_shares`, `actual_cost_usd = filled_shares * fill_price` — the
    ledger reflects exactly what filled, correct even on a partial FAK
    fill.
  - `fill_price` present, `filled_shares` missing: falls back to
    `our_shares = trade_usd / fill_price` (the same full-budget assumption
    the plain market-buy path already made — a pre-existing limitation,
    not a new one), flagged via
    `base_event["fill_accounting"] = "fak_shares_unknown_assumed_full_budget"`.
  - Neither present: `fill_accounting = "fallback_source_price"` +
    `raw_trade_response` logged, same as the existing market-buy path.
  - `require_filled()` (unchanged) still rejects unless `status == "MATCHED"`
    with real `transaction_hashes` — a fully-killed FAK order (nothing
    filled) raises and is logged `failed_trade`, never silently recorded.
- **Off (default):** identical to the pre-existing `buy --max-price`
  behavior, unchanged.

**Verified against the real bullpen CLI before trusting any of this**
(`bullpen polymarket limit-buy <slug> Yes --price 0.05 --shares 5
--expiration fak --preview --output json`, `--preview` — no funds moved):
command syntax accepted, response included a `shares` field (one of
`extract_filled_shares`'s real candidates), `would_submit: false` /
`exchange_request_submitted: false` confirming nothing executed.

**Go-live readiness check, same session — found a hard blocker unrelated
to any of the above:** `bullpen polymarket preflight --output json` →
`POLYMARKET_WALLET_SELECTION_UNAVAILABLE`, `"terminal": true`,
`"retryable": false`, `"safe_to_retry": false`. Bullpen's own account-level
wallet routing is broken (its error names the cause: `usergate selected
Polymarket wallet ... with unsupported wallet_type=3; refusing Safe/Proxy/
Deposit fallback for write path`) and its own response explicitly
instructs: do not relogin, switch wallets, or retry any money-moving
command until Bullpen support confirms the selected wallet route via
`bullpen support bundle --refresh --include-order-credit --output json`.
**`config.LIVE_MODE` was left at `False`** — this is a Bullpen-platform-side
account issue, not a code readiness question, and retrying against a
`terminal`/non-retryable error is exactly what their own guidance says not
to do.

**Tests:** `TestComputeEntrySlippageCeilingPct` (6 — mid-range, floor clamp,
cap clamp, negative-edge-floors-at-zero, `None`-edge fallback, custom
`protection_fraction`), `TestExtractFilledShares` (5),
`TestExecuteBuyFakIntegration` (5 — command construction with the correct
`--price`/`--expiration fak`, partial fill books only the actual filled
shares, full fill, shares-unknown fallback flagged, a fully-killed order
returns `None`/logs `failed_trade` rather than recording a position).
**311 Python tests passing (was 293).**

## 33. Wallet-address casing fix (Rule 34 technical detail)

**`bot.position_key(trader, market_slug, outcome)`:**
```python
return f"{trader.lower()}|{market_slug}|{outcome}"
```
Only `trader` is lowercased — market slugs/outcomes aren't case-ambiguous
the way an address is. Confirmed via grep before editing that this is the
ONLY place `bot.py` builds a `"trader|market_slug|outcome"`-shaped key
(two call sites, both already routed through this helper: `_execute_buy`'s
Rule-29 rebound path and `process_trade`'s immediate-copy path) — no
second key-construction site needed the same fix.

**`risk_manager.wallet_exposure_usd(positions, wallet_address)`:**
```python
wallet_lower = wallet_address.lower() if wallet_address else wallet_address
...
if trader.lower() == wallet_lower:
    total += pos.get("cost_basis_usd", 0.0)
```
Compares both sides lowercased rather than depending on `position_key()`'s
normalization alone — defense in depth for the one function directly
enforcing Rule 26's per-wallet cap.

**`bot.find_cross_trader_position()`:** same treatment,
`other_trader.lower() == trader.lower()`, so a stale differently-cased key
isn't misread as a genuinely different trader (which would let Rule 3's
duplicate-exposure guard silently miss a real duplicate).

**Data migration**, run once against `data/app.db` with `bot.py` stopped:
```sql
UPDATE paper_trade SET wallet_address = LOWER(wallet_address)
  WHERE wallet_address != LOWER(wallet_address);
UPDATE bot_event_log SET trader_address = LOWER(trader_address)
  WHERE trader_address IS NOT NULL AND trader_address != LOWER(trader_address);
UPDATE decision_journal SET wallet_address = LOWER(wallet_address)
  WHERE wallet_address != LOWER(wallet_address);
```
Pre-flight check (zero rows returned, confirmed before running the
migration): `SELECT LOWER(wallet_address), market_slug, outcome, COUNT(*)
FROM paper_trade WHERE status='open' GROUP BY 1,2,3 HAVING COUNT(*) > 1` —
no open position needed merging, only relabeling. Post-migration
invariants checked and held exactly: open-position count (104 -> 104),
`SUM(cost_basis_usd)` on open rows, closed-trade count (115 -> 115),
`SUM(realized_pnl_usd)` on closed rows, `bot_event_log`/`decision_journal`
row counts — all unchanged, confirming the migration only rewrote address
strings, never touched a shares/cost/pnl column. A full `data/app.db`
backup was taken before running it
(`data/app.db.bak-pre-casing-migration-<timestamp>`).

**`bot_source_position` needed no migration**: `db.save_state()` does a
full `DELETE FROM bot_source_position` + re-insert every call (not a diff),
so it self-heals to the new lowercase casing on bot.py's very next save —
confirmed by reading `save_state()` before deciding this table was out of
scope for the migration.

**Restart safety** (why `bot.py` was stopped before either the code or the
DB changed, not patched live): `save_state()`'s fail-safe closes any
currently-open DB row whose key isn't found in the in-memory `positions`
dict, logging `reconciled_missing_from_state`. A running old-code process
holds OLD-cased keys; migrating the DB underneath it would make its next
save fail to match those keys against the now-lowercased rows and
incorrectly close them. Verified post-restart: exactly 104 open positions
before and after, zero `reconciled_missing_from_state` closes, zero
errors in the fresh startup log.

**Tests:** `TestPositionKeyCasing` (3 — lowercases trader, same wallet
different casing produces the same key, slug/outcome untouched),
`TestFindCrossTraderPositionCasing` (2), `TestExposure::test_wallet_exposure_matches_regardless_of_casing`
(1, `test_risk_manager.py`). **317 Python tests passing (was 311).**

## 34. First mute review (Rule 35 technical detail)

**Root cause confirmed by reading `check_circuit_breaker()`**:
`if key in muted_traders: return` at the top means a mute is checked and
never re-evaluated — no code path anywhere clears it once set. Confirmed
separately in `process_trade()`: the mute check (`if trader.lower() in
muted_traders: return`) sits before `_execute_buy()` is ever called, so a
muted wallet generates zero new copy trades, which means
`check_circuit_breaker()` (only ever called after a REAL closed copy
trade) never runs again for it either — performance tracking freezes the
instant a wallet mutes. This is why time alone can't fix it: nothing
observes whether the wallet's edge actually recovered.

**Exact DB operations run** (`bot.py` stopped first — see Rule 34's own
restart-safety note, same reasoning applies here: a running process's
stale in-memory `muted_traders` would otherwise get re-persisted over a
direct clear on its next `save_state()` call):
```sql
UPDATE wallet_profile SET circuit_breaker_muted=0, mute_reason=NULL, muted_at=NULL
  WHERE wallet_address IN (
    '0xdc767c90ec054dd8250cf27cb4b2f675c9807842',  -- crypto-specialist-1
    '0xe16d3f2a5807999b358affd9445c3a09e45e5e30',  -- strict-4
    '0x56acab44cfca2e88bb9b3406890aea7bfa0cd77e'   -- strict-10
  );
```
`consecutive_losses` was checked (already 0 for all three, from a win at
the end of each one's `recent_results_json` rolling window) before
deciding NOT to also reset it — no immediate re-mute risk from stale
counters, so leaving the real history intact was both simpler and more
honest than wiping it.

**`config.TRACKED_TRADERS`**: `strict-2`, `geo-denizz`, `generalist-weak-1`
removed (comments left in place explaining each, matching the established
Rule 31 pattern for `strict-1`). 20 -> 17 entries. Verified via
`python3 -c "import config; print(len(config.TRACKED_TRADERS))"` -> 17,
and via direct membership checks that exactly the 3 intended wallets were
removed and the 3 reinstated wallets remained present.

**Verification after restart**: startup log line confirmed
`tracking 17 trader(s)`; cross-referencing `config.TRACKED_TRADERS` against
`wallet_profile.circuit_breaker_muted` showed 15 of 17 actively copying
(`fed-warren-buffett`, `yield-farmer-1` correctly still muted — untouched
by this review); zero `[ERROR]`/`[CRITICAL]` lines in the log since the
restart.

**Tests:** none new — this was a config/data change (wallet membership +
mute-flag clears via direct SQL), not a new code path; Rule 34's own tests
already cover the underlying mechanism. **317 Python tests passing,
unchanged.**

## 35. VIP exposure cap + EV-based circuit breaker (Rule 36 technical detail)

**`risk_manager.wallet_exposure_cap_usd(wallet_address)`:**
```python
if wallet_address is None:
    return config.MAX_WALLET_EXPOSURE_USD
override = config.VIP_WALLET_EXPOSURE_CAP_USD.get(wallet_address.lower())
return override if override is not None else config.MAX_WALLET_EXPOSURE_USD
```
`check_buy`'s wallet-cap check (previously gated only on
`config.MAX_WALLET_EXPOSURE_USD is not None`) now calls this first and
uses its result as the effective cap, falling through to the same
`skip_risk_wallet_cap` block unchanged otherwise.
`config.VIP_WALLET_EXPOSURE_CAP_USD = {"0x65018f9fc473f6e920b8929a375d39c26a461220": 150.0,
"0x510904c9a58f5c5ad799a1b44947077564175e9c": 100.0}` (strict-7,
political-whale-1).

**`bot.compute_wallet_ev_t_statistic(returns)`:** pure one-sample t-stat —
`mean / sqrt(variance/n)`, `variance` via the standard `(n-1)`-denominator
sample formula. Returns `None` below 2 samples or when `variance < 1e-12`
(an epsilon, not a bare `<= 0` check — floating-point-identical returns
can leave a nonzero-but-negligible variance from representation error,
which would otherwise blow `t` up to an arbitrarily huge, meaningless
magnitude — caught live by a test before this shipped, not assumed away).

**`bot.check_circuit_breaker(trader, nickname, pnl_usd, cost_basis_usd, trader_performance, muted_traders)`:**
gained a required `cost_basis_usd` parameter — all 4 call sites
(`close_position_trailing_tp`, `run_closeout_sweep`, `_book_completed_exit`,
`process_trade`'s SELL branch) updated to pass their already-local
`cost_basis_closed`/`pos["cost_basis_usd"]`. Body:
```python
if cost_basis_usd is None or cost_basis_usd < config.MUTE_MIN_TRADE_COST_USD:
    return
key = trader.lower()
perf = trader_performance.setdefault(key, {"recent_returns": []})
perf["recent_returns"].append(pnl_usd / cost_basis_usd)
perf["recent_returns"] = perf["recent_returns"][-config.MUTE_EV_MIN_SAMPLES:]
if key in muted_traders:
    return
if len(perf["recent_returns"]) >= config.MUTE_EV_MIN_SAMPLES:
    t_stat = compute_wallet_ev_t_statistic(perf["recent_returns"])
    if t_stat is not None and t_stat <= -config.CATEGORY_SKIP_Z_CRITICAL:
        muted_traders[key] = {"muted_at": now_iso(), "reason": ...}
```
Note the return-history append happens BEFORE the `if key in muted_traders`
early-return — deliberately preserved from the old design: an
already-muted wallet's real-trade history keeps updating (sells against
an already-open position aren't blocked by a mute), just never produces a
second mute reason. This is exactly the data a future Shadow Rehab
mechanism would read.

**Persistence (`db.py`)**: DB columns unchanged
(`recent_results_json`/`consecutive_losses` on `wallet_profile`) — no
migration. `load_state()`'s in-memory key renamed `recent_results` ->
`recent_returns`; what's JSON-encoded inside changed from booleans to
floats. `save_state()` always writes `consecutive_losses = 0` now (no
longer computed) and reads `perf.get("recent_returns", [])` instead of
`"recent_results"`.

**Config (`config.py`)**: `MUTE_CONSECUTIVE_LOSS_STREAK` and
`MUTE_WIN_RATE_THRESHOLD` deleted (confirmed via grep before deleting:
referenced nowhere outside `check_circuit_breaker` itself).
`MIN_TRADES_FOR_WIN_RATE_MUTE` renamed `MUTE_EV_MIN_SAMPLES` (same value,
10 — same reasoning: below this, any statistical test is unreliable).
New: `MUTE_MIN_TRADE_COST_USD = 1.0` (half of `MIN_TRADE_USD`'s $3 floor —
anything materially below $1 cannot be a deliberate copy, only a
fragment/remainder).

**Verified before trusting the "catches geo-anon-3 too" claim — and it
didn't hold**: `bot.compute_wallet_ev_t_statistic()` run against
`geo-anon-3`'s real 12-trade return list
(`[0.124, 0.087, -1.0, 0.019, 0.075, 0.099, 0.054, 0.039, -1.0, 0.022, 0.075, 0.082]`)
gives **t=-0.92**, far short of the -1.645 critical value. Corrected in
both the code comments and test suite before merging rather than shipped
overstated — see Rule 36 for the full reasoning on why a t-test structurally
can't catch this fat-tailed pattern with a realistic sample size.

**Tests:** `TestVipWalletExposureCap` (5 — VIP wallet gets override,
non-VIP gets flat cap, `None` wallet gets flat cap, `check_buy` allows a
VIP past the flat cap, `check_buy` still blocks a VIP past its own higher
cap, a non-VIP is blocked at the flat cap unchanged),
`TestComputeWalletEvTStatistic` (5 — clearly negative, clearly positive,
the honest geo-anon-3 near-miss, <2 samples returns `None`, zero-variance
returns `None`), `TestCheckCircuitBreakerEvBased` (8 — dust trade ignored
entirely, a few bad trades in an otherwise-good wallet doesn't mute, a
short losing streak inside a strong history doesn't mute (the confirmed
fix), genuine statistical significance mutes, the single-outlier honest
limitation, already-muted wallets get no second mute reason, already-muted
wallets' return history still updates, below-minimum-samples never mutes
regardless of severity). **336 Python tests passing (was 317).**

## 36. Shadow Rehab + pool-refill script (Rule 37 technical detail)

**`bot._execute_shadow_buy(base_event, key, market_slug, outcome, price, shadow_positions)`:**
same paper-shortfall-aware accounting as `_execute_buy`'s paper-mode
branch, but fixed `config.SHADOW_REHAB_TRADE_USD` size, no
`risk_manager` call, no `MAX_BUYS_PER_TRADER_OUTCOME` cap, writes only to
the `shadow_positions` dict passed in (never `positions`). Logs
`shadow_rehab_buy`.

**`process_trade()`'s BUY branch**, inside the existing
`if trader.lower() in muted_traders:` block:
```python
if config.ENABLE_SHADOW_REHAB and shadow_positions is not None:
    _execute_shadow_buy(dict(base_event), key, market_slug, outcome, price, shadow_positions)
```
(a `dict()` copy of `base_event`, not the original — `_execute_shadow_buy`
mutates it via `.update(shortfall)`, and the original still needs to be
usable in the `return` right after, even though nothing currently reads
it again — defensive, not required by today's control flow.)

**`process_trade()`'s SELL branch**, inserted right after
`source_cost_basis[key]` is updated and BEFORE the `pos = positions.get(key)`
real-position check (so it still runs even when we hold no real
position — the exact case for a muted wallet):
```python
if config.ENABLE_SHADOW_REHAB and shadow_positions is not None:
    shadow_pos = shadow_positions.get(key)
    if shadow_pos and shadow_pos.get("shares", 0) > 0:
        # same fraction_sold, same measure_paper_shortfall-priced close
        # as the real SELL branch below it, logged as "shadow_rehab_sell"
        ...
```
Always uses `measure_paper_shortfall` regardless of `config.LIVE_MODE` —
a shadow trade is inherently a simulation.

**`bot.sweep_shadow_rehab(muted_traders)`:**
```python
if not config.ENABLE_SHADOW_REHAB:
    return
for key in list(muted_traders.keys()):
    returns = get_shadow_rehab_returns(key, limit=config.MUTE_EV_MIN_SAMPLES)
    if len(returns) < config.MUTE_EV_MIN_SAMPLES:
        continue
    t_stat = compute_wallet_ev_t_statistic(returns)
    if t_stat is not None and t_stat >= config.CATEGORY_SKIP_Z_CRITICAL:
        del muted_traders[key]
        append_log({"event_type": "shadow_rehab_reinstated", ...})
```
Called every poll cycle in `main()`, alongside the other cheap per-cycle
sweeps. Mutates `muted_traders` in place; relies on the caller's next
`persist()` to write the clear through — no direct `wallet_profile` write
in this function.

**Threading `shadow_positions` through the call graph**: added as an
optional (`=None`-defaulting) parameter to `process_trade()`,
`_handle_matched_whale_event()`, and `sweep_live_whale_events()` — the
Dual-Track WSS path gets Shadow Rehab too, not just the polling path.
Defaults to `None` specifically so every existing test that calls these
functions directly without the new parameter keeps working unchanged
(confirmed: full suite green before writing a single new test).

**`main()`**: `shadow_positions = load_shadow_positions()` at startup,
alongside the existing `state = load_state()`; `persist()` extended to
also call `save_shadow_positions(shadow_positions)`; `sweep_shadow_rehab(muted_traders)`
called every cycle.

**`db.py` additions** — all new, no schema migration (existing
`paper_trade` table, a new `strategy` value; existing `bot_risk_state`
key-value table for the tracked-traders publish from Rule 36):
- `load_shadow_positions()` / `save_shadow_positions(shadow_positions)`:
  same insert/update/fail-safe-close diff pattern as `save_state()`'s
  positions handling, scoped to `strategy='shadow_rehab'` only, no
  `source_positions`/`trader_performance`/`decision_journal` linkage.
- `get_shadow_rehab_returns(wallet_address, limit=None)`: closed
  `shadow_rehab` rows' `realized_pnl_usd/cost_basis_usd`, most-recent
  first.
- `_CLOSE_REASON_BY_EVENT` tuples extended with a `strategy` field
  (`"shadow_rehab_sell": ("source_sell", False, "shadow_rehab")` added);
  `_maybe_close_paper_trade()`'s `UPDATE` now filters on that strategy
  instead of a hardcoded `'bot_filtered'` — verified with a real
  temporary-SQLite test that a `shadow_rehab_sell` event closes ONLY the
  shadow row when both a real and shadow row are open for the same
  wallet/market/outcome simultaneously (and vice versa for `paper_sell`).
- `get_muted_wallets()`, `get_ever_tracked_wallets()`,
  `get_pool_refill_candidates(exclude_addresses_lower, min_composite_score, limit=None)`:
  the three pool-refill queries, each independently testable.

**`propose_pool_refill.py`** (new standalone script, same convention as
`reset_kill_switch.py`): reports tracked/muted/active counts vs.
`config.TARGET_ACTIVE_TRADER_COUNT`, and if short, prints ranked
candidates. Never writes to the DB or `config.py` — output only. Run live
against the real DB before considering this done (not just unit-tested):
correctly reported `Tracked: 17 | Muted: 1 | Actively copying: 16 |
Target: 20` and proposed 4 real, genuinely-untried candidates ranked by
`composite_score`.

**Config (`config.py`)**: `ENABLE_SHADOW_REHAB = True`,
`SHADOW_REHAB_TRADE_USD = 5.0`, `TARGET_ACTIVE_TRADER_COUNT = 20`,
`POOL_REFILL_MIN_COMPOSITE_SCORE = 0.2` (verified against
`discoverCategorySpecialists.ts`'s real `DEFAULT_MIN_COMPOSITE_SCORE`
constant before reusing the number, not assumed).

**Tests:** `TestExecuteShadowBuy` (3 — real-price wiring, source-price
fallback, multiple buys average up with no cap), `TestProcessTradeShadowRehabWiring`
(5 — muted buy books a shadow position, non-muted buy never touches it,
a sell closes a shadow position with no real position held, the flag
disables it, a caller passing no `shadow_positions` at all is a safe
no-op), `TestSweepShadowRehab` (5 — significant positive edge reinstates,
insignificant returns stay muted, below-minimum-samples stays muted
regardless of severity, the flag disables it, an empty `muted_traders`
is a no-op that never even queries). `test_db_rule37.py` (21, real
temporary SQLite): shadow position persistence (5), shadow-return
computation (5), the strategy-scoped close (2), `get_muted_wallets` (2),
`get_ever_tracked_wallets` (2), `get_pool_refill_candidates` (5).
**370 Python tests passing (was 336).**

**One flake found and fixed along the way, in a NEW test, not
pre-existing**: an early draft of
`test_non_muted_wallet_buy_never_touches_shadow_positions` didn't mock
`measure_paper_shortfall`, so a non-muted BUY fell through the real
`_execute_buy` paper-mode path into a genuine network call — bisected
with `pytest` node-id combinations across files/classes/individual tests
until isolated to that exact test, rather than assumed to be the
already-known `_thread_local` connection-cache flake from earlier this
session (confirmed those were a different, already-fixed issue by
checking git/session history before concluding this was new).

## 37. Full audit: `load_state()`'s missed casing normalization (Rule 38 technical detail)

**`db.load_state()`** — the `bot_source_position` read loop:
```python
for row in cur.fetchall():
    parts = row["key"].split("|")
    if len(parts) == 3:
        key = f"{parts[0].lower()}|{parts[1]}|{parts[2]}"
    else:
        key = row["key"]
    source_positions[key] = source_positions.get(key, 0.0) + row["shares"]
    source_cost_basis[key] = source_cost_basis.get(key, 0.0) + row["cost_basis_usd"]
```
Changed from a plain `source_positions[row["key"]] = row["shares"]`
overwrite to an additive `.get(key, 0.0) + ...` specifically so two DB
rows folding to the same lowercased key SUM rather than one clobbering
the other — confirmed live via direct query that 150 such pairs existed
before this shipped (`SELECT key, shares, cost_basis_usd FROM
bot_source_position` grouped by lowercased key, `HAVING COUNT(*) > 1`).

**Verification, in order**: (1) confirmed the bug live against
`data/app.db` before writing any fix — same discipline as every other
finding this session; (2) wrote a failing test first
(`test_duplicate_casing_rows_are_merged_on_load_not_overwritten`,
`test_db_pending_execution.py`) inserting two raw rows with the same
post-lowercase key and asserting the summed result; (3) two pre-existing
tests in the same file broke as an expected, correct side effect
(`test_save_state_writes_cost_basis_and_load_state_reads_it_back`,
`test_missing_source_cost_basis_defaults_to_zero` — both asserted the
OLD verbatim-casing round-trip) and were updated to expect the now-
lowercased key, not reverted; (4) restarted the real running `bot.py`
and confirmed against the live DB afterward: 1642 rows, 0 mixed-case, 0
duplicate groups, 0 `[ERROR]`/`[CRITICAL]` lines since restart.

**Audit scope covered, all clean**: bare `except:` clauses, mutable
default arguments, f-string/`.format()`-built SQL (injection risk vs.
parameterized queries — all real queries in this codebase already
parameterize), every `db.py` `_connect()` call paired with a
`finally: conn.close()`, `config.py` duplicate top-level constant
assignments (AST-checked, not just grepped), `TRACKED_TRADERS`/
`VIP_WALLET_EXPOSURE_CAP_USD` internal consistency, dashboard `tsc`/
`eslint` (both silent), `TODO`/`FIXME` markers (none), `wss_listener.py`/
`dashboard.py` runtime logs (no errors in the observation window).

## 38. Pool-refill bot detection (Rule 39 technical detail)

**`propose_pool_refill.py`'s `check_is_likely_bot(wallet_address)`:**
```python
try:
    response = run_bullpen_json(["polymarket", "wallet-stats", wallet_address], retries=2)
except Exception:
    return None
behavior = response.get("behavior_stats") or {}
if behavior.get("status") != "ok":
    return None
data = behavior.get("data") or {}
is_bot = data.get("is_likely_bot")
return is_bot if isinstance(is_bot, bool) else None
```
Note the args list does NOT include `--output json` — `run_bullpen_json()`
already appends that itself (confirmed by reading
`_run_bullpen_json_once()`: `["bullpen"] + args + ["--output", "json"]`).
The first version of this function included it anyway, and every call
failed with `exited 3: --output can only be provided once` — caught
because all 4 candidates unexpectedly came back `UNVERIFIED` on the first
live run, not by inspecting the code.

**`main()`'s screening loop:**
```python
candidate_pool = get_pool_refill_candidates(
    exclude, config.POOL_REFILL_MIN_COMPOSITE_SCORE,
    limit=gap * config.POOL_REFILL_CANDIDATE_FETCH_MULTIPLIER,
)
accepted, rejected_bots, unverified = [], [], []
for c in candidate_pool:
    if len(accepted) >= gap:
        break
    is_bot = check_is_likely_bot(c["wallet_address"])
    if is_bot is True:
        rejected_bots.append(c)
        continue
    c["bot_check"] = "not flagged" if is_bot is False else "UNVERIFIED (bullpen call failed/inconclusive)"
    accepted.append(c)
    if is_bot is None:
        unverified.append(c)
```
`is_bot is None` (inconclusive) is NOT treated the same as `is_bot is
False` (confirmed clean) — both let the candidate through, but only the
`False` case is silent; a `None` result is flagged `UNVERIFIED` in the
printed output and counted separately, so a human reviewing the list
knows which candidates still need a manual `wallet-stats` check.

**Verified live, both before and after the fix, against the real DB and
real `bullpen` CLI** (this script has no unit tests, matching
`reset_kill_switch.py`'s precedent — it's a one-shot operator tool, not
library code): before the `--output json` fix, all 4 original candidates
came back `UNVERIFIED`; after, expanding the buffer to
`gap * POOL_REFILL_CANDIDATE_FETCH_MULTIPLIER` (16 candidates) found
**16 of 16 confirmed bots**, including `0xc21ea96be762bb55041529af6e386e7c53b80215`
("JustCrazy") — the wallet Rule 27's TCA floor was built around.

## 39. Evidence-gathering replaces auto-filtering (Rule 40 technical detail)

**`propose_pool_refill.py`'s `fetch_recent_trades(wallet_address, limit=None)`:**
```python
response = run_bullpen_json(
    ["polymarket", "activity", "--address", wallet_address, "--limit", str(limit)], retries=2,
)
activities = response.get("activities") or []
return [a for a in activities if a.get("type") == "TRADE"]
```
`limit` defaults to `config.POOL_REFILL_ACTIVITY_SAMPLE_SIZE` (50) —
a quick-look sample, not a full history.

**`summarize_liquidity_farming_signal(trades)`** — pure, no I/O, returns
plain stats rather than a verdict:
```python
priced = [t for t in trades if isinstance(t.get("price"), (int, float))]
extreme = sum(1 for t in priced if t["price"] < 0.05 or t["price"] > 0.95)
sell_count = sum(1 for t in trades if t.get("side") == "SELL")
quote_counts = Counter((t.get("slug"), round(t["price"], 3)) for t in priced)
top_quote, top_quote_count = quote_counts.most_common(1)[0] if quote_counts else (None, 0)
```
`top_repeated_quote_count` — how many times the single most-common
`(market_slug, price)` pair recurs in the sample — is the strongest
individual signal found in practice: a real conviction bet doesn't need
the same exact quote filled 13-34 times in one market.

**`main()`'s per-candidate loop**: calls both `check_is_likely_bot()`
(kept, from Rule 39, now labeled explicitly as "one signal, not a
verdict") and the new activity-pattern summary, prints both, and warns
inline (`extreme_price_pct >= 50` or `top_repeated_quote_count >= 5`)
without ever removing a candidate from the printed list — the `gap`-
sized cap on candidates now limits `get_pool_refill_candidates()`'s own
`limit` param directly (no separate bot-filtering loop consuming extra
candidates from the buffer), since nothing is filtered out anymore.

**Verified live, same 4 real candidates, before and after**: all 4 now
print concrete extreme-price percentages (73.5-100%) and repeated-quote
counts (13-34x) instead of a bare `is_likely_bot: true` — same reject
outcome, but every number is independently checkable against
`bullpen polymarket activity --address <addr>` rather than trusting a
single opaque server-side flag.

## 40. Liquidity-farming hard gate, `scoreWallets.ts` (Rule 41 technical detail)

**`ScoringRules.liquidityFarming`** (rule_set v4):
```typescript
liquidityFarming: {
  minSampleSize: number;       // 20
  maxExtremePricePct: number;  // 0.5 (fraction, 0..1)
  minRepeatedQuoteCount: number; // 5
}
```

**`fetchRecentTrades(address)`** — new pass-2 call, genuinely distinct from
the existing `fetchActivityBounds` (`wallet-stats --section activity`,
timestamps/count only). **Direct Polymarket Data API, not bullpen** (moved
2026-07-27, same day as the gate itself — see RISK_MANAGEMENT.md Rule 41's
addendum for the full investigation into how much of the TS pipeline could
drop bullpen):
```typescript
import { fetchOnePage } from "./polymarketDataApi";
// ...
return await fetchOnePage(address, RECENT_TRADES_SAMPLE_SIZE, 0);
```
Deliberately `fetchOnePage` (a single bounded fetch), not the existing
`fetchWalletTrades` (which auto-paginates until a SHORT page — asking it
for "50" on a wallet with >=50 trades would keep fetching well past the
intended sample size, silently multiplying request volume across a scan of
hundreds of wallets). `RECENT_TRADES_SAMPLE_SIZE = 50`, matching
`propose_pool_refill.py`'s `POOL_REFILL_ACTIVITY_SAMPLE_SIZE`. On failure,
returns `[]` (caught, logged via `console.warn`, not thrown) — an empty
sample makes `computeLiquidityFarmingSignal` return `null`, which is
exactly what makes the gate fail open. `RecentTradeRecord`'s own type
narrowed to just `{ price?, slug? }` (dropped `type`/`side`/`size`, unused
by `computeLiquidityFarmingSignal`) — kept deliberately decoupled from
`polymarketDataApi.ts`'s full `RawActivityRecord` so the pure scoring
function isn't coupled to that module's exact fetch shape.

**`computeLiquidityFarmingSignal(trades)`** — pure, TS mirror of
`propose_pool_refill.py`'s `summarize_liquidity_farming_signal`:
```typescript
const priced = trades.filter((t) => typeof t.price === "number");
const extreme = priced.filter((t) => t.price! < 0.05 || t.price! > 0.95).length;
const quoteCounts = new Map<string, number>();
for (const t of priced) {
  const key = `${t.slug ?? ""}|${t.price!.toFixed(3)}`;
  quoteCounts.set(key, (quoteCounts.get(key) ?? 0) + 1);
}
const topRepeatedQuoteCount = quoteCounts.size > 0 ? Math.max(...quoteCounts.values()) : 0;
```

**`checkLiquidityFarmingGate(r, rules)`** — mirrors `checkToxicFlowGate`'s
exact `{status: "ignore", reason} | null` shape:
```typescript
const signal = r.liquidityFarmingSignal;
if (!signal || signal.sampleSize < rules.liquidityFarming.minSampleSize) return null;
const extremeGated = signal.extremePricePct >= rules.liquidityFarming.maxExtremePricePct;
const repeatedQuoteGated = signal.topRepeatedQuoteCount >= rules.liquidityFarming.minRepeatedQuoteCount;
if (!extremeGated || !repeatedQuoteGated) return null;
return { status: "ignore", reason: /* ... */ };
```
Wired into `finalizeAndWrite` as a third gate stage, between the recency
gate and `decideStatus`, in the exact same force-ignore-and-`continue`
loop pattern as the other two.

**Verified live** (rule_set auto-bumped v3 -> v4, no manual DB migration —
`getActiveRuleSet()`'s existing version-mismatch auto-deactivate/insert
logic handled it): re-ran `scan:wallets` against the real DB. Hit a real,
unrelated environment issue mid-run — 6 of 191 pass-2 candidates got
`bullpen polymarket activity ... exited 1: Unexpected response from
server — your CLI may be out of date`, confirming the fail-open design
worked as intended (those 6 passed through un-gated rather than crashing
the run). A second, separate issue surfaced in the same run —
`SQLITE_BUSY: database is locked`, from `bot.py` running live and holding
the DB — unrelated to this gate's logic; fixed incidentally by `packages/
db/src/migrate.ts` already enabling WAL mode + busy_timeout on migrate.

**Tests:** `scoreWallets.test.ts` — `describe("computeLiquidityFarmingSignal")`
(empty sample -> null, the exact live gloriafoster signature reproduced
synthetically, a diversified-longshots sample NOT flagged, unpriced trades
excluded from the percentage but still counted in sample size) and
`describe("checkLiquidityFarmingGate")` (gates the confirmed pattern
regardless of compositeScore, fails open on null signal, fails open below
minSampleSize, requires BOTH conditions — neither alone gates — and gates
exactly at the `>=` boundary on both). 195 TS tests passing total.

**v5 fix, same day: added a sell-side-majority check after a confirmed
false positive.** `computeLiquidityFarmingSignal` gained
`extremePriceSellPct`:
```typescript
const extremeTrades = priced.filter((t) => t.price! < 0.05 || t.price! > 0.95);
const extremeSellCount = extremeTrades.filter((t) => t.side === "SELL").length;
const extremePriceSellPct = extremeTrades.length > 0 ? extremeSellCount / extremeTrades.length : null;
```
`RecentTradeRecord` gained `side?: string` back (dropped in the v4 bullpen
migration since it looked unused — turned out to matter). `ScoringRules.
liquidityFarming` gained `minExtremePriceSellPct: 0.5`. `checkLiquidityFarmingGate`
now requires all three: `extremeGated && repeatedQuoteGated && sellGated`,
where `sellGated = signal.extremePriceSellPct !== null && signal.extremePriceSellPct
>= rules.liquidityFarming.minExtremePriceSellPct` — explicit null-check
rather than assuming `extremeGated` guarantees a non-null value, matching
this gate's existing fail-open-on-missing-data posture. Rule_set bumped
v4 -> v5 (same auto-deactivate/insert mechanism as v3->v4, no manual DB
work). Full incident writeup (the real quant-generalist-2 numbers, root
cause, why 0.5 isn't a knife-edge choice): RISK_MANAGEMENT.md Rule 41's
second addendum.

**Tests:** 5 new/updated — the exact quant-generalist-2 shape (44 buy/
2 sell within 41 extreme-price trades sharing one repeated quote)
reproduced synthetically and confirmed NOT gated by
`checkLiquidityFarmingGate`, a null-`extremePriceSellPct` case confirmed
to fail open, and the pre-existing "does NOT gate on X alone" tests
extended so all three conditions are isolated individually rather than
just the original two. 200 TS tests passing (was 195).

## 41. "Zombie position" dump exit (Rule 41 technical detail)

**`paper_trade.last_priced_at`** (new nullable integer column, drizzle
migration `0015_abandoned_mother_askani.sql`): unix seconds of the last
successful `check_trailing_take_profit` price read for that position. Set
in `bot.py` right where `prices_by_key[key] = indicative_price` already
happens:
```python
pos["last_priced_at"] = time.time()
```
and on first creation of a position (`process_trade`'s default dict):
`{"shares": 0.0, ..., "last_priced_at": time.time()}` — a brand-new
position starts its clock at open time, not at `None`. `db.load_state()`
falls back to `opened_at` when the column is `NULL` (a pre-migration row,
or one genuinely never successfully priced) — never treats missing data as
"infinitely stale."

**`ignore_staleness` param** threaded through, default `False` everywhere:
`polymarket_simulator.fetch_order_book(token_id, ..., ignore_staleness=False)`
-> `fetch_order_book_for_outcome(...)` -> `bot.get_market_prices(...,
ignore_staleness=False)`. Only `sweep_zombie_positions` ever passes `True`.

**`sweep_zombie_positions(positions, trader_performance, muted_traders,
tracked_by_lower)`** — new, own interval
(`config.ZOMBIE_SWEEP_INTERVAL_SECONDS = 21600`, 6h), wired into `main()`
alongside the closeout sweep:
```python
if not SHUTDOWN_REQUESTED and now - last_zombie_sweep >= config.ZOMBIE_SWEEP_INTERVAL_SECONDS:
    last_zombie_sweep = now
    sweep_zombie_positions(positions, trader_performance, muted_traders, tracked_by_lower)
```
For each position with `now - pos["last_priced_at"] >=
config.ZOMBIE_POSITION_THRESHOLD_SECONDS` (86400, 24h): calls
`get_market_prices(market_slug, outcome, ignore_staleness=True)`. If that
still returns no price (market lookup itself broken — delisted/renamed),
logs a throttled `zombie_position_unresolvable` error
(`_zombie_unresolvable_failures`, an in-memory per-key counter, exact same
shape/reasoning as `_closeout_fetch_failures`: first failure logs, repeats
suppressed until every `config.ZOMBIE_UNRESOLVABLE_LOG_EVERY`th (4, ~daily
at this sweep interval)). If a price IS available:
`config.ENABLE_ZOMBIE_POSITION_DUMP` gates whether
`close_position_zombie_dump` actually runs, or a `zombie_position_
would_dump` dry-run line is logged instead.

**`close_position_zombie_dump`** — deliberately NOT a parameterized variant
of `close_position_trailing_tp`: skips `check_spread_tolerance` entirely
(a wide spread is expected/accepted here — gating the escape hatch on the
same check that let the position get stuck would defeat the point) and
skips patient-exit-pegging. Live-mode price floor uses
`config.ZOMBIE_EXIT_MAX_SLIPPAGE = 0.25`, NOT `SLIPPAGE_TOLERANCE`:
```python
min_price = round(indicative_price * (1 - config.ZOMBIE_EXIT_MAX_SLIPPAGE), 4)
response = require_filled(run_bullpen_json([
    "polymarket", "sell", market_slug, outcome, str(shares_closed),
    "--min-price", str(min_price), "--yes",
]), "live zombie-dump sell")
```
On failure/timeout, the position is left untouched in `positions` (not
deleted) so the next 6-hour sweep retries — same pattern as
`close_position_trailing_tp`. New event types: `paper_sell_zombie_dump` /
`live_sell_zombie_dump`, `zombie_position_would_dump`, plus the existing
generic `error` type carries `zombie_position_unresolvable`'s message.

**Rollout**: `config.ENABLE_ZOMBIE_POSITION_DUMP = False` by default —
confirmed with Joey (2026-07-27) as the deliberate first step: watch
`zombie_position_would_dump` lines in production before flipping the flag
on. `ZOMBIE_POSITION_THRESHOLD_SECONDS` (24h) and `ZOMBIE_EXIT_MAX_SLIPPAGE`
(25%) were also both explicitly confirmed rather than picked unilaterally.

**Tests:** `test_bot_risk_checks.py` (`TestSweepZombiePositions` — below/at
threshold, missing `last_priced_at`, dry-run vs. flag-on dump call,
unresolvable throttle set/clear; `TestClosePositionZombieDump` — paper-mode
close math, spread check skipped, slippage floor uses
`ZOMBIE_EXIT_MAX_SLIPPAGE` not `SLIPPAGE_TOLERANCE`, failed sell leaves the
position open) and `test_polymarket_simulator.py`
(`ignore_staleness=True` bypasses the raise; default `False` still raises
for existing callers). **384 Python tests passing** (371 + 13 new).

## 42. `PositionTracker` (Rule 42 technical detail)

**`applyTrade(state, trade)`** — the weighted-average update, deliberately
fed `usdcSize`/`size` directly rather than computing a fee:
```typescript
if (trade.side === "BUY") {
  const newShares = prevShares + trade.size;
  const newCost = prevCost + trade.usdcSize;   // usdcSize is ALREADY fee-inclusive
  // avgEntryPrice = newCost / newShares
}
```
On SELL, a sale larger than tracked shares is clamped rather than allowed
to go negative:
```typescript
const soldShares = Math.min(trade.size, existing.shares);
const fractionSold = soldShares / existing.shares;
const proceeds = trade.usdcSize * (soldShares / trade.size);  // only the
const realizedSlice = proceeds - existing.costBasisUsd * fractionSold;  // clamped portion
```
Idempotency uses the identical composite trade-id
`polymarket_data_api.py` already established
(`tx_hash:asset:side:timestamp`) — a `Set<string>` per wallet state, kept
the same across languages on purpose rather than inventing a second
uniqueness scheme.

**`checkMarketResolution(marketSlug)`** — two-step Gamma lookup, mirroring
`polymarket_simulator.py`'s `fetch_market_info`:
```typescript
const plain = await fetchGammaMarket(marketSlug, false);
if (plain) return parseGammaMarket(plain);
const closed = await fetchGammaMarket(marketSlug, true);  // &closed=true
if (closed) return parseGammaMarket(closed);
return { status: "delisted" };
```
`parseGammaMarket` reads `closed`/`outcomes`/`outcomePrices` directly off
the market object — verified live (2026-07-27) against
`wnba-tor-atl-2026-06-22`: `closed=true`, `outcomes=["Toronto Tempo",
"Atlanta Dream"]`, `outcomePrices=["0","1"]` (same index order, confirmed
by the same pattern already relied on for `outcomes`/`clobTokenIds` in
`polymarket_simulator.py`), `umaResolutionStatus="resolved"`.

**`resolveOpenPositions(state)`** — scoped to currently-held markets only
(never a bulk sweep), and a transient fetch error explicitly does NOT
count as delisted:
```typescript
try {
  resolution = await checkMarketResolution(pos.marketSlug);
} catch (err) {
  console.warn(...);
  continue; // retry next update — NOT a delisted verdict
}
```
A genuine `{status: "delisted"}` closes the position with
`realizedPnlUsd: null` and increments a per-market throttle counter
(`unresolvableMarkets: Map<string, number>`) — first failure warns, every
`UNRESOLVABLE_LOG_EVERY=4`th repeat warns again, identical shape/reasoning
to `bot.py`'s `_zombie_unresolvable_failures`/`_closeout_fetch_failures`.

**`computeWalletMetrics(state)`** filters `closedPositions` to
`realizedPnlUsd !== null` before computing anything — a delisted close's
`null` is never coerced into `0` and averaged into win rate or total PnL.
`winRate` itself is `null`, not `0`, when there are zero closed positions
yet — "unknown" and "confirmed 0%" are different claims.

**`updateWalletState(state)`** sorts trades ascending before applying —
confirmed live that `data-api.polymarket.com/activity` returns
newest-first, so trusting fetch order would apply a SELL before the BUY
it's closing out, corrupting the weighted-average math silently.

**`reconcile:position-tracker`** reads the ground-truth wallet list from
`bot_risk_state.tracked_traders` (Drizzle: `db.select().from(botRiskState)
.where(eq(botRiskState.key, "tracked_traders"))`) rather than a hardcoded
address list, so it never drifts from whatever `bot.py` is actually
running. Compares against `wallet_profile.pnlAllTime` per wallet, flags
(doesn't fail) any diff over `PNL_DIFF_FLAG_THRESHOLD_PCT=10` for manual
read, and separately surfaces a truncation warning whenever
`tradeCountAllTime > 5000` (the `fetchWalletTrades` page cap) so an
incomplete reconstruction's gap isn't mistaken for a math bug.

**Tests:** `positionTracker.test.ts` — `describe("applyTrade")` (single
BUY cost basis, two-BUY weighted average, idempotent re-apply, partial
sell's realized slice, full sell's `sold_out` close, a no-op sell against
no tracked position, a clamped over-sized sell), `describe("checkMarketResolution")`
(open, resolved-on-first-try, resolved-only-after-retry, delisted-only-
after-both-empty), `describe("resolveOpenPositions")` (stays open,
resolves with correct PnL, delists with `null` PnL and throttle
increment, transient failure leaves the position open),
`describe("computeWalletMetrics")` (null win rate on zero closes,
delisted exclusion), and `describe("updateWalletState")` (chronological
application despite newest-first fetch order, `lastFetchedAt` set for the
next delta).

**Concurrency fix (Rule 42 addendum, same day)**: `resolveOpenPositions`
fans resolution checks out with `mapWithConcurrency` instead of one
sequential `await` per held market:
```typescript
const uniqueSlugs = Array.from(new Set(Array.from(state.openPositions.values()).map((p) => p.marketSlug)));
const resolutions = await mapWithConcurrency(uniqueSlugs, RESOLUTION_CHECK_CONCURRENCY, async (slug) => {
  try {
    return { slug, resolution: await checkMarketResolution(slug) };
  } catch (err) {
    return { slug, error: (err as Error).message };
  }
});
```
`RESOLUTION_CHECK_CONCURRENCY = 5`, matching `scoreWallets.ts`'s existing
pass-1/pass-2 concurrency rather than a freshly-tuned number. Deduped by
market slug (a `Set`) before fanning out — a wallet holding both outcomes
of one market previously triggered two identical Gamma calls. The
per-position mutation loop that follows (delete from `openPositions`,
push to `closedPositions`) still runs single-threaded, reading from a
plain `Map<string, {resolution} | {error}>` built after the fan-out
completes — no concurrent writers ever touch `state` itself. **Tests:**
2 new — same-market/different-outcome dedup (`fetchMock` called once for
two positions), and two DIFFERENT markets resolving to different correct
outcomes under the same concurrent batch (guards against a fan-out bug
that resolves everything to whichever market's response happened to
arrive first). **221 TS tests passing** (was 219).

**Outcome-name normalization fix (Rule 42 addendum, same day)**:
```typescript
function normalizeOutcomeName(name: string): string {
  return name.replace(/['‘’ʼ`]/g, "");
}
// ...
const normalizedTarget = normalizeOutcomeName(pos.outcome);
const outcomeIndex = resolution.outcomes.findIndex((o) => normalizeOutcomeName(o) === normalizedTarget);
```
Real live finding: `data-api.polymarket.com/activity`'s `outcome` field
and `gamma-api.polymarket.com/markets`' `outcomes` array disagree on
apostrophes for the same real-world name (confirmed on 6+ occurrences in
one wallet's history — "St Josephs FC" vs. "St Joseph's FC", "OHiggins
FC" vs. "O'Higgins FC", "Côte dIvoire" vs. "Côte d'Ivoire"). Deliberately
narrow — only apostrophe-like characters are stripped, nothing is
lowercased or otherwise fuzzy-matched, since over-normalizing risks
silently conflating two genuinely different outcome names. **Tests:** 1
new — the exact "St Josephs FC" case reproduced against a mocked
`closed:true` Gamma response with the apostrophe intact, confirmed
resolving correctly. **222 TS tests passing** (was 221).

**Reconciliation windowing fix (Rule 42 addendum, same day)**:
```typescript
const windowStartEpochSeconds = Math.floor(Date.now() / 1000) - RECONCILIATION_WINDOW_DAYS * 86400;
// ...
const state = newWalletPositionState(address);
state.lastFetchedAt = windowStartEpochSeconds; // bounds fetchWalletTrades' startEpochSeconds
await updateWalletState(state);
// ...
const bullpenPnl30d = profile[0]?.pnl30d ?? null;  // was profile[0]?.pnlAllTime
```
`RECONCILIATION_WINDOW_DAYS = 30` — reuses `WalletPositionState.
lastFetchedAt` (already built for incremental delta updates) as a
one-shot lower bound instead of leaving it `undefined` (full-history
attempt, capped at 5000 trades). Diffs against `wallet_profile.pnl30d`
(same direct bullpen pass-through provenance as `pnlAllTime`) rather than
lifetime PnL. 30 days specifically because `wallet_profile` has that
exact field to diff against — no `pnl90d` equivalent exists, so 30 was
picked for a clean apples-to-apples comparison, not because it's the
"right" window on principle. See RISK_MANAGEMENT.md Rule 42's addendum
for the diagnostic pattern that motivated this (14/17 wallets with large
diffs all hit the 5000-trade cap; the 3 clean matches never did).
No new tests — `reconcile:position-tracker` stays in the live-
integration-script category (same as `propose_pool_refill.py`), verified
by re-running against the real 17 wallets, not unit-tested.

## 43. `computePositionConfidence` (Rule 42 addendum, `scoreWallets.ts`)

```typescript
export function computePositionConfidence(closedCount: number, openCount: number, rules: ScoringRules): number | null {
  const total = closedCount + openCount;
  if (total === 0) return null;
  const completeness = closedCount / total;
  const closedSampleConfidence = clamp(closedCount / rules.closedPositionConfidenceFloor, 0, 1);
  return completeness * closedSampleConfidence;
}
```
`ScoringRules.closedPositionConfidenceFloor: 20` (v6). Not called from
`computeCompositeScore` or anywhere in the pass-1/pass-2 fetch loop yet —
`PositionTracker` has no call site in `scoreWallets.ts` at all currently;
this function exists so the integration, when it happens, doesn't also
need to design the discount math from scratch. Confirmed via `getActiveRuleSet()`'s
existing version-mismatch auto-deactivate/insert logic that the v5 -> v6
bump activates cleanly against the real DB (re-ran `scan:wallets` live).

**Tests:** `scoreWallets.test.ts` — `describe("computePositionConfidence")`:
null (not 0) on zero position data, 0 (not null) on activity with zero
closes, the real yield-farmer-1 shape (212 closed / 226 open) scoring
below 0.5, a 2-closed/0-open wallet scoring low despite 100% completeness
(sample-size gate working independently of the ratio), a well-sampled
mostly-closed wallet scoring above 0.9, and the ramp capping at 1.0 past
the floor (same shape as `computeSampleConfidence`). **228 TS tests
passing** (was 222).

## 44. Capital multiplier + tiered scoring (Rule 44 technical detail)

**`computeCapitalMultiplier`**:
```typescript
export function computeCapitalMultiplier(sharpeProxy: number, rules: ScoringRules): number {
  const saturationFraction = clamp(sharpeProxy / rules.capitalMultiplier.saturation, 0, 1);
  return 1 + saturationFraction * (rules.capitalMultiplier.max - 1);
}
```
Fed `SeriesAnalysis.sharpeProxy` — the raw `meanDelta / stdevDelta`
`analyzePnlSeries` already computes internally, now returned rather than
discarded (only the saturated `consistencyScore = clamp(sharpeProxy /
sharpeSaturation, 0, 1)` was exposed before). `rules.capitalMultiplier =
{ saturation: 0.35, max: 2.0 }` (v7). Wired into `runPass2` right where
`analyzePnlSeries` is already called — no new fetch, reuses data already
paid for:
```typescript
const { ..., sharpeProxy } = analyzePnlSeries(series, rules);
const capitalMultiplier = computeCapitalMultiplier(sharpeProxy, rules);
```
Threaded onto `Pass2Result.capitalMultiplier` and every `upsertWalletProfile`
call site (all 5 — toxic-flow/recency/liquidity-farming gate rejections,
the final decided loop, and pass-1 rejections pass `capitalMultiplier: null`
since pass 1 never reaches `analyzePnlSeries`).

**`db.get_wallet_composite_scores()`** (Python) extended to select and
surface the new column:
```python
"SELECT wallet_address, composite_score, win_rate, trade_count_all_time, "
"capital_multiplier, category_scores_json FROM wallet_profile"
# ...
result[row["wallet_address"].lower()] = {
    "composite": row["composite_score"],
    "composite_win_rate": row["win_rate"],
    "composite_trade_count": row["trade_count_all_time"],
    "capital_multiplier": row["capital_multiplier"],
    "categories": categories,
}
```

**`bot.compute_trade_size_usd()`** — the Kelly math is byte-for-byte
unchanged; only the range it maps into is stretched:
```python
capital_multiplier = wallet_score_entry.get("capital_multiplier") or 1.0
min_trade_usd = config.MIN_TRADE_USD * capital_multiplier
max_trade_usd = config.MAX_TRADE_USD * capital_multiplier
clamped = max(0.0, min(1.0, half_kelly_fraction))
return min_trade_usd + (max_trade_usd - min_trade_usd) * clamped
```
`config.BASE_TRADE_USD` (the `wallet_score_entry is None` / no-win-rate-
evidence fallback) is deliberately returned BEFORE this scaling — a
multiplier never inflates the no-evidence default.

**Tiered scoring**: `getTrackedWalletAddresses()` (mirrors
`reconcilePositionTracker.ts`'s identical helper — same query, same
`bot_risk_state.tracked_traders` source, not shared into a common module
tonight for time reasons but doing the exact same thing) is fetched once
per `main()` run and threaded through `runPass1`/`finalizeAndWrite`/
`upsertWalletProfile` as `trackedAddresses`. `upsertWalletProfile` derives
`tier` and `nextRescoreDueAt` internally on every write:
```typescript
const tier = deriveTier(args.walletAddress, args.status, args.trackedAddresses);
const nextRescoreDueAt = computeNextRescoreDueAt(tier, args.rules, now);
```
`filterDueForRescore(candidates)` runs once in `main()`, right after
`getCandidateWallets()`, and drops anything not yet due — a single query
joining the candidate list against `wallet_profile.nextRescoreDueAt`,
treating both "no row at all" and "row exists but `nextRescoreDueAt` is
NULL or in the past" as due.

**Verified live** (rule_set auto-bumped v6 -> v7, same auto-deactivate/
insert mechanism as every prior version bump): re-ran `scan:wallets`.
First run: 692 of 692 candidates due — expected, since `nextRescoreDueAt`
never existed as a concept before this exact code change, so every row is
legitimately "never scored under this system." Second, immediate re-run:
**10 of 692 due (98.6% reduction)**, completing in seconds — the
self-throttle confirmed working on real data, not just unit tests.

**Tests:** 11 new (`scoreWallets.test.ts`), 4 new (`test_bot_risk_checks.py`)
— see RISK_MANAGEMENT.md Rule 44 for the full list. **239 TS / 388 Python
tests passing.**

## 45. Personal Grafana dashboard (Rule 45 technical detail)

**`risk_manager.py`** — the refactor, byte-for-byte behavior-preserving:
```python
def compute_unrealized_pnl(positions, prices_by_key):
    unrealized = 0.0
    for key, pos in positions.items():
        price = prices_by_key.get(key)
        if price is None or price <= 0:
            continue
        unrealized += pos.get("shares", 0.0) * price - pos.get("cost_basis_usd", 0.0)
    return unrealized

def compute_equity(positions, prices_by_key, realized_pnl):
    return config.PAPER_BANKROLL_USD + realized_pnl + compute_unrealized_pnl(positions, prices_by_key)

def compute_equity_breakdown(positions, prices_by_key, realized_pnl):
    unrealized_pnl = compute_unrealized_pnl(positions, prices_by_key)
    deployed_cost_basis = sum(pos.get("cost_basis_usd", 0.0) for pos in positions.values())
    total_cash = config.PAPER_BANKROLL_USD + realized_pnl - deployed_cost_basis
    total_equity = config.PAPER_BANKROLL_USD + realized_pnl + unrealized_pnl
    return {"total_equity": total_equity, "total_cash": total_cash, "total_unrealized_pnl": unrealized_pnl}
```

**`db.py`** — the shared event-type constant (the zombie-dump bug fix
lives here) and the two new functions:
```python
_REALIZED_PNL_EVENT_TYPES = (
    "paper_sell", "live_sell", "paper_sell_trailing_tp", "live_sell_trailing_tp",
    "paper_sell_zombie_dump", "live_sell_zombie_dump", "position_resolved",
)

def realized_pnl_today(now=None):
    now = now or datetime.now(timezone.utc)
    start_of_day = int(datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp())
    # ... same SUM(json_extract(...)) query as realized_pnl_total(), + "AND timestamp >= ?"

def has_snapshot_for_today(now=None):
    # SELECT 1 FROM daily_portfolio_snapshots WHERE date = ? (today's UTC 'YYYY-MM-DD')

def record_daily_snapshot(total_equity, total_cash, total_unrealized_pnl,
                           realized_pnl_today, active_traders_followed, now=None):
    # INSERT ... ON CONFLICT(date) DO UPDATE SET ... -- idempotent upsert by UTC date
```

**`bot.py`** — `maybe_snapshot_daily_portfolio(positions, prices_by_key,
tracked_traders, muted_traders)`, called from `main()`'s TTP-sweep block
right after the kill-switch equity evaluation (same `prices_by_key`,
same `if prices_by_key is not None` guard), wrapped in its own
try/except so a snapshot failure can never take down the poll loop:
```python
now = datetime.now(timezone.utc)
if now.hour < config.DAILY_SNAPSHOT_TRIGGER_HOUR_UTC:
    return
if has_snapshot_for_today(now=now):
    return
breakdown = risk_manager.compute_equity_breakdown(positions, prices_by_key, realized_pnl_total())
active_traders_followed = len(tracked_traders) - len(muted_traders)
record_daily_snapshot(..., now=now)
```

**Schema** (`packages/db/src/schema.ts`, migration `0017_magical_turbo.sql`):
`daily_portfolio_snapshots(date TEXT PRIMARY KEY, snapshot_at INTEGER,
total_equity REAL, total_cash REAL, total_unrealized_pnl REAL,
realized_pnl_today REAL, active_traders_followed INTEGER)`.

**`docker-compose.grafana.yml`**: `grafana-oss:11.4.0`, port `3001:3000`
(3000 stays the Next.js dashboard), `GF_INSTALL_PLUGINS=frser-sqlite-datasource`,
`./data:/data:ro` (whole directory, not just `app.db` — WAL mode means
recent commits can sit in `app.db-wal` until checkpointed), a separate
named volume (`grafana-storage`) for Grafana's own config so dashboards
survive a container restart.

**Tests:** see RISK_MANAGEMENT.md Rule 45 for the full breakdown — 4 new
in `test_risk_manager.py`, 9 new in `test_db_daily_snapshot.py` (new
file), 5 new in `test_bot_risk_checks.py`. **404 Python tests passing**
(was 388). TS suite unaffected (239, unchanged) — this feature touches
no TypeScript.

## 46. Non-positive Kelly edge skips the copy (Rule 46 technical detail)

**`bot.py`, `compute_trade_size_usd()`** — one new early return, right
after `half_kelly_fraction` is computed and before `capital_multiplier`/
`min_trade_usd`/`max_trade_usd` are even looked at:
```python
half_kelly_fraction = kelly_fraction * config.KELLY_FRACTION_MULTIPLIER

if half_kelly_fraction <= 0:
    return 0.0

capital_multiplier = wallet_score_entry.get("capital_multiplier") or 1.0
min_trade_usd = config.MIN_TRADE_USD * capital_multiplier
max_trade_usd = config.MAX_TRADE_USD * capital_multiplier

clamped = min(1.0, half_kelly_fraction)  # was: max(0.0, min(1.0, half_kelly_fraction))
return min_trade_usd + (max_trade_usd - min_trade_usd) * clamped
```
The `sizing_tier="base"` path (no win-rate evidence anywhere) returns
`config.BASE_TRADE_USD` earlier in the function and never reaches this
code at all — unaffected.

**`bot.py`, `process_trade()`** — the new skip check sits right after the
`score_breakdown` snapshot dict is assembled (so the exact
`shrunk_win_rate`/`kelly_fraction` that triggered it are still captured
for `decision_journal`), and before Rule 29's `LIMIT_ORDER_TRACKED_
WALLETS` branch — a wallet on the limit-order pilot with a non-positive
edge now skips before a `pending_execution` row is ever created, instead
of resting a `target_usd=0` order:
```python
score_breakdown = {..., "trade_size_usd": trade_usd, ...}

if trade_usd <= 0:
    append_log({**base_event, "event_type": "skip_non_positive_kelly_edge",
                "reason": f"half-Kelly fraction <= 0 for {nickname} in "
                          f"category={category!r} (shrunk_win_rate={shrunk_win_rate}, "
                          f"kelly_fraction={kelly_fraction}) — no assumed edge, no trade",
                "score_breakdown": score_breakdown})
    return
```
`trade_usd <= 0` is a safe proxy for "`compute_trade_size_usd` decided to
skip": every other return path in that function (`BASE_TRADE_USD`, or a
positive-Kelly floor/ceiling value) is guaranteed `> 0` given the
existing `MIN_TRADE_USD`/`BASE_TRADE_USD` config invariants, so this
never accidentally catches a legitimate floor-sized trade.

**Distinct from `should_skip_category()` (Rule 19)**: that gate requires
a category `pnl_t_stat` more extreme than `-CATEGORY_SKIP_Z_CRITICAL` —
STATISTICALLY SIGNIFICANT harm — and runs *before* `compute_trade_size_
usd()` is even called, so it can skip without computing a size at all.
This new check is independent and strictly softer: any negative point
estimate, evaluated *after* sizing, on wallet/category combinations that
never cross Rule 19's stricter statistical bar (small or noisy category
samples especially — exactly where this gap showed up live).

**Tests:** RISK_MANAGEMENT.md Rule 46 has the full breakdown — 5 existing
`TestComputeTradeSizeUsd` tests updated (floor assertions → `0.0`
assertions), 1 new `TestProcessTradeScoreSnapshot` integration test
(`test_non_positive_kelly_edge_skips_the_copy_entirely`). **405 Python
tests passing** (was 404). No TS changes.

## 47. Depth-Aware Trade Sizing (Rule 47 technical detail)

**`risk_manager.py`, new pure function:**
```python
def depth_capped_trade_size_usd(trade_size_usd, book_depth_usd, depth_fraction):
    if book_depth_usd is None:
        return trade_size_usd
    return min(trade_size_usd, book_depth_usd * depth_fraction)
```
`None` is a distinct, deliberate case from a real `0.0` depth reading —
the former means "couldn't find out, don't guess," the latter means "the
book really is empty, and 0 is the correct answer." Conflating them would
either silently disable the clamp on a real zero-liquidity book, or wrongly
zero out a trade just because a fetch failed.

**`bot.py`, new `fetch_book_depth_usd(market_slug, outcome)`** — same
shape and fail-open posture as the existing `get_market_ask_price()`/
`get_market_bid_ask()` above it: catches any exception from
`polymarket_simulator.fetch_order_book_for_outcome()` and returns `None`
rather than propagating, sums `price*size` across every visible ask level.

**`bot.py`, `process_trade()`** — wired in immediately after `trade_usd`
is computed, before the score-breakdown snapshot:
```python
trade_usd = compute_trade_size_usd(wallet_score_entry, price, category)

if trade_usd > 0:
    book_depth_usd = fetch_book_depth_usd(market_slug, outcome)
    depth_capped_usd = risk_manager.depth_capped_trade_size_usd(
        trade_usd, book_depth_usd, config.TRADE_SIZE_DEPTH_FRACTION
    )
    if depth_capped_usd < trade_usd:
        append_log({**base_event, "event_type": "depth_cap_would_apply", ...})
        if config.ENABLE_DEPTH_AWARE_TRADE_SIZING:
            trade_usd = depth_capped_usd
```
Skipped entirely when `trade_usd <= 0` already (Rule 46) — no point
spending a network call sizing a copy about to be skipped anyway. The
`depth_cap_would_apply` log only fires when the clamp would actually
bind (not on every trade), so normal, well-liquidated trades produce zero
extra log volume.

**Distinct from `MAX_EVENT_EXPOSURE_USD`/`MAX_WALLET_EXPOSURE_USD`
(`risk_manager.check_buy()`)**: those are portfolio-level aggregates —
total dollars deployed across many positions for one event or wallet —
computed and enforced entirely independently of this. This clamp acts
purely on the single trade's own size, against the single market's own
depth, and runs earlier in `process_trade()` than `check_buy()` does
(`check_buy()` is called from inside `_execute_buy()`, downstream of
sizing) — worth stating precisely since it was the source of a real
testing mistake (see RISK_MANAGEMENT.md Rule 47's own writeup): mocking
`check_buy()` to block a trade does NOT prevent this depth fetch from
running, because sizing happens first regardless.

**Tests:** RISK_MANAGEMENT.md Rule 47 has the full breakdown, including
the real unmocked-network-call testing gap found and fixed while building
this. `TestDepthCappedTradeSizeUsd` (5, `test_risk_manager.py`),
`TestDepthAwareTradeSizing` (4, `test_bot_risk_checks.py`), plus 6
existing tests patched to mock the new fetch. **414 Python tests passing**
(was 405). `ENABLE_DEPTH_AWARE_TRADE_SIZING` defaults `False` — not live
until explicitly enabled after a `bot.py` restart.

## 48. `last_trade_price` type coercion (Rule 48 technical detail)

**`polymarket_simulator.py`, `fetch_order_book()`** — one line changed,
matching how `bids`/`asks` on the same response were already handled:
```python
# Before:
last_trade_price = data.get("last_trade_price")

# After:
last_trade_price_raw = data.get("last_trade_price")
last_trade_price = float(last_trade_price_raw) if last_trade_price_raw is not None else None
```
Confirmed live against 5 real open positions' actual order books before
writing this fix — every one returned `last_trade_price` as a JSON
string (`"0.999"`, `"0.008"`, `"0.001"`, ...), never already numeric.

**Why this specific field, not `bids`/`asks` too**: `bids`/`asks` were
ALREADY correctly cast (`float(level["price"])`) — this function's bug
was narrower than it first looked. Only the `last_trade_price` fallback
(reached in `bot.py`'s `get_market_prices()` when a book has no bids AND
no usable midpoint — i.e. a thin or empty book) carried the uncoerced
string through to a numeric comparison (`indicative <= 0`), which is
exactly the failure mode: illiquid, rarely-traded markets are precisely
where this fallback path fires, and precisely where a stale/older
`last_trade_price` from days-old activity is most likely to be present
at all.

**Tests:** RISK_MANAGEMENT.md Rule 48 has the full investigation —
root-caused by ruling out DB-level data corruption first (`typeof()`
queries against real `paper_trade` rows, both clean), then fetching real
live order books directly to inspect the actual API response shape,
rather than guessing from the stack trace alone. `test_surfaces_
last_trade_price_from_the_same_response` updated to mock the real string
format; new `test_last_trade_price_string_from_real_api_is_coerced_to_
float` pins the type (not just the value) down as a permanent regression
test. **430 Python tests passing** (was 429).

**Live impact**: this bug shipped silently and ran in production for
3+ days (200 real occurrences, oldest ~73 hours old at discovery) before
being found. Each occurrence aborted that entire TTP sweep cycle across
ALL open positions (not just the illiquid one that triggered it) and
skipped that cycle's kill-switch equity evaluation — the kill switch
itself was never permanently blinded (the next successful sweep still
catches a real breach), but its reaction time was worse than the
documented ~5-minute cadence on any cycle this fired. Needs a `bot.py`
restart to take effect.

---

## 49. `check_spread_tolerance()` migrated off bullpen (Rule 4 technical detail)

**`bot.py`, `check_spread_tolerance()`** — the live-order spread/liquidity
gate (RISK_MANAGEMENT.md Rule 4) called `bullpen polymarket preview` for
every live BUY/SELL; this was the last bullpen dependency in the live
order-decision path for market *data* (order execution itself is
unaffected, see below). Swapped for `polymarket_simulator.simulate_fill()`
— a direct, no-auth read of `clob.polymarket.com/book`, the same function
already proven in `measure_paper_shortfall()` since the 2026-07-22 cutover
(see `polymarket_simulator.py`'s own module docstring).

**Scoped deliberately, not a general "remove bullpen" pass**: prompted by
a request to migrate "every bullpen CLI call" to Gamma. Investigation
(dependency map, reported to Joey before any code changed) found: (1)
market/event resolution + end-date + closeout-sweep metadata reads were
already migrated to Gamma on 2026-07-28 (`b204a48`); (2) this spread/
liquidity check was the one remaining live, active, unmigrated *read*
call site — but it needs order-book depth, which lives on the CLOB API
(`clob.polymarket.com/book`), not Gamma (`gamma-api.polymarket.com`,
metadata-only, no order book at all); (3) roughly ten other call sites
(buy, sell, limit-sell, cancel, poll-order, closeout) are order
**execution**, not data, and stay on bullpen by design — Gamma/CLOB are
read-only public APIs with no order-placement or wallet-signing
capability, so "removing bullpen" there would mean building real key
custody into this app, directly contradicting §6's core premise. Joey
confirmed this narrower scope (spread check only) before implementation.

**Also fixed in passing**: the pre-migration branch for the liquidity-
warning case returned a bare 2-tuple (`return False, reason`) instead of
the function's actual 3-tuple contract (`ok, reason, executable_price`) —
every real call site unpacks three values, so this branch would have
raised `ValueError` on unpack the one time it actually fired. Never
observed in production (would need a live order hitting exactly this
branch), caught while rewriting the surrounding code, not from an
incident.

**Tests**: no prior test exercised `check_spread_tolerance()`'s internals
directly (existing tests only mocked the function as a black box at
higher-level call sites) — added `TestCheckSpreadTolerance` (5 new tests:
pass case, relative-vs-absolute-spread rejection, simulate_fill exception
fail-safe, empty book, insufficient-liquidity 3-tuple regression guard).
435 Python tests passing (was 430). **Needs a `bot.py` restart to take
effect** — not yet live as of this entry.

---

## 50. `would_have_passed_spread_gate` on every paper trade (Rule 4 addendum)

**Trigger**: a direct challenge that paper trading was "meaningless" since
`check_spread_tolerance()` (the live spread/liquidity gate) is skipped in
paper mode. The premise was wrong in a specific, checkable way —
`measure_paper_shortfall()` (paper-only) already reads the same real CLOB
book via `simulate_fill()` and has logged real spread/slippage/fees on
every paper trade since 2026-07-22 (`TestMeasurePaperShortfall`). What was
actually missing was an explicit recorded verdict on whether that same book
read would have PASSED the live gate — not the underlying data.

**Why not just gate paper trades too**: considered and explicitly declined.
Making paper mode reject trades the same way live does would silently
narrow all FUTURE paper stats to only "already passed" trades, breaking
comparability with every paper trade recorded before the change — the same
kind of discontinuity the existing "measurement only, never enforced"
design in `measure_paper_shortfall()` was built to avoid in the first
place. Flagged this trade-off to Joey (AskUserQuestion) before writing any
code; she chose the additive flag over gating.

**What shipped**: `check_spread_tolerance()`'s verdict logic (relative
spread vs. `config.SPREAD_TOLERANCE`, plus the insufficient-liquidity case)
was factored out into a shared `_evaluate_spread_gate(price, spread,
insufficient_liquidity)` helper, used by BOTH `check_spread_tolerance()`
(enforced) and `measure_paper_shortfall()` (recorded only) — so the two can
never silently drift onto different definitions of "would this book have
passed." `measure_paper_shortfall()` now sets `would_have_passed_spread_gate`
(bool) on every return path, including the `preview_unavailable` and
`no_executable_price` failure branches (both count as "would not have
passed," matching `check_spread_tolerance()`'s own fail-closed behavior) —
plus a `spread_gate_reason` string whenever the flag is `False`. Nothing
about the paper fill price, position ledger, or PnL changed.

**Tests**: added `TestMeasurePaperShortfallSpreadGateFlag` (5 new tests:
tight-spread pass, wide-relative-spread rejection using the same identical-
absolute-spread setup as `TestCheckSpreadTolerance`, insufficient-liquidity
rejection, preview-failure fail-safe, empty-book fail-safe). 446 Python
tests passing (was 441). **Needs a `bot.py` restart to take effect** — not
yet live as of this entry.

---

## 51. Per-wallet `bot_seen_trade` dedup (Rule 14 addendum, 2026-07-31)

**Trigger**: investigating why most paper trades were hitting
`preview_unavailable` (§50's context) surfaced a real, unrelated bug —
confirmed via Gamma (`&closed=true`) that the failing CLOB reads were for
markets that resolved WEEKS ago, all `detected_by='polling'`, clustered in
tight bursts within seconds of a `bot.py` restart.

**Root cause**: `bot_seen_trade` dedup was capped GLOBALLY at 2000 trade_ids
across ALL tracked wallets combined (`SEEN_TRADE_ID_CAP`, both the in-memory
`deque(maxlen=2000)` in `bot.py` and the persisted table's own prune query
in `db.py`). `config.DIRECT_API_PER_WALLET_LIMIT` always returns each
wallet's most recent 20 trades no matter how old — so a quiet wallet's old
trade_ids sit in the feed response forever. Within one continuous run this
never mattered (the in-memory `seen_set` only grew, nothing evicted it) —
but a busy wallet's volume could push a quiet wallet's trade_ids out of the
PERSISTED top-2000, and the next restart rebuilds `seen_set` from just that
truncated snapshot. One tracked wallet's month-old FIFA World Cup
group-stage trades alone accounted for 64 phantom `paper_buy` re-copies
across two restarts in a single session.

**Fix, in order**:
1. `packages/db/src/schema.ts` / migration `0018_spooky_ender_wiggin.sql` —
   `bot_seen_trade` gains a nullable `wallet_address` column (nullable:
   existing rows can't be backfilled, `trade_id` is `tx_hash:asset:side:
   timestamp` with no wallet baked in — grouped into a capped
   `__unknown__` bucket instead of left unbounded).
2. `config.SEEN_TRADE_IDS_PER_WALLET_CAP = 100` (5x headroom over
   `DIRECT_API_PER_WALLET_LIMIT`) replaces the old global 2000 cap.
3. `db.py`'s `load_state()`/`save_state()` — the flat trade_id list became
   a list of `{"trade_id", "wallet_address"}` dicts; both the load query
   and the prune `DELETE` are now `ROW_NUMBER() OVER (PARTITION BY
   COALESCE(wallet_address, '__unknown__') ORDER BY seen_at DESC, rowid
   DESC)`, capped per partition instead of one global `ORDER BY seen_at
   DESC LIMIT 2000`.
4. `bot.py`'s new `_mark_trade_seen(seen_by_wallet, seen_set,
   wallet_address, trade_id)` replaces the two `seen_ids.append()`/
   `seen_set.add()` call sites (bootstrap + main loop) — keeps the flat
   `seen_set` (the actual `tid not in seen_set` check every poll uses) in
   sync with each wallet's own bounded `deque`, evicting from `seen_set`
   exactly when a wallet's own deque evicts, not on any global schedule.

**Bug caught while writing tests, not shipped separately**: the first
version of the partitioned SQL had no tiebreaker for equal `seen_at`
values. `_now_ts()` is second-granularity, and a burst of trades from one
wallet lands in the same second easily (a unit test with 10 rapid inserts
reproduced it immediately) — without `rowid DESC` alongside `seen_at DESC`,
SQLite's `ROW_NUMBER()` tie order is unspecified, and the test showed it
keeping the OLDEST three rows instead of the newest three. Fixed in both
the load and prune queries before this shipped, not after.

**Tests**: `test_db_seen_trade.py` (4 new tests — busy-wallet-doesn't-evict-
quiet-wallet, per-wallet independent capping, `__unknown__` bucket capping,
wallet_address round-trip) and `TestMarkTradeSeen` in
`test_bot_risk_checks.py` (4 new tests — cross-wallet isolation, own-wallet
eviction order, case normalization, `None`-wallet bucketing). 454 Python
tests passing (was 446). **Needs BOTH a `bot.py` restart AND `pnpm run
db:migrate` applied wherever this deploys** — not yet live as of this
entry.

## 52. `_mark_trade_seen()` idempotency fix + resolved-market guard (Rule 14 addendum, 2026-07-31)

**Trigger**: user asked for a status check ("how is the bot running today");
found the drawdown kill switch latched (2026-07-30T23:56:50Z, equity
$4278.12 vs. peak $6783.03) on a book (`strategy="bot_filtered"`, 49 open
positions, $341 cost basis) far too small to produce that swing. Traced to
`bot_event_log`: `will-spain-win-the-2026-fifa-world-cup-963` (and 19 other
already-resolved markets) had the exact SAME `source_trade_id` re-processed
into a fresh `paper_buy` roughly hourly for 14+ hours — each immediately
closed out again by the next hourly closeout sweep with the identical
`pnl_usd`, on the live process §51's per-wallet dedup fix was already
running on. §51 closed the restart-time eviction gap; this is a DIFFERENT
gap, live within a single continuously-running process.

**Root cause**: `_mark_trade_seen()` was not idempotent. The main loop's
`new_trades = [t for t in trades if t.get("trade_id") not in seen_set]`
filter runs ONCE, before the per-trade loop — if the same trade_id is
present twice in one raw poll batch, both copies pass this filter (neither
is in `seen_set` yet when it runs), and the loop calls both
`_mark_trade_seen()` AND `process_trade()` twice for the same real trade.
The second `_mark_trade_seen()` call appended a SECOND copy of the same
trade_id into the wallet's bounded deque without checking whether it was
already there. When `SEEN_TRADE_IDS_PER_WALLET_CAP`'s rotation later
evicted the FIRST copy, `seen_set.discard(dq[0])` removed the trade_id from
`seen_set` globally — while the second, still-live copy remained deeper in
the deque, invisible to that discard. `seen_set` and the deque desync: the
trade_id reads as "unseen" again well before the wallet's own 100-slot cap
should have naturally forgotten it, and the exact same stale trade gets
copied again.

**Fix, in `bot.py`**:
1. `_mark_trade_seen()` — now a no-op (`return` immediately) if `trade_id`
   is already in `seen_set`, so a duplicate mark from any source can never
   create a second deque entry for the same id.
2. Main polling loop — added `if tid in seen_set: continue` immediately
   before marking/processing each trade in `new_trades`, closing the
   specific within-batch-duplicate route that produced the double-mark.
3. `_market_already_resolved(market_slug, risk_state)` (new) +
   `config.STALE_TRADE_RESOLUTION_CHECK_SECONDS = 3600` — belt-and-
   suspenders guard in `process_trade()`'s BUY branch, checked first
   (before the muted-trader/Shadow-Rehab path too): a BUY signal whose own
   `timestamp` is more than 1 hour old gets one live
   `polymarket_simulator.fetch_market_metadata()` +
   `_parse_market_resolution()` check; refuses to open a position if the
   market has already settled, independent of whatever dedup mechanism let
   the stale signal through. Result cached in
   `risk_state["resolved_markets"]` (in-memory, not persisted — a restart
   just re-pays one cheap fetch the next time, which should be rare) so a
   confirmed-resolved market never pays this cost twice; a fresh (<1h)
   signal — the normal case — never calls it at all. Fails OPEN on a fetch
   error (deliberately the opposite of `resolve_market_event()`'s
   fail-closed doctrine): this is a sanity check on an otherwise-legitimate
   signal, not itself a primary risk gate, so a transient network error
   must not block a genuine buy.

**Examined, not changed**: Shadow Rehab's `_execute_shadow_buy()` (Rule 37)
deliberately has no per-position buy cap — confirmed by design intent in
its own 2026-07-27 docstring, and confirmed the kill switch's
`compute_equity()` only ever reads `strategy="bot_filtered"` positions, so
this is NOT what corrupted the kill switch. It does inflate
`daily_portfolio_snapshots`'s dashboard figures (one muted wallet's shadow
position alone reached 2,364 buys / $11,820 phantom cost basis, of a
258-position / $121,619 total shadow book) — flagged, left for a separate
decision.

**Tests**: `TestMarkTradeSeen` (+2: marking an already-seen id is a no-op;
duplicate-mark doesn't desync `seen_set` from the deque — direct regression
for this incident), `TestTradeAgeSeconds` (+3), `TestMarketAlreadyResolved`
(+3), `TestProcessTradeResolvedMarketGuard` (+3) in
`test_bot_risk_checks.py`. 465 Python tests passing (was 454). **Needs a
`bot.py` restart to take effect — not yet live as of this entry.** The kill
switch stays latched pending this deploy; do not `reset_kill_switch.py`
before it's live.

## 53. Extreme-tail longshots excluded from `compute_unrealized_pnl()` (Rule 6 addendum, 2026-07-31)

**Trigger**: after §52's fix deployed and the kill switch was cleared with
equity_hwm reset to a verified $1869.67, it re-latched within about a
minute — equity $1869.67 vs. a peak of $4338.78, itself a corrupted
first-reading HWM from the very next equity evaluation after restart.
Setting `equity_hwm` explicitly to the verified value and restarting again
held only briefly: `equity_hwm` was found to have crept back up to
$6831.36 with zero kill-switch trigger logged in between — a single
equity reading spiked ~$4900-5000, ratcheted the HWM up silently, and left
it as a landmine for the next normal reading.

**Root cause**: several tracked wallets' open `bot_filtered` positions were
ultra-longshot 2028 US presidential-election bets (`will-josh-stein-win-
the-2028-us-presidential-election`, `will-tom-brady-win-the-2028-
republican-presidential-nomination`, and similar) — `avg_entry_price`
0.001-0.008, $5-10 cost basis, 2500-5000 shares each. A tiny dollar cost at
these prices implies a huge share count; these markets are thin/illiquid,
so a single bad or stale CLOB read on any one of them (e.g. misreading
close to $1.00 while the position is still nominally open, not yet caught
by the hourly closeout sweep) gets amplified by the share count into a
multi-thousand-dollar phantom swing in `compute_unrealized_pnl()`'s
portfolio-wide total.

**Fix, in `risk_manager.py`**: `compute_unrealized_pnl()` now carries any
position at cost (zero unrealized contribution) if its `avg_entry_price`
is within `config.EQUITY_MARK_MIN_ENTRY_PRICE = 0.02` of either $0 or $1 —
unconditionally, regardless of what price a given sweep reports, extending
the function's existing "no usable price -> carried at cost" conservatism
to "economically-tiny-cost-but-huge-share-count -> carried at cost" too.
Gated on `avg_entry_price` (fixed at buy time) rather than on whether a
specific reading looks suspicious, since the vulnerability is inherent to
the position's own economics, not to any one price fetch — deterministic,
no heuristic needed. 0.02 reuses the same threshold value as
`DEFAULT_TCA_MIN_ENTRY_PRICE` (Rule 27, `discoverCategorySpecialists.ts`)
— same judgment call, different subsystem, not re-derived independently.
Applies to both `compute_equity()` (the kill switch) and
`compute_equity_breakdown()` (the Grafana daily snapshot), since both
share this one function — fixes the dashboard's `daily_portfolio_snapshots`
distortion at the same time, not a separate change.

**Deployed**: pushed, pulled to EC2, `bot.py` restarted (final PID recorded
post-deploy), and `equity_hwm`/`kill_switch` reset one more time once the
fix was confirmed running with no new extreme-tail-driven spike across
several TTP sweep cycles.

**Tests**: `TestEquity` (+4: an extreme-tail longshot marked at a
would-be-$4990-swing price is carried at cost instead; the symmetric
high-side tail; a position just outside the 0.02 band is still marked
normally — confirming this isn't a blanket "small positions don't count"
rule; a legacy position missing `avg_entry_price` entirely falls back to
normal marking rather than being silently excluded). 469 Python tests
passing (was 465).

## 54. Phase 1 observability — Prometheus metrics + Telegram alerts (2026-07-31)

**Why**: tonight's entire kill-switch/equity incident (Sec.52-53) was only found because Joey asked
"how's the bot running today" — nothing surfaced it automatically. Built directly in response, as
Phase 1 of the 4-layer architecture roadmap she and Claude discussed the same session (plan file:
`~/.claude/plans/async-questing-oasis.md`, also noted in memory under
`career-positioning-and-quant-roadmap`).

**Resource constraint, checked live before installing anything**: the EC2 box has only 908MB total
RAM (already running `bot.py` + a Next.js dev server, 686MB swap already in use at the time of
checking) — Docker wasn't installed. Both containers below are memory-capped
(`mem_limit: 200m` each) and Prometheus retention is bounded (7 days / 256MB), specifically so this
observability layer can't itself destabilize the box it's monitoring.

**What ships**:
1. **`copybot_events_total{event_type=...}`** (Prometheus Counter) — incremented once inside
   `db.append_log()`, the single choke point every event this codebase logs already flows through
   (paper_buy, paper_sell, every `skip_*` reason, error, etc.) — one instrumentation site covers
   everything, not scattered counter calls at dozens of sites.
2. **`copybot_equity_usd` / `copybot_kill_switch_active` / `copybot_open_positions`** (Gauges) — set
   in `bot.py`'s existing kill-switch evaluation block (same ~5-min cadence as the kill switch
   itself), plus `copybot_kill_switch_active` seeded from the persisted `bot_risk_state` value at
   startup so a restart doesn't show a false "0" before the first sweep runs.
3. `bot.py` exposes these on `config.METRICS_PORT = 9100` via `prometheus_client.start_http_server()`
   — bound to localhost only (Docker's Prometheus reaches it via `host.docker.internal`, an
   `extra_hosts: host-gateway` entry required on native Linux Docker Engine, unlike Docker Desktop
   where it's automatic). A port-bind failure (e.g. the exact stray-duplicate-process failure mode
   from the 2026-07-29 outage) is caught and logged, never allowed to block real trading from
   starting.
4. **`telegram_alerts.py`** (new module, stdlib `http.client` only — same zero-third-party-HTTP-
   library convention as `polymarket_simulator.py`) — `send_telegram_alert(message)`, fails silently
   (logs, never raises) if disabled or `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` aren't set. Both env
   vars already existed in `.env.example` (added 2026-07-26 for a TS end-of-day report,
   `packages/shared/src/telegram.ts`, that was never actually built) — reused here, not
   re-invented; `.env.example`'s comment corrected to point at the real implementation.
   - Immediate alert, never throttled, on `risk_kill_switch_triggered` — rare (latched until a
     human clears it) and exactly what sat undiscovered for 17+ hours tonight.
   - Throttled alert (`config.TELEGRAM_ERROR_ALERT_THROTTLE_SECONDS = 300`, max one per 5 min) on
     `event_type="error"` — a burst of the same underlying failure must not flood Telegram the way
     it would flood a phone; suppressed-count folded into the next alert sent, not dropped.
   - Daily PnL summary, piggybacked onto `maybe_snapshot_daily_portfolio()`'s existing
     once-per-UTC-day trigger rather than a second schedule.
5. `monitoring/docker-compose.yml` + `monitoring/prometheus.yml` + Grafana datasource
   auto-provisioning (`monitoring/grafana-provisioning/`) — Prometheus v3.13.2, Grafana OSS v13.1.1
   (both version-verified live via GitHub releases before pinning, not guessed). Both ports bound
   to `127.0.0.1` only — no new EC2 security-group rule, Grafana accessed via
   `ssh -L 3001:localhost:3000 <host>` per Joey's explicit choice, same posture as every other
   access path since the 2026-07-29 leaked-key incident.

**Tests**: `test_telegram_alerts.py` (+6: disabled/missing-token/missing-chat-id no-ops, 2xx
success, non-2xx failure, network exception caught not raised), `test_db_telegram_alerts.py` (+5:
counter increment, kill-switch immediate alert, error-alert throttle first/suppressed/folded-count
behavior), plus `TestMaybeSnapshotDailyPortfolio` updated to mock the new Telegram call. 480 Python
tests passing (was 469). **Deployed to EC2 as part of the same push as Sec.53's fix** — `docker
compose -f monitoring/docker-compose.yml up -d` still needs to be run manually on the EC2 box
(Docker itself needs installing first), and Joey needs to create her own Telegram bot via
`@BotFather` and populate `.env` with the token/chat_id — neither of those two steps can be done
from this session.
