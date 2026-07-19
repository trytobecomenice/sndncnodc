# Current State

**Audience note:** zero prior context assumed. This is a point-in-time snapshot — check the
"Last reviewed" date below before trusting a number in here over the live system (`bot.out.log`,
`data/app.db`, `bullpen status`).

**Last reviewed: 2026-07-20** (updated same-day: Weather Bot Early-Exit Engine — `earlyExit.ts` — built and live-verified; profit-target/stop-loss auto-close now closes the position lifecycle)

## Snapshot, right now

- **Mode:** paper trading only (`config.LIVE_MODE = False`) — no real funds at risk.
- **Tracked wallets:** 20, from the static list in `config.py`
  (`TRACKED_TRADERS_SOURCE = "static"`). The scored-database source (`"db"`) is built and tested
  but intentionally left off pending review — see `docs/copy-trading/RISK_MANAGEMENT.md` Rule 2.
- **Bot process:** running continuously, polling every 30 seconds.
- **Risk controls live:** per-trader circuit breaker, duplicate-exposure guard, portfolio
  exposure ceiling ($250 total / $30 per event), drawdown kill switch ($100 floor / $50 off
  peak), and the "Disciplined Taker" pre-trade slippage ceiling (5%, LIVE-mode only — see
  `docs/copy-trading/RISK_MANAGEMENT.md` Rules 3–6 and 11 for full detail). All portfolio-level checks are
  unit-tested (`python3 -m unittest test_risk_manager test_bot_risk_checks`, 26/26 passing as of
  this review).
- **Operational hardening (2026-07-19 evening):** every bullpen subprocess call now has an
  explicit hard timeout (60s default; the tracker-feed poll is tightened to 20s — see
  `docs/copy-trading/SAFETY.md` §3, "Whole-process freeze"), and repeated identical closeout-sweep fetch
  failures are throttled in the event log (first logged, then ~daily reminders with a running
  count — `docs/copy-trading/RISK_MANAGEMENT.md` Rule 7) after a transient bullpen outage produced hundreds
  of duplicate error rows. `dashboard.py`'s Start button now launches the bot with `-u`,
  matching the manual launch path. Known remaining gap, accepted for now: nothing external
  restarts or alerts on a dead `bot.py` process.
- **Wallet-research pipeline:** `scanLeaderboard.ts` / `scoreWallets.ts` are built, but a fresh
  re-run is currently **blocked** — bullpen's `data leaderboard` endpoint has been returning
  `NETWORK_TIMEOUT` (its `smart-money` endpoint is healthy, so this is a partial outage, not a
  full one). Don't re-run `pnpm scan:leaderboard` until that endpoint recovers.
- **Execution-shortfall data:** accumulating since 2026-07-18 (`bot.py`'s
  `measure_paper_shortfall`) — not yet at the "few weeks" volume the Phase 2 scoring plan is
  gated on. See `docs/copy-trading/RISK_MANAGEMENT.md` Rule 10.
- **Dashboards:** the built-in `dashboard.py` (port 8787) and the Next.js `apps/dashboard`
  Overview page both read live from `data/app.db`. The Next.js dashboard's other 8 planned pages
  aren't built yet.
- **Weather Bot:** early implementation. `packages/weather/` is a real pnpm workspace member with
  nine scripts/modules built, tested, and live-verified: `ingestMetar.ts` (proven end-to-end
  against RKSI), `pruneHistorical.ts` (2yr/60-day rolling retention), `emergencyCloseoutGuard.ts`
  (Rule 4's 5%/$0.05 slippage ceiling), `checkSettlementAgainstMetar.ts` (Rule 6's 4°F Dual Oracle
  Cross-Check), `oddsFilter.ts` (Rule 10's 10%-90% Extreme Odds Filter), `discoverMarkets.ts`,
  `db/writers.ts`, `stationReconciliation.ts` (dynamic station auto-onboarding — see below).
  28/28 unit tests passing (`pnpm --filter @copybot/weather test`).
  **A real schema bug was found and fixed** (2026-07-19): `weather_station.external_id` had a
  single-column unique constraint that silently contradicted the documented "one row per source"
  design — caught the moment `discoverMarkets.ts` became a second writer, fixed via migration
  `0002_chief_johnny_blaze.sql` (composite `(external_id, source)` unique index), existing data
  verified intact after. **Auto-onboarding built, then corrected twice same-day** (2026-07-19,
  also replacing a hardcoded per-station timezone map in `ingestMetar.ts`): `discoverMarkets.ts`
  resolves real coordinates from `aviationweather.gov` and a real timezone from `geo-tz` (offline
  lookup) automatically for every Wunderground-sourced station it sees — no manual code change
  per city, and deliberately decoupled from the odds filter (Joey caught that gating station
  knowledge on current odds meant most cities stayed unknown forever). Then Joey caught a second,
  bigger gap: the discovery scan itself defaulted to `--limit 10`, when `bullpen polymarket
  discover` actually caps at **100 events per call** (verified live — a higher limit doesn't
  return more), spanning **48 distinct cities**, not the ~8 a limit-10 scan could ever see.
  Default bumped to 100. **Full-scale result**: 1,062 real strike markets scanned, 35 more
  stations auto-onboarded in one ~32s pass (43 total, every inhabited continent), 181 draft
  market mappings written pending Rule 8 review. Every station resolved correctly — `Asia/Kolkata`
  for Lucknow, `America/Argentina/Buenos_Aires` for Buenos Aires, `Africa/Johannesburg` for Cape
  Town, `Asia/Singapore` for Kuala Lumpur (no separate zone exists) — none of which a manual map
  would have gotten right by accident. Re-scan confirmed idempotent (0 newly onboarded). 6
  non-city-temperature "Weather"-category markets (air quality, earthquakes, a hantavirus market)
  were also discovered and correctly fell through the existing non-Wunderground skip path with no
  special-casing needed. `resolveTimezone` separately unit-tested against cities never hardcoded
  anywhere (London, Tokyo). Three more risk parameters formalized in the docs (not yet
  implemented — blocked on entry-rule logic and a capital-base constant that don't exist yet):
  Rule 11 (5% max capital per
  trade), Rule 12 (1.5°F minimum forecast-vs-strike buffer), Rule 13 (ensemble forecasting
  required, not a single deterministic model). `packages/weather`/`packages/copy-trading`
  isolation re-verified clean.
- **Global historical backfill — the full fleet's climatology "memory," built and live-run**
  (2026-07-19, `backfillHistorical.ts`, 10th script). Real finding first: `aviationweather.gov`
  (`ingestMetar.ts`'s source) was verified to cap at ~8-9 days of history, physically incapable of
  a multi-year backfill — switched to the Iowa Environmental Mesonet's ASOS archive (free, public,
  one request per station covers the whole 2-year window). All 43 stations backfilled in one run,
  zero failures, 31,384 daily rows total. Window is exactly 2 years (not the 2.5 first proposed —
  Joey's call, to match Rule 3's retention policy exactly, reusing `pruneHistorical.ts`'s own
  cutoff function so the two can't drift apart). Idempotent both by upsert (re-run confirmed zero
  duplicate rows) and by a resume optimization (re-run skipped 39/43 stations with zero network
  calls, since they were already fully backfilled). Rate-limit-aware by necessity, not caution for
  its own sake — the archive's rate limiter triggered after just 2 manual test requests during
  development; the real 43-station run needed zero backoffs at the 5s pacing used.
- **Ensemble forecast ingestion — built and live-verified across all 43 stations** (2026-07-20,
  `ingestOpenMeteo.ts`, 11th script; `weather_ensemble_forecast`, 8th table, migration
  `0003_cooing_beast.sql`). Fetches `ecmwf_ifs025` (51 members) + `gfs_seamless` (31 members) per
  station — verified both model identifiers live rather than trusting docs, catching that the
  commonly-cited `ecmwf_ifs04` silently returns a single non-ensemble series with no error; the
  real one is `ecmwf_ifs025`. 35,260 rows in one run (exactly 82 members × 10 days × 43 stations),
  zero failures. Chunked bulk upserts (`onConflictDoUpdate`, 300 rows/statement) confirmed via
  `EXPLAIN QUERY PLAN` to actually use the new lookup index. Real signal surfaced immediately:
  RKSI's next-day forecast has `ecmwf_ifs025` putting 11/51 members (~22%) at/above 80°F vs.
  `gfs_seamless`'s 1/31 (~3%) — a genuine cross-model disagreement. Also fixed while here:
  `dashboard.py` (port 8787) had been stopped since before this session and was restarted.
- **Retention closed + probability engine built, both live-verified** (2026-07-20, `pruneForecasts.ts`
  12th script, `calculateProbability.ts` 13th script). Prune policy: keep only the latest
  `issuedAt` generation per (station, model), one atomic correlated-subquery `DELETE`, supported
  by a new index (migration `0004_flashy_rawhide_kid.sql`). Live-proven against the real
  accumulated dataset: 70,520 → 35,260 rows, then confirmed idempotent (re-run deleted 0 rows).
  `calculateProbability.ts` queries the latest generation and computes hit-rate probability,
  combined and per-model — real results pulled live: RKSI next-day ≥80°F came back 31.7% combined
  but `ecmwf_ifs025` 19.6% vs. `gfs_seamless` 51.6%; KLGA's 78-79°F bucket was `ecmwf_ifs025` 31.4%
  vs. `gfs_seamless` a flat 0.0%.
- **The EV Bridge — built and live-verified end-to-end** (2026-07-20, migration
  `0005_magical_captain_britain.sql`; `parseMarketThreshold.ts`, `calculateClimatology.ts`,
  `checkMarkets.ts`, 16th-18th scripts; `weather_market_odds_snapshot`, 9th table). Closes the two
  gaps the previous entry flagged. `weather_market_mapping` gained `metric`/`forecast_for`/
  `target_temp_min_f`/`target_temp_max_f`, populated by a new regex slug-parser
  (`parseMarketThreshold.ts`) that also applies the confirmed rounding-boundary math (a stated
  `27C` bucket is really `[26.5, 27.5]`) — validated against all 100 live events: 1,024/1,052 real
  strike markets parsed, all 28 non-matches confirmed genuinely non-temperature. Re-running
  `discoverMarkets.ts` backfilled 207 mapping rows with the new fields. `calculateClimatology.ts`
  adds the second, independent probability input (±7-day day-of-year historical hit-rate).
  `checkMarkets.ts` orchestrates all of it per `is_active=true` mapping: logs a
  `weather_market_odds_snapshot` row, computes climatology + ensemble probabilities, blends them
  (`0.35 * climatology + 0.65 * forecast`, a stated v1 heuristic), writes
  `weather_probability_estimate` with the edge, and flags `|edge| >= 5%` as notable (log-only, not
  a trade decision). **Live-tested against a real market** (KLGA, temporarily activated then
  reverted — no real position taken): correctly fetched live odds (71.0%), computed and blended
  climatology/forecast probabilities (7.4%), and wrote both DB rows. **A real gap found by that
  same test, not hidden**: the edge came back a huge -63.6pp, not from real mispricing but because
  the market's `forecast_for` date had already elapsed relative to the stored (same-day-issued)
  forecast — `checkMarkets.ts` has no freshness guard yet, so a human reviewing candidates for
  `is_active` must independently confirm the forecast date is meaningfully in the future. Explicitly
  deferred to a future phase (Joey, 2026-07-20): the Order Builder (Rule 11/12 enforcement) and the
  "Buy Low, Sell High" early-exit strategy the odds-history table exists to enable. Full detail:
  `docs/weather/WEATHER_RISK_MANAGEMENT.md` Rule 14.
- **The Staleness Guard + the Order Builder — both built and live-verified** (2026-07-20;
  `staleness.ts`, `orderSizing.ts`, `orderBuilder.ts`, 19th-21st scripts; migration
  `0006_goofy_grey_gargoyle.sql` extends `weather_position`). Closes the forecast-staleness gap the
  previous entry flagged: `checkMarkets.ts` now skips any mapping whose target day is fully past,
  or whose station-local clock has passed 18:00 on the target day itself (same-day trading earlier
  than that is allowed) — station-local time via `Intl.DateTimeFormat` against each station's
  real timezone, not server/UTC time. `orderBuilder.ts` is the "hands" of the bot: reads
  `checkMarkets.ts`'s logged edges and, for every active mapping, enforces Rule 7 (edge must clear
  a 5pp floor), Rule 12 (ensemble forecast must clear a 1.5°F buffer from every strike boundary —
  generalized from a one-sided to a two-sided check for real exact-degree buckets), and Rule 11
  (Quarter-Kelly sizing, hard-capped at 5% of a new `WEATHER_PAPER_BANKROLL_USD = 10000` mock
  capital base — the first concrete value for a constant Rule 11 was previously blocked on) — only
  writing a sized paper trade to `weather_position` if all three pass. Also added, proactively: a
  duplicate-exposure guard so re-running never stacks a second position on an already-open market.
  **Live-verified against the 7 real, human-approved Seoul markets**: 4 correctly rejected on the
  temp buffer (forecasts sitting 0.1-0.6°F from a bucket edge), 3 real paper trades placed — BUY
  YES $154 (quarter-Kelly-bound) and two BUY NO $500 trades (both hit the 5% cap; full-Kelly
  fractions were 67% and 82%, exactly the over-betting scenario the cap exists to catch). Re-run
  confirmed idempotent (0 new orders, all 3 correctly skipped as duplicates). **Joey explicitly
  asked for a design review before this was built** (not just "proceed") — three concerns were
  raised and resolved first: full Kelly was judged too aggressive given the still-uncalibrated edge
  model (resolved: quarter-Kelly), Rule 7's edge floor wasn't in the original scope but was judged
  a natural companion to Rule 12 (resolved: built now), and "too close to end of day" needed a
  concrete number (resolved: 18:00 station-local). **Kept as real paper-trading positions**, Joey's
  explicit choice — not cleared as test artifacts.
- **The Early-Exit Engine — built and live-verified** (2026-07-20; `exitSignals.ts`,
  `earlyExit.ts`, 22nd-23rd scripts; new `closePaperTradeOrder`/`fetchOpenPositions` writers). The
  "brain for swing trading" — closes the position lifecycle Rule 15 left open. Scans every open
  `weather_position` and, using whatever fresher-than-entry `weather_probability_estimate` exists,
  decides HOLD or EXIT: `profit_target` when the edge has decayed below the same 5pp floor a new
  trade needs to clear (Rule 7, reused rather than a new invented constant — "alpha decay"),
  `stop_loss_model_inversion` when a real opposing edge emerges, or `stop_loss_temp_buffer` when a
  fresh forecast drifts back into Rule 12's buffer zone — checked FIRST, ahead of the edge, since a
  point-forecast drift is a more immediate danger signal. Auto-closes (writes `realizedPnlUsd`) on
  any exit signal — an explicit choice Joey confirmed rather than a signal-only design, since paper
  trading means auto-closing is zero-risk and is the only way a closed market becomes re-tradeable
  again (orderBuilder.ts's duplicate-exposure guard otherwise blocks it forever). **Live-verified**
  via two isolated synthetic test cases (inserted, run through the real code path, deleted — the 3
  real Seoul positions untouched throughout and confirmed still open after both tests): a decayed
  2pp-edge position correctly triggered `profit_target` with exactly-correct PnL math ($20.00 on a
  0.50→0.60 move); a strong +30pp-edge position with a forecast 0.9°F from a strike boundary
  correctly triggered `stop_loss_temp_buffer` instead — proving the priority order (danger signal
  beats a currently-strong edge) works as designed. All 3 real Seoul positions independently
  evaluated correctly as HOLD in both runs. 81 unit tests passing across the weather package.
  **Real gap named, not hidden**: exit timeliness is bounded by how often `checkMarkets.ts` is
  manually re-run — no scheduler exists yet for either script, so this isn't yet genuine real-time
  swing trading. Still not built: `verifySettlement.ts` (the live Wunderground/Playwright fetch —
  deliberately deferred as its own step), `detectAnomaly.ts`'s general bounds-check,
  `weather_pnl_snapshot`'s portfolio-level rollup. Full design in
  `docs/weather/WEATHER_ARCHITECTURE.md` / `docs/weather/WEATHER_RISK_MANAGEMENT.md`.
- **Database schema ownership:** TypeScript/Drizzle owns schema and migrations; Python
  (`db.py`) uses CRUD only — see `docs/copy-trading/SAFETY.md` §2.

## Phase 2 strategic scoring — status

Full detail (what each concept is, how it'd work mechanically, and the explicit shortfall-data
gate) now lives in **`docs/copy-trading/RISK_MANAGEMENT.md` Rule 12** — this file no longer duplicates it, to
avoid the two documents drifting apart. Short version: four concepts (risk-reward weighting,
domain-expertise filtering, conviction-sizing anomaly detection, tightening the recency gate)
are agreed in direction but explicitly **not started** — gated on having enough real
execution-shortfall data first, which is still accumulating.
