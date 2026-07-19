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
                          in settlement_rule free text)
        │
        ▼
weather_probability_estimate  (climatology + forecast + same-day METAR nowcast → blended_prob;
                                market_implied_prob from bullpen; edge = blended - implied)
        │
        ├── sparse, on-demand only ──→ verifySettlement.ts (Wunderground — Rule 4/5, pre-closeout ONLY)
        ▼
weather_position  (future entry-rule logic, not yet designed)
        │
        ▼
weather_pnl_snapshot
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

**Probability computation** (future logic, not yet built): `climatology_prob` from the settlement
station's historical observations; `forecast_prob` from the paired NOAA/Open-Meteo forecast
station; both blend into `blended_prob`, tagged with a `model_version` string so blending
strategies stay comparable over time; same-day METAR readings sharpen the estimate intraday as an
event's day arrives (a genuine nowcast edge source, not just a safety net); `market_implied_prob`
comes from the current strike price via `bullpen`; `edge = blended_prob - market_implied_prob`.
Every row is appended, never mutated, to `weather_probability_estimate` — an append-only research
log, same pattern as the Copy Bot's `leaderboard_scan`.

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

**No connection to Copy Bot tables**, and no reuse of the Copy Bot's `rule_set`/`rule_change`
tables — those are shaped around copy-trading-specific concepts (mute streaks, TTP activation,
spread tolerance) and reusing them would blur exactly the boundary `.claudeprompt` requires stay
sharp. Each `weather_probability_estimate` row already carries its own `model_version`, which is
enough versioning for now; a dedicated `weather_rule_set` table is a reasonable future addition
once entry-threshold/position-sizing logic exists, not needed yet.

**PLANNED (2026-07-20, not yet built): an 8th table, `weather_ensemble_forecast`**, for
`ingestOpenMeteo.ts`'s ensemble members — decided, not yet migrated. Rule 13 requires ensemble
forecasting, not a single deterministic model; the existing `weather_forecast_snapshot` table
stores one point forecast per (station, day), which has no shape for ~82 individual ensemble
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
read time. Needs its own retention policy once built (an ensemble row has ~zero value once
`forecastFor` has resolved, same reasoning as `weather_forecast_snapshot`'s existing 60-day
window, likely shorter given the row volume — see Execution Cycle below for the volume estimate
that makes this non-optional, not just tidy).

---

## 3. Execution cycle

No scheduling infrastructure exists anywhere in this repo today (checked: zero `node-cron`,
`setInterval`, `cron.schedule`, `vercel.json`, or `.github/workflows` usage). The Copy Bot's
research pipeline is manual by design (`pnpm scan:leaderboard`, run monthly, by a human). Weather
can't be: forecasts and prices go stale in hours, not months, so this needs real unattended
scheduling — new infrastructure for this repo, not a copy of an existing pattern.

**Mechanism: OS-level `launchd`** (this runs on a Mac), one job per cadence below, **not** a
long-running orchestrator process. Chosen over a `node-cron`-based orchestrator specifically to
avoid stacking a second always-on Node process next to `bot.py` — `docs/copy-trading/SAFETY.md` §3 already
documents an unresolved "nothing restarts a dead process" gap for `bot.py`; a second orchestrator
process would compound that same accepted risk rather than just adding a new one.

| Job | Cadence | Why |
|---|---|---|
| Historical backfill | One-time per station, on onboarding | A backfill, not a stream — capped at 1-2 years per Rule 2 |
| Daily historical obs | Daily | Official daily obs finalize once |
| Historical data pruning | Daily, paired with the above | Enforces Rule 2's rolling retention window |
| Forecast snapshot refresh | Every 3-6h | Open-Meteo/NWS update a few times a day |
| **Ensemble forecast refresh** (`ingestOpenMeteo.ts`, planned) | **Every 3-6h**, same cadence as forecast snapshot | ~82 members × 43 stations × ~10 forecast days ≈ 35,000 rows per cycle — needs its own short retention (Rule 13, planned), not a "run more often than needed" cost |
| METAR nowcast pull | Hourly, same-day markets only | Cheap, high-value intraday signal — forecast input, never settlement |
| Market discovery | Daily | New city/day events appear roughly daily |
| Price refresh + edge recompute | Every 1-2h | Cheap `bullpen` calls; price moves faster than forecast |
| Position management | Same cadence as price refresh | Runs the Rule 3 anomaly gate on every check |
| **Wunderground settlement verification** | **On-demand only** — once per new mapping, once per position pre-closeout | **Deliberately never polled — see `docs/weather/WEATHER_RISK_MANAGEMENT.md` Rule 4** |
| PnL rollup | Daily or after each position pass | Cheap aggregate query |

This mirrors, at the scheduling level, the same cheap/frequent-vs-expensive/slower instinct the
Copy Bot's `scoreWallets.ts` already uses within a single run (its pass-1/pass-2 design).

---

## 4. Proposed `packages/weather/` structure

`packages/weather/` is now a real, wired-in pnpm workspace member — `package.json`/`tsconfig.json`
exist, and ten scripts/modules are built, tested, and live-verified against `data/app.db` and
real Polymarket data (2026-07-19). Structure mirrors `packages/copy-trading`'s conventions: plain
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
    ingestOpenMeteo.ts                  # not yet built — PLANNED: ecmwf_ifs025 (51 members) + gfs_seamless (31 members) ensemble, verified endpoints, see Schema Strategy §2
    verifySettlement.ts                 # not yet built — Playwright, Wunderground, ON-DEMAND ONLY (deliberately deferred, see docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 5)
    detectAnomaly.ts                    # not yet built — Rule 4's general physically-impossible-jump bounds-check
    computeProbability.ts               # not yet built — climatology + forecast + nowcast blend
    checkMarkets.ts                     # not yet built — refresh market-implied prices, recompute edge
    managePositions.ts                  # not yet built — future entry-rule design
    updatePnl.ts                        # not yet built — weather_pnl_snapshot rollup
```

Root `package.json` scripts (future): `weather:ingest-historical`, `weather:ingest-forecast`,
`weather:ingest-metar`, `weather:discover-markets`, `weather:check-markets`,
`weather:manage-positions`, `weather:update-pnl`, `weather:prune-historical` — each its own
`launchd` job. `verifySettlement.ts` deliberately has **no** script/job of its own — it's a
function called from `discoverMarkets.ts` and `managePositions.ts`, never an independent poll.

---

## Explicitly out of scope, for now

- NOAA/Open-Meteo ingestion, market discovery, probability computation, and the live Wunderground
  fetch (`verifySettlement.ts`) — see §4's file table for the current built/not-built split.
- Entry-rule / position-sizing logic for `weather_position`.
- The `weather_rule_set` table.
- `launchd` plist files themselves (mechanism decided; concrete job definitions come with the
  scripts they invoke).

## Critical files

- `packages/db/src/schema.ts` (lines 309-411) — the 7 weather table definitions.
- `packages/copy-trading/src/scanLeaderboard.ts` / `scoreWallets.ts` — patterns this system mirrors.
- `packages/bullpen-client/src/index.ts` — `runBullpenJson`; its header already anticipates
  `packages/weather` as a future consumer.
- `docs/weather/WEATHER_RISK_MANAGEMENT.md` — the rules ledger this document's design choices implement.
- `.claudeprompt` — the isolation mandate this entire system is built to respect.
