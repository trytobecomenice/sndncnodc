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

**Rules 1-15 are implemented and live-verified except where noted (the anomaly-detector's general
bounds-check and `verifySettlement.ts` still planned, see Roadmap). Rule 14 (the EV Bridge) and
Rule 15 (the Order Builder — Rules 7/11/12 now actually enforced, not just documented) are both
implemented and live-verified end-to-end as of 2026-07-20.**

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

**How it works mechanically: implemented and live-verified (2026-07-20), `orderBuilder.ts` /
`orderSizing.ts`.** `checkEdgeFloor(edge, floorPp)` requires `|edge| >= MIN_EDGE_FLOOR` (0.05, i.e.
5 percentage points) before a trade is even considered — evaluated before Rule 12's temp-buffer
check and before any Kelly sizing math runs. **A real interpretation call, disclosed rather than
hidden**: this rule's original text described the floor as "derived from the ~1-2°F gap the
reconciliation PoC measured," but that PoC measured a *temperature* gap, not a *probability-edge*
gap, and no formula in this codebase converts one into the other. Rather than invent an unjustified
conversion, `MIN_EDGE_FLOOR` was set to 5 percentage points — the same order of magnitude already
approved for `checkMarkets.ts`'s notable-edge threshold — as an explicit, named starting value
pending real calibration, not a rigorous derivation from the °F figure. Added to the Order Builder
at Joey's direction even though it wasn't in her original 3-item build list for this phase, since
it was already fully specified here as a required gate for exactly this component.

**System costs & trade-offs:** this margin requirement will mean passing on some marginal-edge
trades that a naive model (treating the settlement read as ground truth with zero uncertainty)
would take — fewer trades, a real opportunity cost, in exchange for not repeating the specific
overconfidence the PoC caught. The 5pp floor itself is unvalidated against real outcomes (no
settlement history exists yet) — a real gap, tracked in the Roadmap.

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

**How it works mechanically: implemented and live-verified (2026-07-20), `orderBuilder.ts` /
`orderSizing.ts` (see Rule 15 for the full orchestrator).** `computePositionSize()` computes the
standard binary-market Kelly fraction (`computeKellyFraction`: for buying "Yes" at price `P` when
our estimate `pHat` is higher, `f* = (pHat - P) / (1 - P)`; buying "No" is the mirror image), scales
it by `KELLY_MULTIPLIER = 0.25` (**Quarter-Kelly, Joey's explicit call, 2026-07-20** — full Kelly
was rejected specifically because it over-bets on a probability estimate this codebase's own docs
already flag as uncalibrated; see Rule 15's design-review note for the fuller reasoning), then hard
-caps at `MAX_CAPITAL_PER_TRADE = 0.05` against `WEATHER_PAPER_BANKROLL_USD = 10000` — **the first
concrete value for the capital-base constant this rule was blocked on**, a stated mock default
(Joey's own suggestion), not yet a real bankroll figure. **Live-verified**: a real -20.4pp edge on
`highest-temperature-in-seoul-on-july-21-2026-28c` computed a full-Kelly fraction of 67.0% — the 5%
cap was the binding constraint, not the quarter-Kelly scaling, exactly the scenario this rule
exists to prevent (an extreme, largely-unvalidated edge estimate translating directly into a
max-size bet).

**System costs & trade-offs:** 5% is a hard ceiling per market, and in practice — per the live
result above — it's usually the binding constraint over quarter-Kelly for anything but a small
edge, meaning most trades size to exactly 5% rather than a graduated Kelly-derived amount. This is
accepted, not a bug: given the blend model and edge threshold are both still uncalibrated, having
the ceiling bind hard is safer than trusting a large Kelly-implied fraction on an unproven
estimate. Revisit once real paper-trade outcomes exist to validate the probability model against.

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

**How it works mechanically: implemented and live-verified (2026-07-20), `orderBuilder.ts` /
`orderSizing.ts`.** `checkTempBuffer(meanForecastF, range, bufferF)` compares the ensemble's own
mean point-estimate forecast (a new field added to `calculateProbability.ts`'s `StationProbability`
— the mean of every combined member's forecast, both models pooled) against the strike range, and
requires clearance from EVERY finite boundary, not just one. **A deliberate generalization,
disclosed rather than silent**: this rule's original text used a one-sided example ("80°F or
above" — a single boundary), but most real markets found live are two-sided exact-degree buckets
(e.g. a "27C" market, already widened by `parseMarketThreshold.ts`'s rounding-boundary math to
`[26.5, 27.5]`°C). For a two-sided bucket, `checkTempBuffer` takes the MINIMUM distance to either
edge and requires that to clear the buffer — this reduces to exactly the original single-boundary
rule when only one bound is set, and is the more conservative choice for two-sided buckets (a
forecast centered inside a narrow bucket can still be close to BOTH edges at once, which is exactly
the jump-risk scenario this rule exists to catch). **Live-verified doing real work**: of 7 active
Seoul markets tested end-to-end, 4 were correctly rejected for sitting within 0.1-0.6°F of a strike
boundary (the ensemble's 79.1°F point estimate landed almost exactly between two adjacent 1.8°F-
wide buckets), while 3 with real 1.9-4.2°F clearance passed.

**System costs & trade-offs:** this is a DIFFERENT margin from Rule 7's residual basis-risk
margin — Rule 7 protects against the settlement READ being imprecise (scraper/oracle
disagreement); this rule protects against the FORECAST itself being imprecise (a point forecast is
never exactly right, and a strike sitting inside the forecast's own normal error band is a coin
flip dressed up as an edge). Both margins are enforced simultaneously in `orderBuilder.ts` before
any Kelly sizing runs. A tight buffer band around any given strike means some real edge gets left
on the table — confirmed live: half of the tested Seoul candidates were rejected on this gate alone
despite having already cleared Rule 7's edge floor, which is exactly the intended trade-off, not an
overly-blunt filter (each rejected market's forecast genuinely sat within the buffer zone).

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
ingestion run. `metric` (`"max"` or `"min"`) is a required parameter, no default — added
2026-07-20 after `parseMarketThreshold.ts` (Rule 14) confirmed live that Polymarket's Weather
category has real `lowest-temperature-in-X` events alongside the `highest-temperature-in-X` ones
already seen, and querying `tMaxF` for a low-temperature market would silently produce a
confidently-wrong probability rather than an error. **Wired to real market thresholds as of Rule
14 (2026-07-20)** — see below for `parseMarketThreshold.ts` and `checkMarkets.ts`, which close the
two gaps this scope note originally flagged (bucket parsing and the `weather_probability_estimate`
write path).

**Why it exists:** direct instruction (Joey, 2026-07-19) — a single deterministic model gives a
false sense of precision on exactly the kind of question (will tomorrow's high cross this precise
threshold) where the honest answer is a probability distribution, not a point guess. This is the
predictive-side complement to Rule 5's insistence on reading the real settlement oracle rather
than approximating it — don't approximate the forecast side either, once real modeling work
begins.

---

## 14. The EV Bridge — market-threshold parsing, odds-history tracking, `checkMarkets.ts`

**What it does:** connects the probability engine (Rule 13) to Polymarket's live prices. For every
market a human has approved (`is_active = true`, Rule 8), the bot parses the market's own strike
threshold, logs a time-series snapshot of Polymarket's current implied probability, computes a
blended probability estimate (climatology + ensemble forecast), and logs the resulting edge
(`blendedProb - marketImpliedProb`). This is a **read-and-log-only** step — it computes and
records Expected Value, it does not decide or place a trade. Introduced 2026-07-20 at Joey's
direction, alongside an explicit strategic framing: the bot is not limited to holding every
position to settlement — a future "Buy Low, Sell High" early-exit strategy (trading the spread
dynamically as odds shift) is the reason the odds-history table below exists, even though the
early-exit logic itself is not built yet.

**How it works mechanically: implemented and live-verified end-to-end against real Polymarket/
ensemble data (2026-07-20).**

- **Schema (migration `0005_magical_captain_britain.sql`, applied to the live DB following the
  established stop-migrate-restart runbook — `bot.py`/`dashboard.py`/the Next.js dev server were
  stopped first, migrated, then restarted).** `weather_market_mapping` gained four nullable
  columns: `metric` (`"max"`/`"min"`), `forecast_for` (`YYYY-MM-DD`), `target_temp_min_f`,
  `target_temp_max_f` (both real °F, already rounding-boundary-adjusted — see below). A new table,
  `weather_market_odds_snapshot` (`market_slug`, `recorded_at`, `implied_probability`, indexed on
  `(market_slug, recorded_at)`), is the odds-history time series — append-only, one row per
  `checkMarkets.ts` observation, never upserted, specifically so a probability *shift* over time
  can be detected later (the whole point of the early-exit strategy Joey described).
- **`parseMarketThreshold.ts`** — a regex-based title/slug parser, `parseMarketThreshold(slug):
  ParsedThreshold | null`. Handles every real slug shape found live across the 100-event/48-city
  scan: exact-degree buckets (`...-27c`, `...-80-81f`), open-ended buckets (`...-32corhigher`,
  `...-below100f`), and both Celsius and Fahrenheit markets. **Rounding-boundary math, confirmed
  live against real market description text for both unit systems**: Polymarket weather markets
  settle on a whole-degree-*rounded* reading, so a bucket stated as exactly `27C` actually covers
  the true range `[26.5, 27.5]` in that market's native unit — every parsed threshold is widened by
  ±0.5° in its native unit BEFORE Fahrenheit conversion, not after (converting the ±0.5°C margin
  itself to °F would silently produce a different, wrong margin). Validated against all 100 live
  events, not just hand-picked cases: 1,024 of 1,052 real strike markets parsed successfully; all
  28 non-matches independently confirmed to be genuinely non-temperature markets (air quality,
  earthquake counts, a hantavirus-pandemic market, "hottest year on record" — the same 6 markets
  Rule 8 already found fall through the non-Wunderground skip path). 9 unit tests cover
  exact-Celsius, Celsius-below/above, Fahrenheit-range, Fahrenheit-below/above, the
  `min`-vs-`max` metric split, and confirm real non-temperature slugs correctly return `null`
  rather than a wrong guess.
- **`discoverMarkets.ts` extended** to call `parseMarketThreshold()` on every market slug and pass
  the result into `upsertMarketMapping` alongside the existing settlement fields — idempotent, so
  re-running it backfilled the parsed fields onto every mapping still inside the current odds-filter
  band without any separate migration script. Live re-run (2026-07-20): 207 of 260 total mapping
  rows now carry `metric`/`forecast_for`/threshold values (the remaining rows are mappings from
  markets that have since moved outside Rule 10's 10-90% band and were correctly not re-touched —
  not a bug, matches the rule's own documented at-scan-time-only scope).
- **`calculateClimatology.ts`** — the second, independent probability input the blend needs. For a
  target date, builds a ±7-day day-of-year window (real `Date` arithmetic, not string matching, so
  month/year boundaries and leap years are handled correctly — 5 unit tests cover exactly these
  cases) across every year actually present in `weather_historical_observation` (discovered from
  the data itself, not hardcoded, so it stays correct as the Rule 3 backfill window rolls forward),
  and computes the historical hit-rate for the target range using the same `computeHitRate`
  function Rule 13's ensemble math already uses. **Genuinely small sample, disclosed not hidden**:
  with the current 2-year backfill this is roughly 30 data points per station/date — a starting
  climatology baseline, explicitly not a mature one.
- **`checkMarkets.ts`** — the orchestrator. For every `is_active = true` mapping: (1) fetches
  current odds via one `bullpen polymarket discover --category weather` call (same data shape
  `discoverMarkets.ts` already uses, not a new API pattern) and logs a
  `weather_market_odds_snapshot` row; (2) computes `climatologyProb` and `forecastProb` in
  parallel; (3) blends them via `blendedProb = 0.35 * climatologyProb + 0.65 * forecastProb` — a
  **deliberately simple v1 heuristic**, weighted toward the specific multi-model forecast over the
  thinner historical base rate, not a rigorously-derived optimal weighting (flagged in-code as a
  first-pass to revisit once real outcome data exists to validate against); (4) computes
  `edge = blendedProb - marketImpliedProb` and writes a full `weather_probability_estimate` row
  (both individual probabilities, the blend, the market price, the edge, `inputsJson` carrying the
  full climatology/forecast breakdown including per-model detail, for later auditability); (5)
  flags (log-only, does not gate or filter anything written) any market whose `|edge|` crosses
  `NOTABLE_EDGE_THRESHOLD = 0.05` (5%) as worth a human look — chosen to match the order of
  magnitude of Rule 11's 5% position-sizing cap, a starting point pending real calibration, not an
  empirically-derived optimal value.

**Live end-to-end proof (2026-07-20)**: one real mapping (`highest-temperature-in-nyc-on-
july-19-2026-80-81f`, station KLGA) was temporarily activated to smoke-test the full pipeline
against live data, then deactivated again immediately — genuine activation for trading remains a
human decision (Rule 8), this was a code-verification step only, and the test odds-snapshot/
probability-estimate rows were deleted afterward so no synthetic data pollutes the real EV history.
The run correctly fetched live odds (71.0%), computed climatology (9.7%) and forecast (6.1%)
probabilities, blended them (7.4%), and wrote both DB rows — proving every piece of the pipeline
works against real data, not just synthetic test fixtures.

**A real gap surfaced by that same smoke test, closed the same day (2026-07-20) — the Staleness
Guard, `staleness.ts`.** The edge computed in that smoke test was a huge -63.6 percentage points —
not because of a genuine mispricing, but because the market's `forecast_for` date (July 19) had
already fully elapsed by the time the test ran (July 20), while the ensemble forecast feeding
`forecastProb` was issued mid-day on the 19th itself. `checkStaleness(forecastFor, timezone, now)`
now gates every mapping in `checkMarkets.ts` BEFORE any odds fetch or probability math runs: a
market is stale (skipped, no edge calculated at all) if its `forecast_for` day is strictly before
the STATION's own current local calendar day, or if it's the current local day but the station-
local clock has passed `STALE_CUTOFF_HOUR = 18` (6pm) — same-day trading before that cutoff is
explicitly allowed (Joey's call, 2026-07-20). Station-local time, not server/UTC time, is used
throughout — computed via `Intl.DateTimeFormat` against `weather_station.timezone` (already
resolved live via `geo-tz`, Rule 8), so DST transitions are handled correctly by the runtime, not
approximated. 8 unit tests cover positive- and negative-offset zones, UTC-midnight day-rollover in
both directions, and the exact cutoff-hour boundary. `checkMarkets.ts` also now stores
`isSameDay`/`stationLocalTime` in each estimate's `inputsJson.staleness` for later reuse, and Rule
15's Order Builder independently re-checks staleness fresh at its own run time rather than trusting
a possibly-stale flag from an earlier `checkMarkets.ts` pass.

**System costs & trade-offs:** the 0.35/0.65 blend weight and the 5% notable-edge threshold are
both stated defaults, not calibrated values — real outcome data (positions taken, actual
settlement vs. predicted probability) is needed before either can be tuned with confidence, and
neither should be treated as more rigorous than it is. The odds-history table is pure overhead
today (nothing reads it yet) in exchange for making the future early-exit strategy possible at all
— logging it from day one avoids a gap in the time series that would otherwise need to be
backfilled (impossible, since past odds can't be reconstructed) once that strategy is actually
built. The forecast-staleness gap above is a real, currently-unmitigated risk for any market whose
`forecast_for` is at or near the present day — until the freshness guard is built, a human
reviewing candidates for `is_active` should independently check that the forecast date is
meaningfully in the future, not rely on `checkMarkets.ts`'s edge number alone for same-day or
past-date markets.

**Why it exists:** direct instruction (Joey, 2026-07-20) — "wire the brain and the eyes together."
The probability engine (Rule 13) and the settlement/mapping infrastructure (Rules 5, 8) were both
real and tested in isolation but had no path connecting them to Polymarket's actual live price;
this rule is that connection. The odds-history table specifically exists because the stated future
strategy is "not just holding to settlement" — an early-exit decision requires knowing how the
market's price has moved, which is unrecoverable after the fact if not logged as it happens.

---

## 15. The Order Builder — translating EV into a sized paper trade

**What it does:** `orderBuilder.ts` is the "hands" of the bot — it reads `checkMarkets.ts`'s
logged edge for every active market, enforces Rules 7, 11, and 12 as hard gates, and if (and only
if) a market clears all three, writes a sized, paper-only trade to `weather_position`. This is the
first component in the pipeline that actually creates a position, not just logs a signal — every
prior EV-related component (Rule 13's probability engine, Rule 14's bridge) only computes and
records.

**How it works mechanically: implemented and live-verified end-to-end (2026-07-20).**

- **`orderSizing.ts`** — pure, unit-tested functions with zero DB/network I/O (same split this
  codebase already uses elsewhere: `computeHitRate` vs. `calculateProbability.ts`, `checkOddsFilter`
  vs. `discoverMarkets.ts`). `computeKellyFraction(pHat, marketProb)` implements the standard
  binary-market Kelly formula; `checkEdgeFloor` (Rule 7), `checkTempBuffer` (Rule 12), and
  `computePositionSize` (Rule 11) are each described in their own rule sections above. 14 unit
  tests, including a direct reproduction of the real Seoul smoke-test numbers (a -26.5pp edge
  correctly produces a 65.4% full-Kelly fraction on the "No" side).
- **Gate order, deliberate**: no-usable-estimate check → staleness re-check → duplicate-position
  guard → Rule 7 (edge floor) → Rule 12 (temp buffer) → Rule 11 (Kelly sizing) → zero-size check →
  write. Cheaper/structural checks run before the rule gates so a market that can never trade
  (e.g. no estimate yet) doesn't pay for temp-buffer computation it doesn't need.
- **Duplicate-exposure guard, added proactively (not explicitly requested, but a clear gap left
  open otherwise)**: `hasOpenPosition(marketSlug)` skips any market that already has a `status =
  'open'` `weather_position` row, so re-running `orderBuilder.ts` — which will happen routinely,
  since `checkMarkets.ts` re-logs new estimates on every pass — never stacks a second position on
  a market it's already holding. Mirrors the Copy Bot's own equivalent guard (`bot.py`). **Live-
  verified idempotent**: re-running immediately after 3 real orders were placed correctly skipped
  all 3 as duplicates and re-evaluated (and correctly re-rejected, same reasons) the other 4.
- **Telemetry, per Joey's explicit spec (2026-07-20)**: `weather_position` (the pre-existing,
  previously-unused 7th table — reused rather than creating a redundant new table, since it was
  already shaped exactly for this: `marketSlug`/`outcome`/`entryPrice`/`ourSizeUsd`/`status`) gained
  8 new columns via migration `0006_goofy_grey_gargoyle.sql`: `ensemble_prob`, `polymarket_prob`,
  `probability_difference`, `is_same_day`, `station_local_time`, `temp_buffer_f`,
  `full_kelly_fraction`, `applied_fraction` — the exact execution metrics that justified each
  trade, recorded at build time for later audit rather than needing to be re-derived.
- **Capital base, resolved**: `WEATHER_PAPER_BANKROLL_USD = 10000` — the first concrete value for
  the constant Rule 11 was blocked on, a stated mock default (Joey's own suggestion, 2026-07-20),
  not a real bankroll figure.

**Live end-to-end proof (2026-07-20), against the 7 real active Seoul markets** (the same ones
Rule 14's smoke test used, this time genuinely activated by Joey's own review, forecasting a
genuinely future day): 4 of 7 correctly rejected on Rule 12's temp buffer (forecasts sitting
0.1-0.6°F from a bucket edge), 0 rejected on Rule 7 (all 7 already had real edges well above the 5pp
floor), 3 orders placed — `BUY YES $154.21` on the 25°C low-temperature bucket (a genuine
quarter-Kelly-bound size, 1.54% of capital, edge 5.5pp), and `BUY NO $500.00` on both the 28°C and
29°C high-temperature buckets (both hit the 5% hard cap — full-Kelly fractions of 67.0% and 82.1%
respectively, on -20.4pp and -13.9pp edges, exactly the scenario Rule 11 exists to bound). Re-run
immediately after: idempotent, 0 new orders, all 3 correctly skipped as duplicates. **Kept as the
first real paper-trading positions**, per Joey's explicit choice (2026-07-20) — not cleared as test
artifacts, since all three genuinely cleared every gate on real data, not a smoke-test bypass.

**A design review happened before this was built, worth recording.** Joey asked explicitly whether
this was a good next step and invited pushback. Three concerns were raised and resolved before any
code was written: (1) full Kelly sizing on an edge estimate this codebase's own docs already flag
as uncalibrated was judged too aggressive — resolved via quarter-Kelly (`KELLY_MULTIPLIER = 0.25`);
(2) Rule 7's edge floor, though not in Joey's original 3-item scope, was judged a natural and
already-specified companion gate to Rule 12 — resolved by building it now; (3) the Staleness Guard's
"too close to end of day" needed a concrete, non-vague definition — resolved as the 18:00
station-local cutoff described above. None of these were reasons to NOT build — paper trading is
exactly the environment where this component should be built and exercised, since it's the only way
to accumulate the real outcome data needed to eventually validate/calibrate the blend weights (Rule
14) and the edge floor (Rule 7) this component depends on.

**System costs & trade-offs:** every numeric default here — `MIN_EDGE_FLOOR`, `KELLY_MULTIPLIER`,
`TEMP_BUFFER_F`, `MAX_CAPITAL_PER_TRADE`, `WEATHER_PAPER_BANKROLL_USD` — is a stated starting value,
not empirically calibrated, and the live result already shows the 5% cap binding harder than
quarter-Kelly scaling for anything but a small edge (see Rule 11). This means early paper-trading
volume will skew toward capped-size trades on the largest apparent edges, which is a real behavior
to watch for: if the blend model is systematically miscalibrated in one direction, this sizing
approach would concentrate risk exactly where the model is most confident and potentially most
wrong. No calibration correction exists yet — this is the reason paper trading, not real capital,
runs first.

**Why it exists:** direct instruction (Joey, 2026-07-20) — "build the 'Hands' of our bot." Every
risk rule enforced here (7, 11, 12) was already fully documented before this component existed;
this rule is where they actually bind for the first time, closing the gap between "a rule is
written down" and "a rule is enforced by running code."

---

## Roadmap / explicitly not yet built

- `detectAnomaly.ts`'s general bounds-check (physically-impossible-jump detection on raw
  readings) — Rule 4's slippage ceiling and Rule 6's dual-oracle cross-check are both built and
  tested; the general anomaly detector they plug into is not.
- `verifySettlement.ts` — the actual Playwright fetch against `wunderground.com`. Deliberately
  deferred as its own dedicated step (2026-07-19) rather than built alongside the pure
  comparison logic, given how much care the earlier evasion-tooling discussion (Rule 5) required.
- **Calibration of every stated-default numeric constant** — `MIN_EDGE_FLOOR` (5pp), the
  `checkMarkets.ts` blend weights (0.35/0.65), `NOTABLE_EDGE_THRESHOLD` (5%), `KELLY_MULTIPLIER`
  (0.25), `TEMP_BUFFER_F` (1.5), `STALE_CUTOFF_HOUR` (18:00) — all are explicitly-flagged starting
  values, not empirically derived. Needs real paper-trade settlement outcomes to validate against;
  the 3 real positions opened 2026-07-20 are the first data toward that.
- **The "Buy Low, Sell High" early-exit strategy** — trading the spread dynamically as odds shift,
  rather than only holding every position to settlement (Joey, 2026-07-20). The odds-history table
  (`weather_market_odds_snapshot`, Rule 14) exists specifically to make this possible later; the
  actual shift-detection/exit-decision logic is not built yet.
- **Position closeout / settlement-driven PnL** — `orderBuilder.ts` only opens positions; nothing
  yet closes a `weather_position` row, computes `realizedPnlUsd`, or rolls up `weather_pnl_snapshot`.
  This is the natural next gap once real positions exist to close.
- `WEATHER_PAPER_BANKROLL_USD` is currently a static mock constant (10,000), not a real,
  accounted-for capital base — Rule 11's sizing math is correct, but nothing yet tracks capital
  consumed by open positions against it (a second open position sized off the same static 10,000
  rather than remaining capital), which the 3-open-position portfolio right now doesn't yet expose
  but a larger one would.
- The `weather_rule_set` table, if/when entry-threshold logic needs its own versioned audit trail.
- `launchd` plist files (mechanism decided in `docs/weather/WEATHER_ARCHITECTURE.md`; concrete job
  definitions come with the scripts they invoke).
- Calibrated anomaly-detection thresholds for `detectAnomaly.ts` (needs real station data first —
  now available: `backfillHistorical.ts` has run for all 43 stations, Rule 3).
- Auto-triggering `backfillHistorical.ts` when `discoverMarkets.ts` auto-onboards a brand-new
  station (Rule 8) — today the two are still separate, manually-triggered steps; a newly
  auto-onboarded station has real coordinates/timezone immediately but no historical rows until
  `backfill:historical` is run again by hand.
