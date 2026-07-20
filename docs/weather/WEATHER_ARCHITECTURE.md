# Weather Bot — System Architecture

**Audience note:** zero prior context assumed, same standard as `docs/copy-trading/SYSTEM_ARCHITECTURE.md`.
**Status: early implementation.** Documentation came first (per an explicit requirement), and four
scripts now exist and are live-verified: `ingestMetar.ts`, `pruneHistorical.ts`,
`emergencyCloseoutGuard.ts`, `checkSettlementAgainstMetar.ts` — see §4 below for exactly what's
built versus still planned. If you're looking for the running trading bot, that's the Copy Bot
(`docs/copy-trading/SYSTEM_ARCHITECTURE.md`) — this document describes the newer, still-partial
Weather Bot.

## What this system will be

A second, fully isolated Polymarket arbitrage bot that trades weather markets — e.g. "Highest
temperature in Seoul on July 19?" — by comparing a modeled probability (built from real weather
forecasts and historical climatology) against the market's current implied price, and opening a
paper position when the model disagrees with the market by enough margin to matter. Same
paper-trading-only posture as the Copy Bot: no live funds, no private keys, ever (see
`docs/weather/WEATHER_RISK_MANAGEMENT.md` Rule 1).

## Why it's a separate system, not a Copy Bot feature

The Copy Bot follows *traders*; this system follows *weather*. There is no wallet to copy, no
trade feed to mirror — instead there's a predictive model competing against the market's own
pricing. Per `.claudeprompt`'s standing instruction, **these two domains must never mix**: separate
code (`packages/weather/`, never `packages/copy-trading/` or any Python Copy Bot file), separate
database tables (7 already reserved and unused: `weather_station`, `weather_historical_observation`,
`weather_forecast_snapshot`, `weather_market_mapping`, `weather_probability_estimate`,
`weather_position`, `weather_pnl_snapshot`, in `packages/db/src/schema.ts` lines 309-411), separate
docs (this file and its companion, not `docs/copy-trading/RISK_MANAGEMENT.md`/`docs/copy-trading/SAFETY.md`). The only
shared infrastructure is the physical SQLite file (`data/app.db`) and two already-shared
TypeScript packages (`packages/bullpen-client`, `packages/db`) — same sharing model the Copy Bot's
TS research layer already uses, not a new exception.

## The central design problem this architecture solves: basis risk

Polymarket's weather markets settle on **whole-degree-Celsius buckets** (a market for "22°C" pays
out only if the day's actual high resolves to exactly that bucket). That makes the *exact* number
used to settle the market load-bearing in a way a linear-payout market wouldn't be — a data source
that's "close enough" for a forecast is not necessarily close enough to trust for settlement.

This was tested directly, not assumed. A same-day reconciliation check (RKSI, Incheon Airport,
July 18 2026 — a complete day) compared NOAA's free public METAR feed against Wunderground's own
reported daily high/low for the identical station:

| | METAR (`aviationweather.gov`) | Wunderground | Result |
|---|---|---|---|
| High | 26°C | 80°F = 26.67°C | **Different whole-°C bucket (26 vs 27)** |
| Low | 21°C | 72°F = 22.22°C | **Different whole-°C bucket (21 vs 22)** |

Both readings ran consistently warmer on Wunderground — same direction, not noise. Root cause
unconfirmed (Wunderground doesn't publish its aggregation methodology), but the practical
conclusion was unambiguous: a free, ToS-clean source that *looks* like it should match the
settlement station (same airport, same ICAO code) does not reliably match closely enough for
whole-degree-bucket settlement. This finding is why the architecture below treats "the source used
to predict" and "the source used to settle" as two structurally different roles, never conflated —
see `docs/weather/WEATHER_RISK_MANAGEMENT.md` Rules 4-6 for the full reasoning and the rules that follow
from it.

---

## 1. Data flow

```
[Fast, frequent, PREDICTIVE sources: NOAA/NWS, Open-Meteo, METAR]
        │  each normalizes to °F/inches at ingestion, preserves raw_json verbatim
        ▼
weather_station (one row per source per real-world location)
        │
        ├──→ weather_historical_observation  (climatology baseline; 1-2yr rolling window — Rule 2)
        └──→ weather_forecast_snapshot        (forecast_prob input; re-issued snapshots, not overwritten)
        │
        │  human-reviewed ONCE per city/event (not per strike market)
        ▼
weather_market_mapping  (market_slug ↔ SETTLEMENT station — Wunderground; forecast station noted
                          in settlement_rule free text; metric/forecast_for/target_temp_min_f/
                          target_temp_max_f parsed from the slug itself — Rule 14, 2026-07-20)
        │
        ├──→ weather_market_odds_snapshot  (append-only time series of Polymarket's own implied
        │                                   probability — the "eyes" half of a future early-exit
        │                                   strategy — Rule 14, 2026-07-20)
        ▼
weather_probability_estimate  (climatology + forecast → blended_prob; market_implied_prob from
                                bullpen; edge = blended - implied — checkMarkets.ts, Rule 14)
        │
        ├── sparse, on-demand only ──→ verifySettlement.ts (Wunderground — Rule 4/5, pre-closeout ONLY)
        ▼
weather_position  (the Order Builder — orderBuilder.ts, Rule 15: Kelly-sized paper trades,
                    enforcing Rules 7/11/12, live-verified 2026-07-20)
        │
        ├── earlyExit.ts (Rule 16, the "brain for swing trading"): scans open positions against
        │   fresher checkMarkets.ts estimates, closes on profit-target (alpha decay below the same
        │   Rule 7 floor) or stop-loss (model inversion / temp-buffer failure) — paper-only,
        │   auto-closing, live-verified 2026-07-20
        ├── settlePositions.ts (Rule 19): for a position held to expiration, marks it to
        │   Polymarket's own on-chain resolution (bullpen polymarket event, NOT Wunderground —
        │   see Rule 19 for why this differs from Rule 5's scope) — live-verified 2026-07-20
        ▼
weather_pnl_snapshot  (rollup not yet built — see Roadmap)
```

**Why two source roles, not one.** Every source that feeds `forecast_prob`/`climatology_prob`
(NOAA, Open-Meteo, METAR) is fast, cheap, and frequently refreshed — but per the basis-risk finding
above, **none of them is trusted as the number a position is validated against at resolution**.
Only Wunderground fills that role, and only via a conservative, on-demand fetch — never a polled
source. Conflating these two roles is exactly the mistake the reconciliation check caught before
any code was written.

**Station identity is source-scoped, not global.** `weather_station.external_id` holds a
different identifier per source for what a human considers "the same place" — NOAA's
gridpoint/station ID, Open-Meteo's rounded lat/lon (it has no station concept), METAR's ICAO
code, Wunderground's station page ID. There's no database-level link between these rows for the
same real place (matches the rest of this schema — zero foreign keys anywhere, see Schema
Strategy below); the pairing is made once, by a human, during the per-city mapping review.
**Corrected 2026-07-19**: this table's `external_id` column originally had a single-column
`UNIQUE` constraint, which contradicted "one row per source" and silently made it impossible for
two sources to ever share the same external ID — caught the moment `discoverMarkets.ts` (a second
writer) actually tried it, fixed via migration to a composite `(external_id, source)` unique
index. See `docs/weather/WEATHER_RISK_MANAGEMENT.md` Rule 9 for the full story — a concrete
example of this schema's "no FKs" trade-off cutting both ways: no rigidity from out-of-order
writes, but also no automatic catch for an overly-strict constraint until real multi-writer usage
exercises it.

**Market mapping is semi-automated discovery, human-approved — implemented and live-run (2026-07-19).**
`discoverMarkets.ts` runs `bullpen polymarket discover --category weather --output json` to find
new events, parses each description for its settlement station, and writes one
`weather_market_mapping` row per surviving strike market (Rule 10's odds filter applies first),
all sharing one station pairing per event so human review only happens once per city/event even
though Polymarket groups many binary strike markets — one per degree bucket — under that one
event. `is_active` always starts `false`. **Real finding from the first live run**: not every
weather event actually settles via Wunderground — Hong Kong's cites the Hong Kong Observatory
directly; `discoverMarkets.ts` detects and skips non-Wunderground events rather than mislabeling
them. The
alternative (reviewing every individual degree-bucket market) was considered and rejected as
excess manual effort for no added safety — the risky judgment call is which station to trust, not
which bucket.

**Probability computation — built and live-verified end-to-end (2026-07-20), `checkMarkets.ts`
(Rule 14).** `climatology_prob` comes from `calculateClimatology.ts` (a ±7-day day-of-year window
across every backfilled year of `weather_historical_observation`); `forecast_prob` comes from
`calculateProbability.ts`'s ensemble query (Rule 13); both blend into `blended_prob = 0.35 *
climatology + 0.65 * forecast` (a stated v1 heuristic, not a calibrated weighting), tagged with a
`model_version` string so blending strategies stay comparable over time; `market_implied_prob`
comes from the current strike price via `bullpen` and is also logged, on every check, as its own
row in `weather_market_odds_snapshot`; `edge = blended_prob - market_implied_prob`. Every row is
appended, never mutated, to `weather_probability_estimate` — an append-only research log, same
pattern as the Copy Bot's `leaderboard_scan`. **A real gap found live**: a market whose
`forecast_for` date has already elapsed produces a large but meaningless edge, since the stored
ensemble forecast predates the market's price having converged toward the actual outcome — see
`docs/weather/WEATHER_RISK_MANAGEMENT.md` Rule 14 for the full finding; a freshness guard is not
yet built.

---

## 2. Schema strategy

All 7 tables already exist in `packages/db/src/schema.ts` (lines 309-411), created in the initial
migration, zero rows, zero code touching them today — no migration is needed to start building.

**Pipeline**: `weather_station` → (`weather_historical_observation`, `weather_forecast_snapshot`)
→ `weather_market_mapping` (the only table touching both the weather and Polymarket domains, via
`market_slug`) → `weather_probability_estimate` → `weather_position` → `weather_pnl_snapshot`.

**No foreign-key constraints**, matching the rest of this codebase (zero `.references()` calls
anywhere in `schema.ts`, weather or Copy Bot tables alike). SQLite only enforces FKs with
`PRAGMA foreign_keys = ON` per-connection, which would turn any out-of-order write (e.g. a
backfill script racing ahead of its station row) into a hard failure instead of an inert loose
reference — real rigidity cost, for several independent ingestion scripts, in exchange for a
benefit (referential integrity on a system that never hard-deletes rows) that's available more
cheaply another way: an application-level guard function (`assertStationExists(stationId)` in
`packages/weather/src/db/writers.ts`), called at the top of every writer that references a
`station_id`/`market_slug` — the same single-writer-funnel pattern the Copy Bot's
`upsertWalletProfile` already uses.

**`weather_market_mapping.station_id` = the settlement station** (Wunderground), not a forecast
station — this is the one place the original design assumption changed after the reconciliation
finding. The paired forecast station lives in the existing free-text `settlement_rule` column
(e.g. *"Settles via Wunderground RKSI page; forecast/nowcast paired to METAR RKSI + Open-Meteo
grid 37.47,126.45"*) — no schema change needed.

**BUILT AND LIVE-VERIFIED (2026-07-20): `weather_market_mapping` extended, plus a 9th table,
`weather_market_odds_snapshot`** (migration `0005_magical_captain_britain.sql`, applied following
the established stop-`bot.py`/`dashboard.py`/Next.js-dev-server, migrate, restart runbook).
`weather_market_mapping` gained four nullable columns — `metric` (`"max"`/`"min"`), `forecast_for`
(`YYYY-MM-DD`), `target_temp_min_f`, `target_temp_max_f` — populated by `parseMarketThreshold.ts`
regex-parsing the market's own slug (nullable because ~28 real Weather-category markets aren't
temperature buckets at all — air quality, earthquakes, etc. — and correctly parse to nothing).
`weather_market_odds_snapshot` is a new, separate, append-only table (`market_slug`, `recorded_at`,
`implied_probability`, indexed on `(market_slug, recorded_at)`) — deliberately never upserted,
since it exists specifically to be a time series a future early-exit strategy can read for a
probability *shift*, which an upserted "latest value" table can't answer. See
`docs/weather/WEATHER_RISK_MANAGEMENT.md` Rule 14 for the full parsing-engine and
rounding-boundary-math detail.

**BUILT AND LIVE-VERIFIED (2026-07-20): `weather_position` extended** (migration
`0006_goofy_grey_gargoyle.sql`) — the Order Builder (Rule 15) reuses this pre-existing, previously
-empty 7th table rather than creating a new one, since it was already shaped exactly for a paper
trade (`marketSlug`/`outcome`/`entryPrice`/`ourSizeUsd`/`status`). Gained 8 nullable columns for
execution telemetry: `ensemble_prob`, `polymarket_prob`, `probability_difference`, `is_same_day`,
`station_local_time`, `temp_buffer_f`, `full_kelly_fraction`, `applied_fraction` — the exact
metrics that justified each trade, per Joey's explicit spec, recorded at build time rather than
needing to be re-derived later.

**No connection to Copy Bot tables**, and no reuse of the Copy Bot's `rule_set`/`rule_change`
tables — those are shaped around copy-trading-specific concepts (mute streaks, TTP activation,
spread tolerance) and reusing them would blur exactly the boundary `.claudeprompt` requires stay
sharp. Each `weather_probability_estimate` row already carries its own `model_version`, which is
enough versioning for now; a dedicated `weather_rule_set` table is a reasonable future addition
once entry-threshold/position-sizing logic exists, not needed yet.

**BUILT AND LIVE-VERIFIED (2026-07-20): an 8th table, `weather_ensemble_forecast`**
(migration `0003_cooing_beast.sql`), for `ingestOpenMeteo.ts`'s ensemble members. Rule 13 requires
ensemble forecasting, not a single deterministic model; the existing `weather_forecast_snapshot`
table stores one point forecast per (station, day), which has no shape for ~82 individual ensemble
members. Rather than cram a large JSON blob into `weather_forecast_snapshot.rawJson` (would work
with zero schema change, but forces every future probability calculation to parse JSON in
application code instead of running SQL), the decision (Joey, 2026-07-20) was a dedicated table:

```
weather_ensemble_forecast
  id
  stationId
  forecastFor      -- the target date this member is forecasting, station-local calendar day
  issuedAt         -- when this ensemble run was fetched (re-issued forecasts accumulate, same
                       pattern as weather_forecast_snapshot)
  model            -- "ecmwf_ifs025" | "gfs_seamless"
  memberIndex      -- 0 = control member, 1..50 (ecmwf) or 1..30 (gfs) = perturbed members
  tMaxF, tMinF      -- that member's own forecasted daily max/min, station-local day
  UNIQUE (stationId, forecastFor, issuedAt, model, memberIndex)
```

This makes "probability of hitting a target bucket" a direct SQL query —
`COUNT(*) WHERE tMaxF >= threshold` divided by total member count — rather than JSON parsing at
read time. **Verified live**: `EXPLAIN QUERY PLAN` on exactly that query confirms
`weather_ensemble_forecast_lookup_idx` is actually used (`SEARCH ... USING INDEX`, not a table
scan). Real result pulled this way, RKSI's next day: `ecmwf_ifs025` put 11/51 members (~22%) at or
above 80°F, `gfs_seamless` put only 1/31 (~3%) — a genuine cross-model disagreement, exactly the
signal a single deterministic forecast would have hidden.

**Retention gap — closed same-day (2026-07-20) by `pruneForecasts.ts`.** Each `ingestOpenMeteo.ts`
run is a NEW forecast generation (`issuedAt` set fresh per run) — by design, matching
`weather_forecast_snapshot`'s existing "reissued forecasts accumulate" pattern, not a bug. Policy
(Joey's call): keep ONLY the latest `issuedAt` generation per (station, model) pair, delete
everything older — no rolling window, since `calculateProbability.ts` only ever needs the current
distribution. Implemented as a single correlated-subquery `DELETE`, atomic by construction (one
SQL statement, no read-then-delete race window against a concurrent ingestion run), supported by
a dedicated index (`weather_ensemble_forecast_station_model_issued_idx` on
`(stationId, model, issuedAt)`, migration `0004_flashy_rawhide_kid.sql`). **Live-verified**: ran
against the real 70,520-row dataset from two accumulated test generations — pruned to exactly
35,260 (one generation × 43 stations × 2 models), then re-ran immediately and confirmed 0 rows
deleted (correct: nothing is "older than the max" once only one generation remains — proves the
query doesn't over-delete on a single-generation table). **Trade-off, named explicitly**: this
policy makes it impossible to later ask "how did our forecast change as the event approached" — a
real forecast-skill-validation question for a future `reviewOutcomes.ts` — since only the single
latest snapshot ever survives. Revisit toward a short rolling window if that question becomes
important; not needed for the "make today's trading decision" use case this serves now.

---

## 3. Execution cycle

**BUILT AND LIVE-VERIFIED (2026-07-20) — `runWeatherLoop.ts` + one `launchd` job, refining (not
reversing) the original design below.** The original plan was one `launchd` job PER cadence,
explicitly to avoid a long-running always-on orchestrator process (reasoning preserved below,
since it still explains the "not always-on" constraint this design also respects). Joey's explicit
instruction this session (2026-07-20) asked for one master runner script + one `.plist` instead —
resolved by making the single script internally cadence-aware (a small JSON state file tracks each
gated step's last-run time) rather than either contradicting the original non-persistent-process
goal or building N separate jobs. **The orchestrator itself is still not a long-running process**
— `launchd` spawns it fresh on each hourly tick, it runs for seconds-to-minutes, then exits; the
original concern (a second always-on Node process alongside `bot.py`) doesn't apply either way.

**Sequence, per run**: Ingest (`ingestMetar.ts` every tick; `discoverMarkets.ts` daily-gated;
`ingestOpenMeteo.ts` 4h-gated) → Prune (`pruneForecasts.ts` only when ensemble ingest just ran;
`pruneHistorical.ts` daily-gated) → **Settle Positions** (`settlePositions.ts`, every tick,
unconditional — Rule 19, marks expired positions to Polymarket's own resolution, runs regardless
of Check Markets' outcome) → Check Markets (`checkMarkets.ts`, every tick, **critical** — see Rule
18) → Order Builder (`orderBuilder.ts`) → Early Exit (`earlyExit.ts`) → PnL Snapshot
(`updatePnl.ts`, always attempted last). Full failure-handling and cadence-gating detail:
`docs/weather/WEATHER_RISK_MANAGEMENT.md` Rule 18.

| Job | Cadence (as actually implemented) | Why |
|---|---|---|
| Historical backfill | One-time per station, on onboarding — **not** part of the hourly loop | A backfill, not a stream — capped at 1-2 years per Rule 2; re-running it hourly would be a wasteful full-fleet sweep for data that doesn't change that often |
| METAR nowcast pull | Every hourly tick | Cheap, high-value intraday signal — forecast input, never settlement |
| Market discovery | Gated to ~daily within the hourly orchestrator | New city/day events appear roughly daily; running the 100-event scan hourly was judged unnecessary |
| Ensemble forecast refresh (`ingestOpenMeteo.ts`) | Gated to ~4h within the hourly orchestrator | ~82 members × 43 stations × ~10 forecast days ≈ 35,000 rows per cycle — real, avoidable API/DB load if run hourly instead |
| Forecast/ensemble pruning | Runs immediately after ensemble ingest, same gated tick | Only meaningful right after new data lands |
| Historical data pruning | Gated to ~daily within the hourly orchestrator | Enforces Rule 2's rolling retention window |
| Price refresh + edge recompute (`checkMarkets.ts`) | Every hourly tick | Cheap `bullpen` calls; price moves faster than forecast |
| Order Builder / Early Exit | Every hourly tick, immediately after Check Markets succeeds | Matches the "position management" cadence the original table specified |
| **Wunderground settlement verification** | **On-demand only** — once per new mapping, once per position pre-closeout | **Deliberately never polled, and NOT part of the orchestrator at all — see `docs/weather/WEATHER_RISK_MANAGEMENT.md` Rule 4** |
| PnL rollup (`updatePnl.ts`) | Every hourly tick, always attempted last | Cheap aggregate query; doesn't strictly depend on this tick's fresh Check Markets data |

This mirrors, at the scheduling level, the same cheap/frequent-vs-expensive/slower instinct the
Copy Bot's `scoreWallets.ts` already uses within a single run (its pass-1/pass-2 design).

**Activation — deliberately not done yet.** The `.plist`
(`packages/weather/launchd/com.copybot.weather.loop.plist`) was written and live-tested, including
under a simulated launchd environment (`env -i` with a minimal `PATH`, no inherited shell state) —
this test caught two real bugs before activation, not after (see Rule 18 for both). `launchctl
load` has NOT been run; Joey chose to review and activate it herself when ready
(`AskUserQuestion`, 2026-07-20) rather than have it activated automatically. Exact commands, for
whenever that is:

```bash
# Install (copies the plist into the per-user LaunchAgents directory, then loads it)
mkdir -p ~/Library/LaunchAgents
cp /Users/joeychan/polymarket-copybot/packages/weather/launchd/com.copybot.weather.loop.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.copybot.weather.loop.plist

# Confirm it's loaded (prints its PID if currently running, "-" if idle between ticks, and its last exit status)
launchctl list | grep com.copybot.weather.loop

# Watch it run (RunAtLoad=true means the load command above also triggers an immediate first run)
tail -f /Users/joeychan/polymarket-copybot/packages/weather/data/weather-loop.log
tail -f /Users/joeychan/polymarket-copybot/packages/weather/data/weather-loop.error.log

# Stop and uninstall
launchctl unload ~/Library/LaunchAgents/com.copybot.weather.loop.plist
rm ~/Library/LaunchAgents/com.copybot.weather.loop.plist
```

---

## 4. Proposed `packages/weather/` structure

`packages/weather/` is now a real, wired-in pnpm workspace member — `package.json`/`tsconfig.json`
exist, and thirty scripts/modules are built, tested, and live-verified against `data/app.db` and
real Polymarket/Open-Meteo data (2026-07-19 through 2026-07-20). Structure mirrors
`packages/copy-trading`'s conventions: plain
`tsx`-executed scripts, an `isMainModule` guard so files stay test-importable, vitest for
pure-function tests. **`db/writers.ts` is now built** — introduced exactly when
`discoverMarkets.ts` became a second writer of `weather_station` rows, the trigger this section
originally said would justify it.

```
packages/weather/
  package.json                          # @copybot/weather — built ✅
  tsconfig.json                         # built ✅
  src/
    ingestMetar.ts                      # BUILT ✅ — aviationweather.gov, nowcast/forecast input, never settlement
    pruneHistorical.ts                  # BUILT ✅ — Rule 3, enforces 2yr/60-day rolling retention
    backfillHistorical.ts               # BUILT ✅ — Rule 3, one-shot full-fleet 2yr backfill via IEM ASOS archive (NOT aviationweather.gov — see Rule 3)
    emergencyCloseoutGuard.ts           # BUILT ✅ — Rule 4's slippage ceiling (5% / $0.05 floor)
    emergencyCloseoutGuard.test.ts      # BUILT ✅ — 8 tests
    checkSettlementAgainstMetar.ts      # BUILT ✅ — Rule 6's Dual Oracle Cross-Check (4°F threshold)
    checkSettlementAgainstMetar.test.ts # BUILT ✅ — 8 tests
    oddsFilter.ts                       # BUILT ✅ — Rule 10's Extreme Odds Filter (10%-90% band)
    oddsFilter.test.ts                  # BUILT ✅ — 8 tests
    discoverMarkets.ts                  # BUILT ✅ — bullpen discover --category weather → draft mapping candidates, odds-filtered
    db/writers.ts                       # BUILT ✅ — shared upsertWeatherStation/upsertMarketMapping/findStationByExternalId
    stationReconciliation.ts            # BUILT ✅ — auto-onboarding: real coords (aviationweather.gov) + timezone (geo-tz, offline)
    stationReconciliation.test.ts       # BUILT ✅ — 4 tests, incl. cities never hardcoded anywhere (London, Tokyo)
    ingestNoaa.ts                       # not yet built — forecast + climatology input
    ingestOpenMeteo.ts                  # BUILT ✅ — ecmwf_ifs025 (51 members) + gfs_seamless (31 members), 35,260 rows/run live-verified across all 43 stations
    pruneForecasts.ts                   # BUILT ✅ — Rule 13, keeps only latest issuedAt generation per (station, model)
    calculateProbability.ts             # BUILT ✅ — Rule 13, ensemble hit-rate probability, combined + per-model
    calculateClimatology.ts             # BUILT ✅ — Rule 14, ±7-day day-of-year historical base rate
    parseMarketThreshold.ts             # BUILT ✅ — Rule 14, slug → metric/threshold parser, 1024/1052 real markets parsed
    parseMarketThreshold.test.ts        # BUILT ✅ — 9 tests
    calculateClimatology.test.ts        # BUILT ✅ — 5 tests (window/leap-year/boundary math)
    checkMarkets.ts                     # BUILT ✅ — Rule 14, the EV Bridge: odds snapshot + blended prob + edge, live-verified end-to-end
    verifySettlement.ts                 # not yet built — Playwright, Wunderground, ON-DEMAND ONLY (deliberately deferred, see docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 5)
    detectAnomaly.ts                    # not yet built — Rule 4's general physically-impossible-jump bounds-check
    staleness.ts                        # BUILT ✅ — Rule 14 addendum, station-local staleness/cutoff guard
    staleness.test.ts                   # BUILT ✅ — 8 tests
    orderSizing.ts                      # BUILT ✅ — Rule 15, pure Kelly/edge-floor/temp-buffer/sizing math
    orderSizing.test.ts                 # BUILT ✅ — 14 tests
    orderBuilder.ts                     # BUILT ✅ — Rule 15, the Order Builder: reads weather_probability_estimate, writes sized paper trades to weather_position
    exitSignals.ts                      # BUILT ✅ — Rule 16, pure profit-target/stop-loss decision math
    exitSignals.test.ts                 # BUILT ✅ — 11 tests
    earlyExit.ts                        # BUILT ✅ — Rule 16, the Early-Exit Engine: scans open positions, auto-closes on profit-target/stop-loss
    constants.ts                        # BUILT ✅ — Rule 17, shared WEATHER_PAPER_BANKROLL_USD (correctness-critical, not left as two independent copies)
    updatePnl.ts                        # BUILT ✅ — Rule 17, the Portfolio Rollup: equity curve, one weather_pnl_snapshot row per run
    settlePositions.ts                  # BUILT ✅ — Rule 19, the Settlement Engine: marks expired positions to Polymarket's own resolution
    runWeatherLoop.ts                   # BUILT ✅ — Rule 18, the Orchestrator: cadence-aware, runs the full pipeline in sequence
  launchd/
    com.copybot.weather.loop.plist      # BUILT ✅ — Rule 18, hourly launchd job — written and tested, NOT YET ACTIVATED (Joey's call)
```

`packages/weather/package.json` scripts, built: `ingest:metar`, `prune:historical`,
`discover:markets`, `backfill:historical`, `ingest:openmeteo`, `prune:forecasts`,
`calculate:probability`, `check:markets`, `build:orders`, `check:exits`, `settle:positions`,
`update:pnl`, `run:loop`.
`run:loop` (`runWeatherLoop.ts`) supersedes the original "one `launchd` job per script" plan — see
§3 for the cadence-aware single-orchestrator design and why it doesn't reintroduce the always-on-
process problem the original plan was written to avoid. `verifySettlement.ts` deliberately has
**no** script/job of its own — it's a function called from `discoverMarkets.ts`, never an
independent poll, and deliberately not part of the orchestrator either (Rule 4/5's on-demand-only
requirement).

---

## Explicitly out of scope, for now

- NOAA ingestion and the live Wunderground fetch (`verifySettlement.ts`) — see §4's file table for
  the current built/not-built split.
- **Activating the scheduler** — the `.plist` is written and tested but `launchctl load` has not
  been run; see §3 and `docs/weather/WEATHER_RISK_MANAGEMENT.md` Rule 18 for why this is Joey's
  call, not a technical gap.
- The `weather_rule_set` table.

## Critical files

- `packages/db/src/schema.ts` (lines 309-411) — the 7 weather table definitions.
- `packages/copy-trading/src/scanLeaderboard.ts` / `scoreWallets.ts` — patterns this system mirrors.
- `packages/bullpen-client/src/index.ts` — `runBullpenJson`; its header already anticipates
  `packages/weather` as a future consumer.
- `docs/weather/WEATHER_RISK_MANAGEMENT.md` — the rules ledger this document's design choices implement.
- `.claudeprompt` — the isolation mandate this entire system is built to respect.
