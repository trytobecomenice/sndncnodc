# Current State

**Audience note:** zero prior context assumed. This is a point-in-time snapshot — check the
"Last reviewed" date below before trusting a number in here over the live system (`bot.out.log`,
`data/app.db`, `bullpen status`).

**Last reviewed: 2026-07-19** (updated same-day: Weather Bot architecture docs added)

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
  verified intact after. **Auto-onboarding built and live-verified** (2026-07-19, replacing a
  hardcoded per-station timezone map in `ingestMetar.ts` too): when `discoverMarkets.ts` finds an
  in-band market at an unknown station, it now resolves real coordinates from `aviationweather.gov`
  and a real timezone from `geo-tz` (offline lookup) automatically — no manual code change per
  city. Live-verified onboarding KLGA (NYC) and ZSPD (Shanghai) with correct real timezones
  (`America/New_York`, `Asia/Shanghai`) on first contact; `resolveTimezone` unit-tested against
  cities never hardcoded anywhere (London, Tokyo). `discoverMarkets.ts`'s live runs scanned 110
  real strike markets: 94 filtered by the odds band, 11 skipped as non-Wunderground (Hong Kong
  settles via the Hong Kong Observatory — a real, unanticipated finding), 5 genuinely in-band
  markets (3 NYC, 2 Shanghai) auto-onboarded and written as draft mappings, pending Rule 8 human
  review. Three more risk parameters formalized in the docs (not yet implemented — blocked on
  entry-rule logic and a capital-base constant that don't exist yet): Rule 11 (5% max capital per
  trade), Rule 12 (1.5°F minimum forecast-vs-strike buffer), Rule 13 (ensemble forecasting
  required, not a single deterministic model). `packages/weather`/`packages/copy-trading`
  isolation re-verified clean. Still not built: `verifySettlement.ts` (the live Wunderground/Playwright fetch — deliberately
  deferred as its own step), NOAA/Open-Meteo ingestion, probability computation, position
  management. Full design in `docs/weather/WEATHER_ARCHITECTURE.md` /
  `docs/weather/WEATHER_RISK_MANAGEMENT.md`.
- **Database schema ownership:** TypeScript/Drizzle owns schema and migrations; Python
  (`db.py`) uses CRUD only — see `docs/copy-trading/SAFETY.md` §2.

## Phase 2 strategic scoring — status

Full detail (what each concept is, how it'd work mechanically, and the explicit shortfall-data
gate) now lives in **`docs/copy-trading/RISK_MANAGEMENT.md` Rule 12** — this file no longer duplicates it, to
avoid the two documents drifting apart. Short version: four concepts (risk-reward weighting,
domain-expertise filtering, conviction-sizing anomaly detection, tightening the recency gate)
are agreed in direction but explicitly **not started** — gated on having enough real
execution-shortfall data first, which is still accumulating.
