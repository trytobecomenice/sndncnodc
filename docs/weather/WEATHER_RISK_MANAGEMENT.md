# Weather Bot — Risk Management & Rules

**Audience note:** zero prior context assumed, same standard as `docs/copy-trading/RISK_MANAGEMENT.md`. Every
rule below follows What it does / How it works mechanically / System costs & trade-offs / Why it
exists — precise enough to extend, tune, or challenge without re-deriving the reasoning.

**Status: early implementation.** This rules ledger was written *before* any code, per an explicit
documentation-first requirement, and is kept in sync as pieces get built — see each rule's "How it
works mechanically" for what's implemented and unit-tested today (`ingestMetar.ts`,
`pruneHistorical.ts`, `backfillHistorical.ts`, `emergencyCloseoutGuard.ts`,
`checkSettlementAgainstMetar.ts`, `discoverMarkets.ts`, `oddsFilter.ts`, `db/writers.ts`,
`stationReconciliation.ts`) versus what's still planned (Rules 11-13's
position-sizing/temperature-buffer/ensemble requirements,
`detectAnomaly.ts`'s general bounds-check, `verifySettlement.ts`'s live Wunderground fetch, and
all entry-rule/position-sizing logic). **This is the single source of truth for what rules will
apply to the Weather Bot** — check it
before proposing a new restriction so a decision already made here doesn't get silently
re-thought. `docs/weather/WEATHER_ARCHITECTURE.md` is the companion: data flow, schema, and scheduling
detail that explains *how* these rules get implemented mechanically.

This document is intentionally separate from `docs/copy-trading/RISK_MANAGEMENT.md` (the Copy Bot's rules
ledger) — per `.claudeprompt`, the two systems' rules, like their code and schema, must never be
treated as one system. Where a rule below is conceptually similar to a Copy Bot rule (e.g. paper
trading only), it's restated here in full rather than cross-referenced, so this document stands
alone.

---

## 1. Paper trading only

**What it does:** no real funds move, no private key is ever requested, stored, or signed by any
Weather Bot code, for the same reason and to the same absolute degree as the Copy Bot.

**How it works mechanically:** identical mechanism to the Copy Bot's Rule 1 — a single
`LIVE_MODE`-style flag gates every order path, defaulting to simulated fills recorded directly
into `weather_position`. All Polymarket-side execution, if this ever moves beyond paper, would be
delegated entirely to the external `bullpen` CLI (via `packages/bullpen-client`, already shared
with the Copy Bot) — this codebase has no code path capable of accepting or transmitting a private
key, for weather or copy-trading alike.

**System costs & trade-offs:** paper fills for a market that resolves in a single settlement event
(not a continuously-tradeable position like a Copy Bot paper trade) are simpler to reason about
than the Copy Bot's — there's no ongoing slippage/spread cost between paper and live to measure
the way `bot.py`'s shortfall tracking does, since a weather position typically opens once and
resolves once rather than being actively managed minute-to-minute.

**Why it exists:** same product requirement as the Copy Bot — this is v1, and v1 does not place
real trades, full stop. Any future move to live execution is a separate, deliberately reviewed
decision, never a side effect of another change.

---

## 2. Strict physical isolation — "100% Dependency Isolation," formal definition

**What it does:** the Weather Bot's code, database tables, and documentation live entirely
separately from the Copy Bot's. This rule has three concrete, checkable parts (defined explicitly
by Joey, 2026-07-19 — not left as a vague principle):

1. **Zero runtime imports.** `packages/weather` must never import any code or module from
   `packages/copy-trading`, and vice versa. They execute as completely separate, decoupled OS
   processes — no shared process, no shared in-memory state, ever.
2. **Isolated third-party dependencies.** Heavy or risky dependencies required only by weather
   work (e.g. Playwright, needed for the future Wunderground settlement fetch — Rule 5) are
   declared exclusively in `packages/weather/package.json`. `packages/copy-trading/package.json`
   must remain entirely untouched by weather-driven dependency additions, and vice versa.
3. **Table-level logic boundary.** Both packages intentionally share `@copybot/db` (the same
   physical SQLite file and one Drizzle schema file, `packages/db/src/schema.ts`) — that sharing
   is deliberate, approved infrastructure, not a violation. But weather business logic must never
   read or write a Copy Bot table (`wallet_profile`, `paper_trade`, `leaderboard_scan`,
   `decision_journal`, `rule_set`, `bot_event_log`, or any other non-`weather_*` table) — it is
   strictly limited to the 7 `weather_*` tables.

**How it works mechanically:** all Weather Bot code lives in `packages/weather/` only — never
`packages/copy-trading/`, never any Python Copy Bot file (`bot.py`/`db.py`/`config.py`/
`bullpen_client.py`). The 7 weather DB tables (`weather_station` through `weather_pnl_snapshot`,
`packages/db/src/schema.ts` lines 309-411) are structurally separate from every Copy Bot table —
no shared rows, no shared `rule_set`/`rule_change` reuse (those are shaped around copy-trading
concepts like mute streaks and TTP activation; reusing them would blur the boundary this rule
exists to keep sharp). **Verified compliant as of `ingestMetar.ts` (2026-07-19)**: a repo-wide
grep found zero references from `packages/weather/` to `packages/copy-trading/` or to any
Copy-Bot-specific table name, and zero references the other direction — `packages/weather/package.json`
currently depends on only `@copybot/db` + `drizzle-orm`, nothing from `packages/copy-trading`.
This check should be re-run (a plain grep is sufficient — see the parenthetical above) any time a
new weather script is added, not assumed to stay true automatically.

**System costs & trade-offs:** this means some genuine duplication — the Weather Bot can't reuse
the Copy Bot's `rule_set` versioning table, circuit-breaker patterns, or portfolio risk manager
even where the underlying need (versioned thresholds, a kill-switch pattern) is conceptually
similar; each gets its own weather-specific implementation. Isolated dependencies (part 2) also
means the two packages' `node_modules` will diverge over time (e.g. only weather ever pulls in
Playwright, a heavy dependency) — accepted cost, not an oversight, and arguably a benefit: a
Copy Bot install never pays for weather-only dependencies, and a security issue in one package's
dependency tree can't implicate the other's.

**Why it exists:** `.claudeprompt`'s standing instruction: "Keep these domains separated at all
times. Never mix Copy Bot logic with Weather Bot logic, and never let their database tables or
schema evolve as if they were one system." Restated here as explicitly non-negotiable, not merely
a default, per direct instruction — a future contributor should never need to infer this boundary
from context.

---

## 3. Lean historical data (rolling 1-2 year retention)

**What it does:** `weather_historical_observation` never accumulates more than 1-2 years of data
per station — no multi-year mass backfill, ever.

**How it works mechanically: implemented and live-verified (2026-07-19)** —
`packages/weather/src/pruneHistorical.ts` (`pnpm --filter @copybot/weather prune:historical`)
deletes `weather_historical_observation` rows with `obs_date` older than
`HISTORICAL_RETENTION_YEARS = 2` (the wider end of the agreed 1-2yr range — climatology gets more
reliable with more history, and this stays a small table regardless), and separately prunes
`weather_forecast_snapshot` rows whose `forecast_for` date is more than
`FORECAST_SNAPSHOT_RETENTION_DAYS = 60` in the past (a forecast has zero value once its target
date has already resolved — a much shorter useful lifespan than a historical observation, so it
gets its own, tighter cutoff). No schema change required — this is a data-lifecycle policy
enforced by a scheduled job, not a database constraint. Verified end-to-end: run against the real
`data/app.db`, correctly left a genuine 2026-07-18 row untouched, then correctly deleted a
deliberately-inserted 2020-01-01 test row while leaving everything else intact. Not yet wired to
a scheduler (no `launchd` job exists in this repo today, per `docs/weather/WEATHER_ARCHITECTURE.md`'s
execution-cycle table) — run manually until that's built.

**Global historical backfill, implemented and live-run across the full fleet (2026-07-19).**
`packages/weather/src/backfillHistorical.ts` (`pnpm --filter @copybot/weather backfill:historical`)
builds the actual climatology "memory" this rule's retention window exists to bound — one real
2-year daily min/max row per station, for every onboarded station, not just the one or two spot-
checked earlier. **This required a data-source change, discovered live, not assumed:**
`aviationweather.gov`'s `/api/data/metar` endpoint — `ingestMetar.ts`'s source — was tested
directly against a multi-year request and found to have a hard ceiling around **8-9 days** of
real history (its `hours` parameter tops out at 750 before the API rejects the request, and even
within that it silently caps at ~400 records) — it's a live "recent conditions" feed, not an
archive, and no amount of pacing or chunking gets around a server-side limit. The real free,
public source for multi-year historical METAR is the **Iowa Environmental Mesonet's ASOS archive**
(`mesonet.agron.iastate.edu`, Iowa State University) — verified live to return a station's entire
2-year window in ONE request (44,626 raw readings for RKSI alone), and confirmed to accept the
same ICAO station code unchanged for both US and non-US stations (IEM's response labels drop the
leading "K" for US airports — e.g. `KLGA` → `LGA` — but that's cosmetic only; the *query* uses the
same code weather_station already stores, no remapping needed). Results are written with the same
`source: "metar"` tag `ingestMetar.ts` uses (same underlying METAR data, different access point),
sharing the identical `(station_id, obs_date, source)` upsert target. Daily aggregation uses each
station's own **real local timezone** (already resolved via Rule 8's auto-onboarding) rather than
`ingestMetar.ts`'s UTC-day simplification — a genuine correctness improvement, worth doing here
since this backfill is meant to be the trustworthy long-term record, not just a fast nowcast input.

**Live result, full fleet**: all 43 onboarded stations backfilled in one run, zero failures,
31,384 total daily rows (~730 days × 43 stations, a handful of stations landing slightly under —
708-728 days — from genuine gaps in their real historical record, not a bug: every other station
landed at exactly 730-731). **Idempotency verified two ways**: (1) `onConflictDoUpdate` on the
existing unique index means a re-run can never create a duplicate row, confirmed by direct query
after re-running (zero `(station_id, obs_date)` pairs with `count > 1`); (2) a resume optimization
(`countExistingDays`) skips a station's network request entirely if it's already within 2 days of
full coverage, confirmed on immediate re-run — 39 of 43 stations were skipped with zero requests
made, only the handful with genuine data gaps re-checked (harmless — IEM re-serves the same cached
range cheaply, no new rows result, verified by row count staying at exactly 31,384 across re-runs).
**Pacing, taken seriously because it had to be**: this script triggered IEM's rate limiter
("Too many requests from your IP address, slow down.") after just two rapid manual test requests
during development — `fetchStationCsv` detects that exact response text and backs off 60s (up to 3
attempts) rather than treating it as a hard failure, on top of a 5s base delay between the 43
station requests (more conservative than the 2-3s originally proposed, a deliberate choice after
seeing how easily the limiter triggered). The full 43-station run needed zero rate-limit backoffs
in practice — the 5s pacing was sufficient — but the detection/backoff path exists and would
trigger automatically if IEM's tolerance ever tightens.

**Window discipline**: exactly 2 years, not the 2.5 originally requested — reuses
`pruneHistorical.ts`'s own `historicalObservationCutoff()` function directly (not a duplicated
constant) specifically so the backfill window and the prune cutoff can never drift out of sync;
backfilling past the documented retention window would just have had `pruneHistorical.ts` delete
the oldest data right back out the next time it runs.

**System costs & trade-offs:** 1-2 years is enough for a meaningful climatology baseline
(day-of-year historical distribution) but explicitly not enough for long-cycle climate pattern
analysis (e.g. decadal trends, ENSO-cycle effects) — if a future version of the probability model
wants that, this retention policy would need deliberate revisiting, not silent expansion.

**Why it exists:** keeps `weather_historical_observation` small and fast regardless of how many
stations get onboarded over time — a deliberate simplicity choice, not a storage-cost necessity
(this is a small SQLite table by any measure at this scale); the point is keeping the table's
growth bounded and predictable as the station count grows, rather than letting query performance
quietly degrade a year from now.

---

## 4. Extreme Data Anomaly Protection ("Sold-Out Switch")

**What it does:** any weather reading that represents a physically impossible jump from recent
readings (e.g. a station report spiking 10°C above its own trailing readings with no plausible
cause) is treated as a fatal data anomaly — never a trade signal, never even trusted enough to
reach the probability model.

**How it works mechanically:** a pure bounds-check function, `detectAnomaly(newReading,
recentReadings, station)` in `detectAnomaly.ts` (not yet built — planned, see Roadmap), will sit
in front of every write to `weather_historical_observation`/`weather_forecast_snapshot` and every
settlement read from `verifySettlement.ts`, comparing the new value against a plausible
delta-per-interval bound for that station/season. On trip: (1) the anomalous reading is never
written as trusted data, (2) any pending order for a position touching that station is aborted
immediately, (3) an existing open position at that station is escalated to Emergency Closeout.
Same architectural shape as the Copy Bot's portfolio kill switch (`risk_manager.py`'s
`evaluate_equity`/latch pattern) — a tripped condition acts immediately rather than waiting for a
human to notice.

**Emergency Closeout is NOT an unconditional market sell — implemented and unit-tested
(2026-07-19), refining the original wording of this rule.** `checkEmergencyCloseoutSlippage()` in
`packages/weather/src/emergencyCloseoutGuard.ts` gates the sell itself: it compares the current
executable price against a reference price (the position's last known-good mark, never the
anomalous reading itself) and blocks the sell if either (a) the executable price sits below an
absolute floor of **$0.05** (a near-zero quote usually means "no real bid," not "fair value is
low"), or (b) the adverse move from the reference price exceeds a **5% slippage ceiling**. Both
numbers are Joey's explicit defaults (2026-07-19), configurable, not yet calibrated against real
market behavior. **If the check blocks the sell, the position is NOT force-closed** — it must be
flagged for urgent manual review instead (same fail-closed principle as Rule 6), since dumping
into an illiquid book can realize a worse loss than the anomaly the switch exists to protect
against. This mirrors bot.py's `check_slippage_ceiling` in spirit but is deliberately a different
function for a different context: the Copy Bot's principle is "never gate a sell, only a buy"
(a risk layer that traps you in a losing position adds risk) — this rule's explicit instruction is
the opposite for THIS specific mechanism, because an anomaly-driven emergency exit from a
possibly-illiquid, one-off weather market carries a different risk shape than a routine Copy Bot
exit in an already-liquid Polymarket sports/politics market. 8 unit tests, all passing
(`pnpm --filter @copybot/weather test`), including the exact boundary case (5% precisely) and the
absolute-floor case independent of relative slippage.

**System costs & trade-offs:** a legitimate, fast-moving real weather event (e.g. a genuine rapid
frontal passage) could in principle trip the anomaly gate and force an unnecessary early close —
the trade-off is deliberately accepted: false positives that close a position early are
recoverable mistakes, a bad sensor reading feeding a real trade is not. The slippage ceiling adds
a second trade-off on top: a blocked emergency sell means the position stays open (under manual
review) for longer than "immediate" during exactly the conditions that triggered the anomaly in
the first place — accepted because dumping blind into an illiquid book was judged the worse
outcome. The anomaly bound thresholds themselves (the `detectAnomaly.ts` piece, not yet built)
still need calibration once real station data exists.

**Why it exists:** direct requirement — this is a hard, non-negotiable defense against a single
bad data point (sensor glitch, upstream API bug, a value literally swapped between Fahrenheit and
Celsius somewhere in a pipeline) silently producing a real position or blocking a real exit. This
reuses the exact same mechanism to protect against a second failure mode too — see Rule 6, a
scraper silently returning a wrong-but-plausible-looking settlement value trips this same gate.

---

## 5. Settlement source authority: Wunderground, verified narrowly

**What it does:** for whole-degree-bucket markets, the exact settlement number is read directly
from Wunderground — not approximated from any other source — but only via a conservative,
on-demand fetch, never a continuously-polled one, and never using tooling built to evade
Wunderground's own anti-abuse defenses.

**How it works mechanically:** a same-day reconciliation check (documented in full in
`docs/weather/WEATHER_ARCHITECTURE.md`) found that NOAA's free METAR feed and Wunderground's own reported
high/low for the identical airport station disagreed by enough to cross a whole-degree-Celsius
bucket boundary, on both the day's high and low. Given markets settle on exactly these buckets, an
approximate source is not acceptable for settlement — so `verifySettlement.ts` (Playwright,
rendering `wunderground.com`'s pages exactly as a normal browser would, since the site is a
client-rendered SPA with no data in static HTML) is the only trusted settlement read. It is called
in exactly two places: once per new market mapping (confirming the settlement station reads
sensibly before `is_active` is ever set true), and once per open position, shortly before its
market resolves, as the final pre-closeout check. It is **never** wired to a recurring
`launchd` job or polling loop.

**Explicitly excluded**: proxy rotation, IP-rotation, or user-agent spoofing — any tooling whose
purpose is evading Wunderground's rate-limiting or bot-detection. This was a direct decision, not
an oversight: reading a public page occasionally, as a normal browser, is a materially different
act from actively engineering around a site's anti-abuse defenses, and building the latter was
declined for a pipeline that drives real position decisions. If Wunderground data is ever needed
at higher volume/reliability than this conservative cadence supports, the correct path is a paid
enterprise API contract (IBM/The Weather Company), not evasion engineering.

**System costs & trade-offs:** the conservative, on-demand-only cadence means this fetch is rare
by construction — which also means it's the least-battle-tested code path in the whole system,
and the one most likely to have degraded silently (site redesign, changed selectors) by the time
it's actually needed. See Rule 6 for the direct mitigation. There is also a real chance
Wunderground blocks or rate-limits even this conservative usage over time, since no arrangement
with them exists — this is an accepted risk of relying on unlicensed access to their public page,
not a solved problem.

**Why it exists:** the reconciliation finding made clear that "close enough" sources produce
settlement-flipping errors on cliff-edge binary markets — a mistake with 100%-payout-swing
consequences, not gradual mispricing. Reading the actual oracle, conservatively, is the direct
fix; evading their defenses to read it more often was assessed as not worth the operational and
reputational risk for the marginal benefit, given the fetch only needs to succeed rarely by design.

---

## 6. Fail-closed settlement verification

**What it does:** if `verifySettlement.ts` fails, times out, gets blocked, or returns an
implausible value, the bot never guesses — it flags for manual review and never force-executes a
closeout on missing or suspicious data.

**How it works mechanically:** two distinct failure modes are both handled by refusing to proceed
automatically: (1) an outright fetch failure (network error, timeout, page structure the parser
doesn't recognize) — the position is flagged, not closed, and does not fall back to
METAR/Open-Meteo as a silent settlement substitute (doing so would silently reintroduce the exact
basis-risk gap Rule 5 exists to close); (2) a fetch that *succeeds* but returns an implausible
value (wrong day, wrong station, a stale cached number, or the effect of an undetected site
redesign) — every settlement read is sanity-checked against an independent band before being
trusted, and a reading outside that band trips the Rule 4 anomaly gate rather than being accepted
as truth.

**Dual Oracle Cross-Check — the concrete implementation of failure mode (2)'s sanity check, built
and unit-tested (2026-07-19).** `checkSettlementAgainstMetar()` in
`packages/weather/src/checkSettlementAgainstMetar.ts` compares the scraped Wunderground reading
against the co-located METAR reading for the **same station, same hour** — if they disagree by
more than **4°F** (`MAX_HOURLY_CROSS_CHECK_DELTA_F`, Joey's stated threshold), the anomaly gate
trips immediately rather than trusting the Wunderground read. This threshold is deliberately
tighter than the ~1-2°F systematic gap the reconciliation PoC measured on a DAILY high/low
comparison (different instruments/aggregation windows genuinely can differ somewhat over a full
day) — an HOURLY, same-instant comparison has no such aggregation-window excuse, so a large gap
here is a much stronger "something broke" signal, not normal micro-variation. Verified: the
function correctly PASSES when given this session's actual PoC-scale gap (1.2°F) replayed as an
hourly comparison, confirming the 4°F threshold doesn't false-positive on exactly the magnitude of
discrepancy already proven to occur between these two sources. **Scope note**: this is pure
comparison logic only, built and tested against synthetic inputs — it does not yet fetch a real
Wunderground reading itself. Wiring it to a live read is `verifySettlement.ts`'s job (Playwright,
still not built — deliberately deferred to its own dedicated review before it touches the live
site, per Rule 5's framing of that fetch as the more sensitive piece).

**System costs & trade-offs:** "flag for manual review" means a human (Joey) is the fallback for
every settlement-verification failure — this doesn't scale to a large number of simultaneous
positions without either accepting slower resolution on some of them or building a more automated
escalation path later. Acceptable at current, small scale; worth revisiting if position count
grows meaningfully.

**Why it exists:** this is the second half of Rule 5's PoC-driven caution — Rule 5 established
that reading the wrong number is worse than the METAR gap already proved is possible; this rule
establishes that reading *no* number, or an unverified one, must never be treated as equivalent to
reading the right one. A closeout decision based on a guess is exactly the failure mode the entire
reconciliation exercise was run to prevent.

---

## 7. Residual basis-risk margin

**What it does:** even after switching to Wunderground as the settlement oracle, positions on
bucket-boundary markets require a minimum edge margin *beyond* zero before they're considered
tradeable — the model is never allowed to treat "we read Wunderground" as "basis risk is zero."

**How it works mechanically (future — entry-rule logic not yet built):** `weather_probability_estimate.inputs_json`
carries an explicit residual-basis-uncertainty note per estimate. `managePositions.ts`'s
(not-yet-designed) entry rule must require `edge` to clear a floor derived from the ~1-2°F gap the
reconciliation PoC actually measured — a concrete, evidence-based lower bound on the margin
required, not a hypothetical safety factor picked out of the air.

**System costs & trade-offs:** this margin requirement will mean passing on some marginal-edge
trades that a naive model (treating the settlement read as ground truth with zero uncertainty)
would take — fewer trades, a real opportunity cost, in exchange for not repeating the specific
overconfidence the PoC caught.

**Why it exists:** there is no confirmation that Polymarket's own resolution/dispute process reads
the identical Wunderground endpoint, at the identical instant, with the identical rounding, that
`verifySettlement.ts` reads. Rule 5 meaningfully reduces the basis-risk gap the PoC found; it does
not provably close it to zero. Treating a de-risked source as a risk-free one would be the same
category of mistake this whole rules ledger exists to avoid — just one step removed.

---

## 8. Market-mapping human review

**What it does:** every new Polymarket weather event gets its settlement station reviewed and
approved by a human once, before any of its markets can be traded — never fully automatic.

**How it works mechanically: implemented and live-run against real Polymarket data (2026-07-19).**
`packages/weather/src/discoverMarkets.ts` (`pnpm --filter @copybot/weather discover:markets`) runs
`bullpen polymarket discover --category weather` and, for each event, writes one
`weather_market_mapping` row per surviving strike market (Rule 10's odds filter applies first —
see below) — all sharing one settlement-station pairing per event, so the human review happens
once per **city/event**, not per individual degree-bucket market, even though each strike market
gets its own row (Polymarket groups many binary strike markets under one event, e.g. Seoul's
"highest temperature" event contains 11 separate whole-degree markets). Every write goes through
`upsertMarketMapping`, which always defaults `is_active` to `false` — no market trades off an
unreviewed mapping. **Real finding from the first live run**: not every weather event settles via
Wunderground — Hong Kong's event cites the Hong Kong Observatory directly. `discoverMarkets.ts`
detects this per-event (checking for `wunderground.com` in the description) and skips
non-Wunderground events entirely rather than mislabeling their `settlement_source`; handling other
settlement authorities is a real, separate future capability. **Station coordinates are never
fabricated, and unknown stations are auto-onboarded, not hardcoded (upgraded 2026-07-19).**
Wunderground's event descriptions name a station and parse to an ICAO-style code (from the cited
URL — verified the URL's path DEPTH varies by country, e.g. Asian cities use
`.../kr/incheon/RKSI` but NYC uses `.../us/ny/new-york-city/KLGA`, so the parser matches the
trailing segment, not a fixed path depth) but never give lat/lon directly. Originally, a market's
mapping was only written if an existing `weather_station` row for that external ID already had
coordinates to borrow — meaning any city not already manually onboarded via `ingestMetar.ts` was
silently skipped forever, which does not scale as Polymarket adds new city markets continuously
(Joey, 2026-07-19: "Tomorrow it might be London or Tokyo, and the bot will get stuck again"). Now,
`resolveWundergroundStation()` calls `stationReconciliation.ts`'s `resolveStationMetadata()` to
fetch real lat/lon/name from `aviationweather.gov` (the same free source `ingestMetar.ts` already
uses) and derive a real IANA timezone from those coordinates via `geo-tz` (an offline lookup
library — no network call, no manual per-station map). **Corrected again, same day**: the first
version of this fix only resolved a station when one of ITS OWN markets happened to pass the odds
filter at scan time — Joey caught that this still left most cities unknown, since a city's markets
are only briefly in-band and a scan that misses that window would never learn the station at all
("it shouldn't only have 2 [stations]"). Station knowledge is now fully decoupled from Rule 10:
every Wunderground-sourced event's station gets resolved/onboarded on every scan regardless of
current odds — Rule 10 still gates which *markets* get written to `weather_market_mapping` (a real
capital/attention decision), but never gates which *stations* this system simply knows about,
since station metadata alone commits no capital and carries no risk.

**Full-scale live verification (2026-07-19), after Joey pointed out the discovery scan itself was
also artificially narrow.** The default `--limit` passed to `bullpen polymarket discover` was 10 —
confirmed live that the API actually caps at **100 events per call regardless of a higher limit
requested** (tried 200, got 100 back), and those 100 events span **48 distinct cities**, not the
~8 a `--limit 10` scan could ever see. Default bumped to 100 (the real ceiling, not an arbitrary
round number). Re-run at full scale: **1,062 real strike markets** scanned across all 100 events,
**35 additional stations auto-onboarded in one pass** (43 total), **181 draft market mappings**
written pending Rule 8 review — all in ~32 seconds. Every single station resolved to correct real
names/coordinates/timezones spanning every inhabited continent (e.g. `Asia/Kolkata` for Lucknow,
`America/Argentina/Buenos_Aires` for Buenos Aires, `Africa/Johannesburg` for Cape Town,
`Asia/Singapore` for Kuala Lumpur — Malaysia has no separate `Asia/Kuala_Lumpur` zone), none of
which any manual per-city map would have gotten right by accident. Re-running confirmed idempotent
(0 newly onboarded, same 181 mappings). The Polymarket "Weather" category also surfaced 6
non-city-temperature markets (air quality index, earthquake counts, a hantavirus-pandemic market,
"hottest year on record") that share the same binary-outcome JSON shape but obviously aren't
Wunderground-settled — these fall through the existing non-Wunderground skip path with zero
special-casing needed, confirming that path is robust to market shapes beyond just temperature
buckets. Still never fabricated: if a parsed code isn't a real METAR-reporting station, resolution
returns `null` and the market is skipped and logged, exactly as before.

**System costs & trade-offs:** reviewing once per city/event (rather than once per individual
degree-bucket market) trades a small amount of theoretical rigor for a large reduction in manual
effort — one city/day event can contain 5-10+ strike markets, and the risky judgment call (which
physical station to trust) doesn't vary market-to-market within one event, so per-market review
would be repeated effort without added safety.

**Why it exists:** the reconciliation PoC is itself proof that "this station name looks like a
match" is not a safe automated assumption — a human needs to make that call, at least until this
system has enough of a track record to justify loosening the gate.

---

## 9. No foreign-key constraints (application-level guards instead)

**What it does:** the 7 weather tables have zero database-level foreign-key constraints, matching
every other table in this schema — referential correctness (e.g. "does this `station_id` actually
exist?") is enforced in application code, not by SQLite.

**How it works mechanically: implemented (2026-07-19), corrected from this rule's original
description.** `packages/weather/src/db/writers.ts` (built once a second script,
`discoverMarkets.ts`, also needed to write `weather_station` rows — the exact trigger
`ingestMetar.ts`'s original comment said would justify it) holds `findStationByExternalId`,
`upsertWeatherStation`, and `upsertMarketMapping` — the single-writer-funnel pattern the Copy
Bot's `upsertWalletProfile` already uses. The actual existence guard is structural rather than a
separate named `assertStationExists` function (this rule originally described one that wasn't
actually built that way): `upsertMarketMapping` requires a real `stationId`, and callers (e.g.
`discoverMarkets.ts`) obtain that ID only from `upsertWeatherStation`/`findStationByExternalId` —
there is no code path that can write a mapping row with a fabricated or unverified station
reference. **A real bug this design already caught**: `weather_station.external_id` originally had
a single-column `UNIQUE` constraint (not composite with `source`), silently contradicting this
codebase's own documented "one row per source per real-world location" design
(`docs/weather/WEATHER_ARCHITECTURE.md` §1). It surfaced the moment two different sources actually
referenced the same station (`discoverMarkets.ts` trying to create a `wunderground` row for a
station `ingestMetar.ts` had already onboarded as `metar`) — a live `UNIQUE constraint failed`
error, not a silent corruption. Fixed via a real migration (`packages/db/drizzle/0002_chief_johnny_blaze.sql`):
dropped the single-column unique index, added a composite one on `(external_id, source)`. Existing
data survived the migration untouched (verified).

**System costs & trade-offs:** SQLite only enforces FK constraints with `PRAGMA foreign_keys = ON`
per-connection — turning that on would make any out-of-order write (e.g. a backfill script racing
ahead of its station row landing) a hard failure instead of an inert loose reference, real
rigidity for a system with several independent ingestion scripts that can plausibly run out of
strict order. The structural-guard approach catches the same class of bug (a typo'd or
deleted-in-error station ID) without that rigidity cost — though, as the composite-index bug
above shows, "no FKs" also means schema-level mistakes (like a too-strict unique constraint) don't
get caught until real, live multi-writer usage exercises them. Worth remembering as a general
caution about this rule, not just a one-time fix.

**Why it exists:** consistency with the rest of this codebase (zero `.references()` calls exist
anywhere in `packages/db/src/schema.ts` today, Copy Bot tables included) — introducing FKs only
for weather tables would make this domain structurally inconsistent with everything else in the
same schema file for no proportionate benefit, since nothing in this system hard-deletes rows.

---

## 10. Extreme Odds Filter

**What it does:** the bot only ingests/tracks strike markets whose current implied probability
("Yes" outcome price) is between **10% and 90%** — anything outside that band is skipped
entirely, never written to `weather_market_mapping`.

**How it works mechanically: implemented and unit-tested (2026-07-19).**
`checkOddsFilter()` in `packages/weather/src/oddsFilter.ts` (`MIN_IMPLIED_PROB = 0.1`,
`MAX_IMPLIED_PROB = 0.9`, both Joey's stated defaults) is applied by `discoverMarkets.ts` to every
individual strike market's "Yes" probability before any DB write is even considered — a market
failing this filter never reaches the station-lookup or mapping-write logic at all. 8 unit tests
pass, including exact-boundary cases and both live-observed real values (0.0005 and 0.99 from an
actual `bullpen polymarket discover` run). **Confirmed doing real work on live data**: of 110
real strike markets scanned in the first live run, 94 were filtered out by this rule alone — most
Asian city markets that day had already collapsed to near-certainty on one bucket (the day was
nearly over, the actual high effectively already known), exactly the "capital-inefficient
steamroller" pattern this rule exists to screen out. Three genuinely contested NYC markets (Yes =
0.165, 0.525, 0.26) and two Shanghai low-temperature markets (0.166, 0.808) passed the filter —
the real, live proof the band finds real trading-band opportunities, not just filters everything.

**System costs & trade-offs:** a market can cross into or out of the 10-90% band as new
information arrives (e.g. a forecast update) — this rule is evaluated at discovery/scan time, not
continuously, so a market that enters the band between scans isn't picked up until the next
`discoverMarkets.ts` run, and one that exits the band isn't automatically dropped from
`weather_market_mapping` (no code yet un-tracks a previously-written mapping — a re-scan just
never re-writes it, it doesn't delete it). Whether stale, now-out-of-band mappings should be
actively pruned is an open question, not yet addressed.

**Why it exists:** direct instruction (Joey, 2026-07-19) — deep out-of-the-money markets (<10%)
are lottery tickets (thin edge-per-dollar-of-risk); deep in-the-money markets (>90%) are
capital-inefficient (a bet there ties up capital for a payout ratio too small to matter). The bot
should spend its attention and capital strictly inside the actively-contested trading band.

---

## 11. Position sizing cap (Kelly-inspired)

**What it does:** no single market may ever receive more than **5% of total capital**
(`MAX_CAPITAL_PER_TRADE = 0.05`) in one trade.

**How it works mechanically (planned — formalized 2026-07-19, not yet implemented):** this
requires `managePositions.ts`'s entry-rule logic (not yet built) and a defined capital base for
the Weather Bot (also not yet defined — the Copy Bot has `PAPER_BANKROLL_USD`; the Weather Bot has
no equivalent constant yet, a real prerequisite this rule is blocked on). Once both exist, sizing
a new position must clamp `positionSize <= 0.05 * totalCapital`, evaluated at the moment of entry
against current capital (realized + unrealized), the same equity-computation shape as the Copy
Bot's `risk_manager.py compute_equity`.

**System costs & trade-offs:** 5% is a hard ceiling per market, not a Kelly-criterion-computed
optimal fraction — true Kelly sizing would vary per trade based on edge and odds, which needs a
calibrated edge estimate this system doesn't have yet (Rule 7's residual basis-risk margin and
Rule 12's temperature buffer are both prerequisites for a trustworthy edge number). Treat this 5%
as a conservative ceiling that a future real Kelly calculation should sit BELOW, not as the Kelly
fraction itself.

**Why it exists:** direct instruction (Joey, 2026-07-19) — no single weather event's outcome,
however well-modeled, should be able to inflict capital loss disproportionate to one bet's worth
of information. Mirrors the Copy Bot's per-event exposure cap (`docs/copy-trading/RISK_MANAGEMENT.md`
Rule 6) in spirit — a portfolio-concentration guard — but expressed as a percentage of total
capital rather than a fixed dollar amount, appropriate for a system whose capital base isn't fixed
yet the way the Copy Bot's `PAPER_BANKROLL_USD` is.

---

## 12. Minimum forecast margin of safety (jump risk defense)

**What it does:** the bot must unconditionally skip a trade if the gap between the Polymarket
strike boundary and the model's own forecast point estimate is less than **1.5°F**
(`TEMP_BUFFER = 1.5`).

**How it works mechanically (planned — formalized 2026-07-19, not yet implemented):** requires
`computeProbability.ts` (not yet built). At evaluation time, for a given strike boundary (e.g.
"80°F or above"), compute `abs(forecastPointEstimateF - strikeBoundaryF)`; if that's below
`TEMP_BUFFER`, the trade is skipped regardless of how favorable `edge` (Rule 7) otherwise looks —
this check happens BEFORE the edge/margin check, not instead of it. Needs explicit unit handling:
some markets bucket in whole-degree Celsius (most Asian cities observed so far), others in 2°F
bands (NYC) — the comparison must happen in one consistent unit (°F, per how this rule is stated)
with both sides converted consistently, not compared raw.

**System costs & trade-offs:** this is a DIFFERENT margin from Rule 7's residual basis-risk
margin — Rule 7 protects against the settlement READ being imprecise (scraper/oracle
disagreement); this rule protects against the FORECAST itself being imprecise (a point forecast is
never exactly right, and a strike sitting inside the forecast's own normal error band is a coin
flip dressed up as an edge). Both margins likely need to be cleared simultaneously before a trade
is allowed — a real design detail for whoever builds `computeProbability.ts`/`managePositions.ts`,
not resolved further here. A tight buffer band around any given strike will mean some real edge
gets left on the table; accepted, since trading inside forecast noise isn't edge at all.

**Why it exists:** direct instruction (Joey, 2026-07-19) — "jump risk": a forecast can be
directionally right and still land on the wrong side of a hard whole-degree resolution boundary
by a small, ordinary amount of forecast error. Requiring real separation between the forecast and
the strike protects against exactly that failure mode, distinct from (and in addition to) the
basis-risk margin in Rule 7.

---

## 13. Ensemble oracle requirement

**What it does:** the bot's forecast input must come from an **ensemble of models**, not a single
deterministic forecast — used specifically to surface fat-tail/low-probability scenarios a single
point forecast would hide.

**How it works mechanically: architecture decided and verified live, not yet implemented
(2026-07-20).** `ingestOpenMeteo.ts` (not yet built) will call Open-Meteo's dedicated Ensemble API
(`https://ensemble-api.open-meteo.com/v1/ensemble`, free non-commercial tier, no API key) —
verified directly against the live endpoint rather than trusting secondhand documentation, which
caught a real error: the commonly-cited ECMWF model identifier `ecmwf_ifs04` silently returns only
a single non-ensemble series with no error — the actual working identifier is **`ecmwf_ifs025`**
(51 members: 1 control + 50 perturbed, confirmed live). Per Joey's decision (2026-07-20), both
`ecmwf_ifs025` (51 members) and `gfs_seamless` (31 members) are fetched — 82 combined members per
station/day, a genuine multi-model ensemble rather than one model family's internal spread, a more
robust fat-tail defense than either alone. Response member keys follow `temperature_2m` (control)
/ `temperature_2m_member01`...`memberNN` (perturbed) — verified against real live data, not
assumed. Individual members are retained only 3 days server-side, so this system's own database is
the only durable record of what the ensemble said — critical for later validating forecast
accuracy against actual outcomes. Storage: a new table, `weather_ensemble_forecast` (see
`docs/weather/WEATHER_ARCHITECTURE.md` §2 for the exact column design) — chosen over cramming the
distribution into `weather_forecast_snapshot.rawJson` specifically so `computeProbability.ts` can
later answer "what fraction of members hit the target bucket" with a direct SQL `COUNT`, not JSON
parsing at read time.

**Built and live-verified across all 43 stations (2026-07-20)**: 35,260 rows in one run (exactly
82 members × 10 days × 43 stations), zero failures, chunked bulk `onConflictDoUpdate` upserts
(300 rows/statement) confirmed via `EXPLAIN QUERY PLAN` to actually use the lookup index, not scan
the table. A real cross-model disagreement surfaced immediately: RKSI's next-day forecast has
`ecmwf_ifs025` putting 11/51 members (~22%) at or above 80°F versus `gfs_seamless` putting only
1/31 (~3%) — precisely the kind of signal a single deterministic model would have hidden, which is
the entire justification for this rule.

**System costs & trade-offs:** real, quantified, not hand-waved — confirmed ~35,000 rows per
refresh cycle (not an estimate: the live run hit 35,260 exactly), at the same 3-6h cadence as the
existing forecast snapshot job. **Retention gap closed same-day (2026-07-20) by
`pruneForecasts.ts`** — see Rule 3-style treatment below (this is really an addendum to Rule 3's
retention philosophy, applied to this table specifically): keeps only the latest `issuedAt`
generation per (station, model), deleted via one atomic correlated-subquery `DELETE`. Live-proven
against the real accumulated 70,520-row dataset: pruned to exactly 35,260, then confirmed
idempotent (re-running when only one generation remains deletes 0 rows, not everything). Fetching
two models instead of one roughly doubles both the row volume and the API calls per refresh cycle
(2 calls per station instead of 1) — accepted specifically because tail-risk representation was
judged worth the cost, the same trade-off already justified below.

**Probability calculation — built and live-verified (2026-07-20), `calculateProbability.ts`.**
Queries the latest ensemble generation per model for a station/day and computes what fraction of
members land inside a target range — combined AND broken out per model, deliberately never
collapsed into one blended number by default, since hiding cross-model disagreement would defeat
this rule's entire purpose. **Real, live results**: RKSI's next-day ≥80°F probability came back
combined 31.7% (26/82) — but `ecmwf_ifs025` said 19.6% while `gfs_seamless` said 51.6%, a 32-point
spread on the same question. KLGA's 78-79°F bucket was starker still: `ecmwf_ifs025` at 31.4%
versus `gfs_seamless` at a flat 0.0%. Pure hit-rate math (`computeHitRate`) is unit-tested
independent of the DB, including a direct reproduction of the RKSI 22%/3% finding from the first
ingestion run. **Scope, deliberate**: this module is the probability math only — it does not yet
parse a market's actual temperature bucket from its `market_slug` (no stored min/max columns
exist on `weather_market_mapping` yet) and does not yet write to `weather_probability_estimate`
(keyed by `market_slug`/`outcome`, which this module has no way to populate without that parsing
step). Both are real, distinct next steps — most naturally `checkMarkets.ts`'s job — not silently
done here.

**Why it exists:** direct instruction (Joey, 2026-07-19) — a single deterministic model gives a
false sense of precision on exactly the kind of question (will tomorrow's high cross this precise
threshold) where the honest answer is a probability distribution, not a point guess. This is the
predictive-side complement to Rule 5's insistence on reading the real settlement oracle rather
than approximating it — don't approximate the forecast side either, once real modeling work
begins.

---

## Roadmap / explicitly not yet built

- `detectAnomaly.ts`'s general bounds-check (physically-impossible-jump detection on raw
  readings) — Rule 4's slippage ceiling and Rule 6's dual-oracle cross-check are both built and
  tested; the general anomaly detector they plug into is not.
- `verifySettlement.ts` — the actual Playwright fetch against `wunderground.com`. Deliberately
  deferred as its own dedicated step (2026-07-19) rather than built alongside the pure
  comparison logic, given how much care the earlier evasion-tooling discussion (Rule 5) required.
- Entry-rule / position-sizing logic for `weather_position` (Rule 7 depends on this existing;
  Rules 11 and 12 must both be enforced there once it's built).
- `computeProbability.ts`'s actual climatology/forecast blend math (Rule 13's ensemble
  requirement applies here once built).
- Wiring `calculateProbability.ts` to real market thresholds — needs `weather_market_mapping`
  extended with stored min/max bucket columns (or a slug-parsing step), plus writing the result
  into `weather_probability_estimate` (keyed by `market_slug`/`outcome`). Most naturally
  `checkMarkets.ts`'s job once it exists — the probability math itself is done (Rule 13).
- A defined capital-base constant for the Weather Bot (Rule 11 is blocked on this — no
  `PAPER_BANKROLL_USD`-equivalent exists yet for this system).
- The `weather_rule_set` table, if/when entry-threshold logic needs its own versioned audit trail.
- `launchd` plist files (mechanism decided in `docs/weather/WEATHER_ARCHITECTURE.md`; concrete job
  definitions come with the scripts they invoke).
- Calibrated anomaly-detection thresholds for `detectAnomaly.ts` (needs real station data first —
  now available: `backfillHistorical.ts` has run for all 43 stations, Rule 3).
- Auto-triggering `backfillHistorical.ts` when `discoverMarkets.ts` auto-onboards a brand-new
  station (Rule 8) — today the two are still separate, manually-triggered steps; a newly
  auto-onboarded station has real coordinates/timezone immediately but no historical rows until
  `backfill:historical` is run again by hand.
