# Weather Bot — Risk Management & Rules

**Audience note:** zero prior context assumed, same standard as `docs/copy-trading/RISK_MANAGEMENT.md`. Every
rule below follows What it does / How it works mechanically / System costs & trade-offs / Why it
exists — precise enough to extend, tune, or challenge without re-deriving the reasoning.

**Status: early implementation.** This rules ledger was written *before* any code, per an explicit
documentation-first requirement, and is kept in sync as pieces get built — see each rule's "How it
works mechanically" for what's implemented and unit-tested today (`ingestMetar.ts`,
`pruneHistorical.ts`, `emergencyCloseoutGuard.ts`, `checkSettlementAgainstMetar.ts`) versus what's
still planned (`detectAnomaly.ts`'s general bounds-check, `verifySettlement.ts`'s live
Wunderground fetch, and all entry-rule/position-sizing logic). **This is the single source of
truth for what rules will apply to the Weather Bot** — check it
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
execution-cycle table) — run manually until that's built. Historical backfill on onboarding a new
station is capped at the same 2-year window from day one, rather than backfilling everything
available and pruning down afterward.

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

**How it works mechanically:** `discoverMarkets.ts` runs `bullpen polymarket discover --category
weather` periodically and surfaces one draft candidate per **city/event** (not per individual
degree-bucket market — Polymarket groups many binary strike markets under one event, e.g. Seoul's
"highest temperature" event contains a separate market for every whole-degree outcome). A human
approves the settlement-station pairing once per event; every strike market under that event
inherits the approved mapping automatically. `is_active` on `weather_market_mapping` defaults to
requiring this approval — no market trades off an unreviewed mapping.

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

**How it works mechanically:** a single validation helper, `assertStationExists(stationId)` in
`packages/weather/src/db/writers.ts`, is called at the top of every writer function that
references a `station_id` or `market_slug` — the same single-writer-funnel pattern the Copy Bot's
`upsertWalletProfile` already uses (one function per table is the only place that writes it,
making the safety boundary easy to verify by inspection).

**System costs & trade-offs:** SQLite only enforces FK constraints with `PRAGMA foreign_keys = ON`
per-connection — turning that on would make any out-of-order write (e.g. a backfill script racing
ahead of its station row landing) a hard failure instead of an inert loose reference, real
rigidity for a system with several independent ingestion scripts that can plausibly run out of
strict order. The `assertStationExists` approach catches the same class of bug (a typo'd or
deleted-in-error station ID) without that rigidity cost.

**Why it exists:** consistency with the rest of this codebase (zero `.references()` calls exist
anywhere in `packages/db/src/schema.ts` today, Copy Bot tables included) — introducing FKs only
for weather tables would make this domain structurally inconsistent with everything else in the
same schema file for no proportionate benefit, since nothing in this system hard-deletes rows.

---

## Roadmap / explicitly not yet built

- `detectAnomaly.ts`'s general bounds-check (physically-impossible-jump detection on raw
  readings) — Rule 4's slippage ceiling and Rule 6's dual-oracle cross-check are both built and
  tested; the general anomaly detector they plug into is not.
- `verifySettlement.ts` — the actual Playwright fetch against `wunderground.com`. Deliberately
  deferred as its own dedicated step (2026-07-19) rather than built alongside the pure
  comparison logic, given how much care the earlier evasion-tooling discussion (Rule 5) required.
- Entry-rule / position-sizing logic for `weather_position` (Rule 7 depends on this existing).
- `computeProbability.ts`'s actual climatology/forecast blend math.
- The `weather_rule_set` table, if/when entry-threshold logic needs its own versioned audit trail.
- `launchd` plist files (mechanism decided in `docs/weather/WEATHER_ARCHITECTURE.md`; concrete job
  definitions come with the scripts they invoke).
- Calibrated anomaly-detection thresholds for `detectAnomaly.ts` (needs real station data first).
