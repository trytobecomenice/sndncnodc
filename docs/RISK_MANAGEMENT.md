# Risk Management & Tracking Rules

**Audience note:** this document assumes zero prior context on this project. It's written for
a quant developer who needs to understand every automated risk control this system enforces —
what it does, exactly how it's implemented, what it costs, and why it was built — well enough
to extend, tune, or challenge any of it without archaeology through the codebase first.

**This is the single source of truth for "what rules currently apply."** Reference it before
deciding on a new restriction, so an old decision doesn't get silently re-thought or
contradicted. `docs/SAFETY.md` is the engineering companion: shared-database ownership rules,
migration/cutover runbooks, and deeper implementation notes that don't fit the rule-by-rule
format below. Keep both current in the same commit when a rule changes — don't let them drift.

## System context, in one paragraph

This is a Polymarket copy-trading bot. `bot.py` polls a live trade feed (via the external
`bullpen` CLI, which owns all Polygon/Polymarket auth and signing — this codebase never touches
a private key) for trades made by a set of "tracked" wallets, and mirrors qualifying trades as
its own paper (simulated) or live (real-money) orders, sized independently of the source
trader's own size (`config.FIXED_TRADE_USD`, currently $5/trade). State — open positions, trade
history, per-trader mute status — lives in a shared SQLite database (`data/app.db`) that Python
(`bot.py`/`db.py`) and a TypeScript research layer (`packages/copy-trading`) both touch, under a
strict ownership split (Rule 8). Every rule below is a gate that can reject or abort a trade
before it happens; **none of them retroactively unwind a trade that already executed.**

---

## 1. Paper trading only

**What it does:** `config.LIVE_MODE = False` forces every order `bot.py` places to be simulated.
No real funds move, no private key is ever requested, stored, or signed by this codebase.

**How it works mechanically:** `LIVE_MODE` is a single boolean read at multiple call sites in
`bot.py` (buy, sell, and TTP-close paths) that branches between a paper fill (recorded directly
into the `paper_trade` table via `db.py`, priced at the source trade's own reported price) and a
real order (`bullpen polymarket buy/sell --yes`, which requires `LIVE_MODE = True` AND passes
through every other gate in this document first — the spread guard and Disciplined Taker
slippage ceiling, Rules 4 and 11, only run at all when `LIVE_MODE` is true, since a paper fill
has no real execution to protect). All Polygon/Polymarket key custody is delegated to the
external `bullpen` CLI, which runs as a subprocess (`run_bullpen_json` / `run_bullpen_command` in
`bot.py`) — this app has no code path that could accept, log, or transmit a key even by mistake.

**System costs & trade-offs:** paper mode's biggest cost is realism drift — a paper fill always
executes at the source trader's own reported price, which real execution would never achieve
(you're always seconds behind the trade you're copying). Rule 10 (execution shortfall tracking)
exists specifically to quantify this gap without touching paper P&L, so the optimism is
*measured*, not eliminated.

**Why it exists:** per this project's build spec, v1 must not place real trades, must not ask
for or store private keys, and must not sign transactions — this is a hard product requirement,
not a default that happened to stick. Flipping `LIVE_MODE` is treated as a separate, deliberately
reviewed change, never a silent side effect of another commit.

---

## 2. Wallet tracking source control

**What it does:** the bot only copies trades from a bounded, explicitly-approved set of
wallets — never "anyone on the leaderboard." Which set is active is controlled by
`config.TRACKED_TRADERS_SOURCE`, currently `"static"`.

**How it works mechanically:** `"static"` reads a hardcoded wallet list from `config.py`.
`"db"` (built and tested, not yet activated) instead reads every `wallet_profile` row with
`status = 'track'` — the output of the TypeScript wallet-scoring pipeline
(`packages/copy-trading/src/scoreWallets.ts`, see Rule 12). The switch is read once at startup
(`get_tracked_traders()` in `bot.py`); when `"db"` is active, `db.py` additionally enforces
`MIN_TRACKED_TRADERS = 3` — if the scored pool ever drops below that floor, the bot refuses to
start rather than silently run with too few (or zero) traders. This is a fail-loudly-at-startup
design, not a runtime fallback.

**System costs & trade-offs:** in `"static"` mode, a wallet that goes cold or starts
underperforming stays tracked until a human edits `config.py` — no automatic pruning. `"db"`
mode fixes that (monthly re-scoring drops cold/toxic wallets automatically, Rule 12) but couples
the bot's tracked set to the scoring pipeline's correctness — a scoring bug would propagate
directly into what the bot copies.

**Why it exists:** the `"db"` mechanism is fully built and unit-tested but has been deliberately
left off — this is a scaling gate, not an oversight. It stays `"static"` until a human reviews a
real scored batch and is confident the scoring rules (Rule 12) are picking sensible wallets, per
the project's "no automated switch flips real risk without explicit review" principle.

---

## 3. Duplicate-exposure guard

**What it does:** prevents the bot from accidentally holding multiple, uncapped positions in
the exact same market outcome. Two sub-rules: (a) if a DIFFERENT tracked trader already holds an
active position in this outcome, the copy is blocked entirely; (b) the SAME trader may add to
their own position (average up), but only up to `config.MAX_BUYS_PER_TRADER_OUTCOME = 2` total
buys on that outcome.

**How it works mechanically:** positions are keyed `trader|market_slug|outcome`
(`position_key()` in `bot.py`), so without this guard, two different tracked traders both buying
`"will-x-happen" / "Yes"` would each open their own separate position — silently doubling (or
N-tupling) real exposure to one outcome. `find_cross_trader_position()` scans open positions for
any OTHER trader already holding this exact market+outcome before every BUY; a match blocks the
copy (`skip_duplicate_position`). Same-trader adds are tracked via a `buy_count` field on the
position record, checked against the cap before any additional buy is allowed
(`bot.py` process_trade, ~line 695).

**System costs & trade-offs:** the cross-trader block is coarse — it can't distinguish "trader B
is piling into a crowded trade" (bad) from "trader B independently, coincidentally, likes this
outcome too" (fine); both are blocked identically. `MAX_BUYS_PER_TRADER_OUTCOME = 2` also caps
legitimate high-conviction averaging-up beyond two buys, which could reject a good trade in a
scenario where the source trader keeps adding a third or fourth time.

**Why it exists:** added 2026-07-16 after recognizing that per-trader position keys have no
natural ceiling on total exposure to a single outcome across multiple tracked wallets — without
this, the portfolio-level exposure ceiling (Rule 6) would still eventually catch runaway
exposure, but only after it had already grown much larger than intended.

---

## 4. Spread & liquidity guard

**What it does:** before any LIVE order, rejects the copy if the current relative bid/ask spread
exceeds `config.SPREAD_TOLERANCE = 5%`, or if the market preview itself reports a liquidity
warning.

**How it works mechanically:** `check_spread_tolerance()` in `bot.py` calls
`bullpen polymarket preview` for a fresh read of the CURRENT book — deliberately independent of
the (possibly stale) price the trade feed reported for the source trade. Preview's `spread`
field is an ABSOLUTE price-tick value (e.g. `0.01`), not a fraction — the function divides by
price to get a comparable relative number across outcomes trading near $0.05 vs. near $0.95
(verified empirically: a thin long-shot market and a liquid ~50/50 market can report the
identical absolute spread while differing 10x+ in relative terms). This call is reused, not
repeated, by the Disciplined Taker check (Rule 11) — see that rule for why that matters.

**System costs & trade-offs:** one extra `bullpen` subprocess call per live BUY/SELL
(retried up to `FEED_FETCH_RETRIES = 3` times, 0.5s apart, on transient failure). Fails
**closed**: if preview itself errors (network/timeout/parse failure), the function returns
not-ok rather than skipping the check — the deliberate trade-off is missing a real copy over
firing blind into an unknown book. Only runs in `LIVE_MODE`; paper fills never see this check, so
paper P&L can look better than a live run would have, on exactly the trades this would have
blocked.

**Why it exists:** added 2026-07-15 as the first defense against the core risk of copy trading —
a copy can fill at a much worse price than the source trader's own fill if the book has moved or
is simply too thin to absorb a same-sized trade cleanly.

---

## 5. Per-trader circuit breaker

**What it does:** automatically mutes (stops copying) an individual tracked trader whose own
recent copy-trade performance turns bad — either 3 consecutive losing closes, or a win rate
under 30% once at least 10 closed trades exist.

**How it works mechanically:** `check_circuit_breaker()` in `bot.py` runs immediately after
every realized P&L is logged on one of the bot's own closes (`paper_sell`/`live_sell`), keyed by
`trader.lower()`. It tracks a rolling list of the trader's last `MIN_TRADES_FOR_WIN_RATE_MUTE =
10` win/loss outcomes and a consecutive-loss counter. `MUTE_CONSECUTIVE_LOSS_STREAK = 3` trips
immediately; `MUTE_WIN_RATE_THRESHOLD = 0.30` only evaluates once 10 closes exist (a smaller
sample is too noisy to act on). A mute is written to `wallet_profile` (via `db.py`) and persists
across restarts; **existing open positions from a muted trader can still be sold** — a mute only
blocks new BUY signals from that trader, never an exit.

**System costs & trade-offs:** this is per-trader only — it has no view of total portfolio
exposure or drawdown across all tracked traders combined (that gap is closed by Rule 6, added
later). It also judges a trader purely on the bot's OWN copy performance, not the trader's raw
on-chain performance — a trader who's genuinely profitable but consistently beats the bot's
execution speed (Rule 10's shortfall problem) could get muted for a structural latency issue
that isn't really "their" fault.

**Why it exists:** added 2026-07-16 — before this, an underperforming trader could be copied
indefinitely with no automatic response, relying entirely on a human noticing and manually
removing them from the tracked list.

---

## 6. Portfolio-level risk manager

**What it does:** three independent controls, all in `risk_manager.py` (added 2026-07-18), that
cap risk across the WHOLE book rather than one trader at a time:
- **Total exposure ceiling** (`config.MAX_TOTAL_EXPOSURE_USD = $250`): sum of open cost basis +
  the new trade may not exceed this.
- **Per-event exposure cap** (`config.MAX_EVENT_EXPOSURE_USD = $30`): same check, scoped to one
  Polymarket *event* (which can span several correlated markets, e.g. multiple strike-price
  outcomes on the same underlying question).
- **Drawdown kill switch**: halts all new BUYs if portfolio equity drops below an absolute floor
  of `config.EQUITY_FLOOR_USD = $100`, or falls `config.MAX_DRAWDOWN_FROM_PEAK_USD = $50` below
  its own high-water mark — whichever trips first.

**How it works mechanically:** every function in `risk_manager.py` is pure (no I/O, no DB, no
subprocess) and unit-tested independently (`test_risk_manager.py`) — `bot.py` owns all the
wiring (state load/persist via `db.py`, market→event resolution via `bullpen`). `check_buy()`
evaluates checks cheapest-and-most-absolute-first: kill switch, then total ceiling, then
per-event cap; limits use strict "would exceed" semantics (landing exactly ON a limit is
allowed). Exposure uses **cost basis**, not live market value — it needs no price fetch and can't
swing between two checks a few seconds apart. Per-event exposure requires resolving
market→event via `bullpen polymarket market` (memoized in `bot_market_event`); **resolution
failure fails CLOSED** — the buy is skipped rather than letting unattributable exposure bypass
the cap. Equity = `PAPER_BANKROLL_USD` + realized P&L + unrealized P&L, where unrealized comes
from the Trailing Take-Profit sweep's price fetches (`compute_equity()`), so equity — and
therefore the kill switch — only refreshes every `TRAILING_TP_CHECK_INTERVAL_SECONDS = 300s`
(~5 min), not per-trade. Once tripped, the halt **latches** in `bot_risk_state`, persists across
restarts, and requires running `python3 reset_kill_switch.py` (a standalone script, deliberately
NOT a dashboard button — the dashboard never writes to `data/app.db`) followed by a bot restart.

**System costs & trade-offs:** the ~5-minute equity refresh lag means the kill switch can't react
to a drawdown that happens and reverses within a single sweep window — it's a portfolio-level
backstop, not a per-trade circuit breaker. Per-event exposure only catches concentration within
one Polymarket event; economically-correlated bets across DIFFERENT events (e.g. two related
elections) are invisible to this cap. All three controls gate new BUYs **only** — sells,
trailing-TP exits, and closeouts are never blocked, deliberately, because a risk layer that traps
you in a losing position adds risk instead of removing it (this principle repeats at Rule 11).

**Why it exists:** the per-trader circuit breaker (Rule 5) has no concept of total dollar
exposure or drawdown across the whole book — a portfolio could accumulate dangerous aggregate
risk one individually-fine trade at a time. Added 2026-07-18 specifically to close that gap, and
explicitly framed as the enabler for tracking MORE wallets safely (paired with Rule 11) instead
of relying on a short, hand-curated list to keep risk manageable by inspection.

---

## 7. Exit protections

**What it does:** open positions are watched for a trailing take-profit exit, and closed
automatically if the underlying market resolves — independent of any new signal from the source
trader.

**How it works mechanically:** `check_trailing_take_profit()` arms once a position's peak profit
has ever reached `config.TRAILING_TP_ACTIVATION_PCT = 50%`; once armed, a pullback of
`config.TRAILING_TP_DRAWDOWN_PCT = 10 percentage points` off that peak triggers a full-position
market sell (`close_position_trailing_tp()`) — a "suck back" exit design that locks in a large
run-up without exiting on every minor wiggle before that. This sweep runs at most once per
`TRAILING_TP_CHECK_INTERVAL_SECONDS = 300s`, not on every poll cycle (`POLL_INTERVAL_SECONDS =
30s`). Pricing uses `get_market_prices()`: `best_bid` (what a market sell would actually receive
right now) is the ONLY price allowed to trigger a sell; a fallback `indicative_price`
(midpoint/last-trade) only keeps the high-water mark fresh, since `last_trade` on a dead book can
be arbitrarily stale and must never fire an exit on its own. A separate closeout sweep
(`run_closeout_sweep()`) checks for markets that have resolved and closes those positions too.

**System costs & trade-offs:** the 5-minute TTP check interval means a fast reversal between
sweeps can erode gains below the intended drawdown trigger before the bot reacts — a deliberate
latency/API-load trade-off, not a bug. Exit logic depends entirely on `bullpen`'s live pricing
being available; if `bullpen polymarket price` fails, that position simply isn't re-evaluated
that cycle (fails soft, not closed) rather than forcing a stale-priced exit. The same fails-soft
principle applies to the closeout sweep: a market whose lookup fails is left alone and retried
next sweep, and since 2026-07-19 repeated identical lookup failures are throttled in the event
log (first failure logged, then one reminder per ~24 consecutive failures with a running count —
`_closeout_fetch_failures` in `bot.py`) so a bullpen backend outage can't flood `bot_event_log`
with hundreds of duplicate error rows the way one did before the throttle existed.

**Why it exists:** without an active exit mechanism, a profitable copy relies entirely on the
source trader eventually selling — which may happen too late, or not before a resolution.
Trailing TP locks in gains proactively rather than passively mirroring the source trader's exit
timing.

---

## 8. Database ownership boundaries

**What it does:** TypeScript/Drizzle owns all schema (DDL); Python only ever performs
row-level CRUD against the shared SQLite database.

**How it works mechanically:** `db.py` and `migrate_to_sqlite.py` issue only
`SELECT`/`INSERT`/`UPDATE`/`DELETE` — never `CREATE`/`ALTER TABLE`. Schema changes happen
exclusively via `pnpm db:migrate` on the TS side. Every writer sets
`PRAGMA busy_timeout=5000` on its own connection, and WAL mode is applied once at first
migration, so `bot.py`, the TS operator loop, and the dashboard's one mutation route can share
one SQLite file without lock contention under normal load.

**System costs & trade-offs:** SQLite's limited `ALTER TABLE` support means Drizzle Kit often
does a rename-and-copy-table dance under the hood for a schema change — this is UNSAFE to run
concurrently with another process's open transaction on that table. `bot.py` and any TS operator
loops must be stopped before every `pnpm db:migrate`, which means a schema change always requires
a brief, coordinated downtime window — there is no live-migration path today.

**Why it exists:** with three independent processes able to touch one file
(`bot.py`, the future `packages/copy-trading` operator loop, `apps/dashboard`), an unenforced
schema-ownership split would eventually let two processes fight over the same table's shape. This
boundary makes that structurally impossible rather than relying on convention.

---

## 9. Cross-layer write boundary (scoring vs. circuit breaker)

**What it does:** the TypeScript wallet-scoring pipeline and Python's per-trader circuit breaker
each own a disjoint set of columns on `wallet_profile` — neither can silently overwrite the
other's decision.

**How it works mechanically:** `wallet_profile.status`/`statusReason`/`statusChangedAt` (and the
scoring sub-columns) are written exclusively by `scoreWallets.ts`'s single write function
(`upsertWalletProfile`) — `db.py` never writes them (`migrate_to_sqlite.py` seeds `status` once,
at first insert only, as a one-time migration artifact, not an ongoing write path).
`circuitBreakerMuted`/`muteReason`/`mutedAt`/`consecutiveLosses`/`recentResultsJson` are written
exclusively by `bot.py`'s circuit breaker via `db.py`. The safety boundary is enforced by
omission: `upsertWalletProfile`'s Drizzle `onConflictDoUpdate` only touches the columns
explicitly listed in its `set` clause, and those five circuit-breaker columns are simply never
named there — so a monthly re-score run structurally cannot un-mute a trader bot.py just muted,
even by accident.

**System costs & trade-offs:** the two systems can disagree without either being "wrong" — a
wallet can be `status = "track"` (scoring likes its recent numbers) while also
`circuitBreakerMuted = true` (the bot's own copy performance on it has been bad). `bot.py`
checks the mute flag independently of `status`, so a muted-but-still-tracked wallet is correctly
skipped either way, but the two signals are never reconciled into one number.

**Why it exists:** without an explicit split, a monthly re-score (Rule 12) could plausibly reset
a mute a human or the circuit breaker deliberately set, re-exposing the bot to a trader it had
just learned to avoid.

---

## 10. Execution shortfall tracking

**What it does:** every paper copy also fetches the CURRENT executable price and logs the signed
difference against the source trader's own fill price — a real measurement of how much worse (or
better) this copy would have executed live, without changing the recorded paper fill or P&L.

**How it works mechanically:** `measure_paper_shortfall()` calls `bullpen polymarket preview`
(same shape of call as Rule 4's spread guard) and passes the result through
`compute_shortfall()` — pure arithmetic, unit-testable without a live call. Values are SIGNED so
positive always means "our copy would execute worse than the source trader's fill," for both
sides: on a BUY, worse means paying MORE (`executable_price > source_price`); on a SELL, worse
means receiving LESS. Negative values (we'd have filled BETTER — price moved our way during the
copy delay) are kept, not clamped, because the whole point is the SIGNED AVERAGE over many
copies. Results land in `bot_event_log.payload_json` as `shortfall_pct`/`shortfall_usd` — no
schema change was needed. This is also the best available maker/taker signal: the trade feed
itself carries no maker/taker flag, but a wallet whose copies consistently show shortfall close
to the market's own spread is very likely a maker (their fills sit at the bid/ask; a copy has to
cross the spread to follow them).

**System costs & trade-offs:** one extra `bullpen preview` subprocess call per paper copy — pure
overhead with no effect on the trade itself. Fails soft (`shortfall_status` field records the
failure) so a lost measurement never blocks or delays the actual copy. By design this number
NEVER feeds back into paper accounting — meaning paper P&L stays structurally optimistic
(it will always look better than a live run of the same trades), which is a known, accepted
distortion, not an oversight.

**Why it exists:** paper mode's biggest blind spot is that a paper fill always executes at the
source trader's exact reported price — a price a live copy could never actually achieve, since
real execution is always seconds behind the trade it's copying. This measures that gap directly
instead of assuming it. The plan (Rule 12) is to let this accumulate for several weeks, then feed
average per-wallet shortfall into the scorer as a copyability penalty.

---

## 11. Disciplined Taker: pre-trade slippage ceiling

**What it does:** before any LIVE buy, aborts the copy entirely — before any order is submitted —
if the market has already moved against the copy by more than `config.SLIPPAGE_TOLERANCE = 5%`
since the source trader's own fill.

**How it works mechanically:** `check_slippage_ceiling()` in `bot.py` (added 2026-07-19) compares
`source_price` (the tracked trader's own fill) against `executable_price` (a fresh preview price)
and computes the adverse move as a signed percentage. It is **proactive**, not reactive: it reuses
the exact preview price `check_spread_tolerance()` (Rule 4) already fetched for the same trade —
zero extra network round-trip — and runs BEFORE any order is placed. This is a distinct
mechanism from the existing `--max-price`/`--min-price` bounds already passed to the order
itself (same `SLIPPAGE_TOLERANCE` constant, reused not duplicated): the order-level bound only
stops a BAD FILL, but the order can still rest unfilled on the book at the limit price rather
than cleanly not happening — and could come back to fill hours later against a signal that's no
longer live. The pre-trade check prevents that order from ever being submitted at all.
BUY-only by design (checked via the `side` parameter, though the function is side-aware in case
a deliberate SELL use is ever added) — the same "never gate an exit" principle as Rule 6.

**System costs & trade-offs:** no additional network cost (reuses Rule 4's call). The trade-off
is entirely in the threshold value: `SLIPPAGE_TOLERANCE` was raised from 3% to 5% on 2026-07-15
specifically to cut false "unresolved trade" misses on fast-moving markets — the accepted cost of
that widening is that copies which have drifted meaningfully worse than the source trader's price
will now go through instead of aborting, which matters more on thin/low-liquidity markets. Paper
mode is unaffected directly (this only gates LIVE orders), but Rule 10's shortfall measurement
already shows what this check would have caught, even in paper mode.

**Why it exists:** without this, a market that moved significantly between the source trader's
trade and the bot's execution attempt would still get an order SUBMITTED — protected only by the
order's own price bound, which can rest unfilled rather than cleanly failing. Added 2026-07-19
alongside Rule 6's portfolio controls, as part of the same strategic goal: making it safe to
track MORE wallets by trusting the system to reject bad executions automatically, rather than
relying on a small, hand-curated, closely-watched list.

---

## 12. Phase 2 strategic scoring (planned, not yet built)

**What it does:** four scoring concepts, agreed 2026-07-19, meant to move the TypeScript wallet
scorer (`packages/copy-trading/src/scoreWallets.ts`) beyond win-rate/consistency alone. **None of
these four are built yet** — this section is a design target, explicitly gated on Rule 10's
shortfall data accumulating first, not a sprint plan.

**Important clarification, since this has caused confusion before:** the scorer's rule_set v3
(current, live today) ALREADY enforces two hard gates that are easy to mistake for "Phase 2"
work:
- A **toxic-flow consistency gate** (`minConsistencyScore = 0.20`): any wallet whose
  rolling-window consistency score falls below this is force-`"ignore"`d regardless of ROI or win
  rate — a signature of volume-farming (many small "winning" trades whose mark-to-market series
  is too choppy to trust). In its first real run, this gate alone cut 16 of 23 pass-2 survivors.
- A **recency ("zombie") gate** (`maxDaysSinceLastTrade = 30`): any wallet inactive for 30+ days
  is force-`"ignore"`d regardless of historical score.

Phase 2 is about *new* concepts and *tightening* those existing numbers — not building the gates
from scratch, which is already done.

**How it works mechanically (planned):**
- **Win-rate vs. risk-reward**: a high win rate is worthless — or actively dangerous — if a
  single loss wipes out many wins' worth of gains. Needs a payoff-ratio term weighted at least as
  heavily as raw win rate, to penalize "penny-picker" wallets.
- **Domain-expertise filtering**: score and gate wallets PER CATEGORY (the `primary_category`
  field already exists, unused, in `leaderboard_scan.raw_json`), not just overall — a wallet
  that's an expert in US Politics isn't automatically trustworthy betting on Sports.
- **Conviction-sizing anomaly detection**: a wallet betting well above its own historical average
  size is a signal worth scaling copy size up for — but needs a filter that distinguishes genuine
  conviction from "their bankroll just grew." This is the highest-blast-radius item on the list,
  since it directly scales real position size.
- **Recency tightening**: revisit the existing 30-day cutoff toward a stricter number (e.g. 14
  days) once shortfall data (Rule 10) shows how fast "hot" actually decays.

**System costs & trade-offs (anticipated):** tightening the consistency or recency gates further
will shrink the qualifying wallet pool — potentially below `topNPoolSize`/`MIN_TRACKED_TRADERS`
(Rule 2), which is exactly why this is gated on real data rather than intuition. Domain-expertise
filtering multiplies the number of scores tracked per wallet (one per category instead of one
overall), increasing both DB storage and the number of `bullpen` calls needed per scoring run.

**Why it exists:** win-rate and consistency alone don't capture risk-reward shape, domain
specialization, or conviction signals — real gaps identified after the v3 rule_set (toxic-flow
gate) was already live. Deliberately NOT built yet: doing this before enough shortfall data
exists would mean tuning against intuition instead of evidence, which is the same mistake the
toxic-flow gate's threshold (0.20) was chosen carefully to avoid.

---

## What is intentionally still simple

The current setup is conservative by design — it focuses on avoiding obvious bad fills, bad
liquidity, and weak traders, rather than being a full multi-factor execution engine. Rule 11
(Disciplined Taker) and Rule 6 (portfolio risk manager) are meaningful steps toward one; Rule 12
(Phase 2 scoring) is the planned next step beyond that.

## Roadmap / status of previously "likely future changes"

- Stronger portfolio-level risk controls — **done** (Rule 6), 2026-07-18.
- More advanced maker/taker logic — **in progress**: shortfall tracking is live (Rule 10) as a
  proxy signal; scoring it into the wallet scorer is still pending enough real data.
- More explicit scoring rules for entry/exit decisions — **in progress**: the pre-trade slippage
  ceiling is done (Rule 11); the Phase 2 scoring concepts (Rule 12) are logged, not built.
- Optional live-trading rollout, only after review — **still fully pending**;
  `TRACKED_TRADERS_SOURCE` also stays `"static"` until a scored wallet batch is reviewed (Rule 2).
- Not yet started: the weather arbitrage bot (separate, isolated system — see
  `docs/CURRENT_STATE.md`); remaining Next.js dashboard pages (only Overview exists today);
  `reviewOutcomes.ts`/`updateRules.ts` (the score-vs-actual-outcome validation loop that would let
  scoring rules self-tune from real data instead of hand-picked thresholds).
