# Current State

**Audience note:** zero prior context assumed. This is a point-in-time snapshot — check the
"Last reviewed" date below before trusting a number in here over the live system (`bot.out.log`,
`data/app.db`, `bullpen status`).

**Last reviewed: 2026-07-19** (updated same-day: Weather Bot architecture docs added)

## Snapshot, right now

- **Mode:** paper trading only (`config.LIVE_MODE = False`) — no real funds at risk.
- **Tracked wallets:** 20, from the static list in `config.py`
  (`TRACKED_TRADERS_SOURCE = "static"`). The scored-database source (`"db"`) is built and tested
  but intentionally left off pending review — see `docs/RISK_MANAGEMENT.md` Rule 2.
- **Bot process:** running continuously, polling every 30 seconds.
- **Risk controls live:** per-trader circuit breaker, duplicate-exposure guard, portfolio
  exposure ceiling ($250 total / $30 per event), drawdown kill switch ($100 floor / $50 off
  peak), and the "Disciplined Taker" pre-trade slippage ceiling (5%, LIVE-mode only — see
  `docs/RISK_MANAGEMENT.md` Rules 3–6 and 11 for full detail). All portfolio-level checks are
  unit-tested (`python3 -m unittest test_risk_manager test_bot_risk_checks`, 26/26 passing as of
  this review).
- **Operational hardening (2026-07-19 evening):** every bullpen subprocess call now has an
  explicit hard timeout (60s default; the tracker-feed poll is tightened to 20s — see
  `docs/SAFETY.md` §3, "Whole-process freeze"), and repeated identical closeout-sweep fetch
  failures are throttled in the event log (first logged, then ~daily reminders with a running
  count — `docs/RISK_MANAGEMENT.md` Rule 7) after a transient bullpen outage produced hundreds
  of duplicate error rows. `dashboard.py`'s Start button now launches the bot with `-u`,
  matching the manual launch path. Known remaining gap, accepted for now: nothing external
  restarts or alerts on a dead `bot.py` process.
- **Wallet-research pipeline:** `scanLeaderboard.ts` / `scoreWallets.ts` are built, but a fresh
  re-run is currently **blocked** — bullpen's `data leaderboard` endpoint has been returning
  `NETWORK_TIMEOUT` (its `smart-money` endpoint is healthy, so this is a partial outage, not a
  full one). Don't re-run `pnpm scan:leaderboard` until that endpoint recovers.
- **Execution-shortfall data:** accumulating since 2026-07-18 (`bot.py`'s
  `measure_paper_shortfall`) — not yet at the "few weeks" volume the Phase 2 scoring plan is
  gated on. See `docs/RISK_MANAGEMENT.md` Rule 10.
- **Dashboards:** the built-in `dashboard.py` (port 8787) and the Next.js `apps/dashboard`
  Overview page both read live from `data/app.db`. The Next.js dashboard's other 8 planned pages
  aren't built yet.
- **Weather Bot:** architecture-only, no code yet (`packages/weather/` is still an empty
  scaffold) — but the full design is now documented in `docs/WEATHER_ARCHITECTURE.md` and
  `docs/WEATHER_RISK_MANAGEMENT.md`, written *before* any ingestion code per an explicit
  documentation-first requirement. Key decision: a live reconciliation check (METAR vs.
  Wunderground, same station/day) found real whole-degree-Celsius discrepancies, so
  Wunderground is the settlement oracle (fetched conservatively, on-demand only, no
  IP-evasion tooling) while NOAA/Open-Meteo/METAR feed prediction only — see
  `docs/WEATHER_RISK_MANAGEMENT.md` Rules 4-7 for the full reasoning.
- **Database schema ownership:** TypeScript/Drizzle owns schema and migrations; Python
  (`db.py`) uses CRUD only — see `docs/SAFETY.md` §2.

## Phase 2 strategic scoring — status

Full detail (what each concept is, how it'd work mechanically, and the explicit shortfall-data
gate) now lives in **`docs/RISK_MANAGEMENT.md` Rule 12** — this file no longer duplicates it, to
avoid the two documents drifting apart. Short version: four concepts (risk-reward weighting,
domain-expertise filtering, conviction-sizing anomaly detection, tightening the recency gate)
are agreed in direction but explicitly **not started** — gated on having enough real
execution-shortfall data first, which is still accumulating.
