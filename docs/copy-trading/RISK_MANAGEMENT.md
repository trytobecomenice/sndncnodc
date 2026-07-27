# Risk Management & Tracking Rules

**Audience note:** this document assumes zero prior context on this project. It's written for
a quant developer who needs to understand every automated risk control this system enforces —
what it does, exactly how it's implemented, what it costs, and why it was built — well enough
to extend, tune, or challenge any of it without archaeology through the codebase first.

**This is the single source of truth for "what rules currently apply."** Reference it before
deciding on a new restriction, so an old decision doesn't get silently re-thought or
contradicted. `docs/copy-trading/SAFETY.md` is the engineering companion: shared-database ownership rules,
migration/cutover runbooks, and deeper implementation notes that don't fit the rule-by-rule
format below. Keep both current in the same commit when a rule changes — don't let them drift.

## System context, in one paragraph

This is a Polymarket copy-trading bot. `bot.py` polls a live trade feed (via the external
`bullpen` CLI, which owns all Polygon/Polymarket auth and signing — this codebase never touches
a private key) for trades made by a set of "tracked" wallets, and mirrors qualifying trades as
its own paper (simulated) or live (real-money) orders, sized independently of the source
trader's own size (`compute_trade_size_usd()`, confidence-weighted by the source wallet's score
since 2026-07-22 — see Rule 15; $3-$10/trade currently, was a flat $5). State — open positions,
trade history, per-trader mute status — lives in a shared SQLite database (`data/app.db`) that
Python (`bot.py`/`db.py`) and a TypeScript research layer (`packages/copy-trading`) both touch,
under a strict ownership split (Rule 8). Every rule below is a gate that can reject or abort a
trade before it happens; **none of them retroactively unwind a trade that already executed.**

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

### Summary: the three problems this solves

To make it safe to track many wallets at once rather than relying on a
small, closely-watched list, this system addresses three fundamental
problems in keeping risk bounded across the WHOLE book, not one trade at a
time.

#### 1. The "aggregate exposure" problem

**Challenge:** a portfolio can accumulate dangerous total risk one
individually-fine trade at a time — no single copy looks reckless, but
their sum can be.

- **Mechanism: total exposure ceiling.** Sum of open cost basis across every
  position, plus the new trade, may not exceed `config.MAX_TOTAL_EXPOSURE_USD`
  ($1250, raised from $250 on 2026-07-25 — see this rule's "Why it exists"
  for the evidence that motivated it). Uses cost basis, not live market
  value, so it needs no price fetch and can't swing between two checks
  taken seconds apart.

#### 2. The "hidden concentration" problem

**Challenge:** several markets can look like separate, small, unrelated bets
while actually being one large correlated bet on the same underlying
question (e.g. multiple strike-price outcomes on the same event).

- **Mechanism: per-event exposure cap.** A second, tighter ceiling
  (`config.MAX_EVENT_EXPOSURE_USD`, $30) scoped to one Polymarket *event*
  (which can span several markets). Event resolution **fails closed** — if
  the market's parent event can't be determined, the buy is skipped rather
  than letting unattributable exposure bypass the cap.

#### 3. The "catastrophic drawdown" problem

**Challenge:** how to stop trading before a losing streak, or something
systemic going wrong, erodes the bankroll past the point of recovery.

- **Mechanism: drawdown kill switch.** Halts all new BUYs if portfolio
  equity drops below an absolute floor (`config.EQUITY_FLOOR_USD`, $900,
  scaled from $100 on 2026-07-25 alongside `PAPER_BANKROLL_USD`'s
  $125->$1125 raise — same 80%-of-bankroll ratio preserved, not a fixed
  dollar amount left stale against a much larger base) or falls
  `config.MAX_DRAWDOWN_FROM_PEAK_USD` ($450, same 40%-of-bankroll ratio,
  scaled from $50) below its own high-water mark — whichever trips first.
  The halt **latches**, persists across restarts, and requires a
  deliberate manual reset (`python3 reset_kill_switch.py`) — never
  auto-clears.

All three gate new BUYs only, never sells/exits — a risk layer that traps
you in a losing position adds risk instead of removing it (this principle
repeats at Rule 11).

---

**What it does:** three independent controls, all in `risk_manager.py` (added 2026-07-18), that
cap risk across the WHOLE book rather than one trader at a time:
- **Total exposure ceiling** (`config.MAX_TOTAL_EXPOSURE_USD = $1250`): sum of open cost basis +
  the new trade may not exceed this.
- **Per-event exposure cap** (`config.MAX_EVENT_EXPOSURE_USD = $30`): same check, scoped to one
  Polymarket *event* (which can span several correlated markets, e.g. multiple strike-price
  outcomes on the same underlying question). Deliberately NOT raised alongside the total ceiling
  — see "Why it exists" below.
- **Drawdown kill switch**: halts all new BUYs if portfolio equity drops below an absolute floor
  of `config.EQUITY_FLOOR_USD = $900`, or falls `config.MAX_DRAWDOWN_FROM_PEAK_USD = $450` below
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

**2026-07-25 update — total exposure ceiling raised $250 -> $1250, evidence-driven, not a
round-number guess.** Real data over the prior 2 days showed the OLD $250 cap had become the
dominant bottleneck on trade volume, not signal quality or the TCA/significance filters:
`skip_risk_exposure_ceiling` fired 3,347 times in that window, **100% against this specific total
cap** (not the per-event or per-wallet ones), while the portfolio sat at $247.22/$250 — blocking
416 distinct (wallet, market) signals that never got a chance. Joey added $1000 of real capital to
back the higher ceiling — `PAPER_BANKROLL_USD` moved from $125 to $1125 to match, and
`EQUITY_FLOOR_USD`/`MAX_DRAWDOWN_FROM_PEAK_USD` were scaled proportionally (same 80%/40%-of-bankroll
ratios as before) rather than left as stale, now-far-too-tight dollar amounts. `MAX_EVENT_EXPOSURE_USD`
($30) and `MAX_WALLET_EXPOSURE_USD` ($50, Rule 26) were deliberately left UNCHANGED — fully
utilizing the new $1250 ceiling now requires broader diversification (~42 events / ~25 wallets)
rather than bigger single-event/single-wallet bets, a reasonable side effect of raising the total
cap alone, not an inconsistency the way leaving the kill-switch thresholds unscaled would have
been.

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
latency/API-load trade-off, not a bug. **Migrated 2026-07-22**: `get_market_prices()` no longer
calls `bullpen polymarket price` — like Rule 10's simulator cutover, this is a read-only price
check with no custody/signing involved, so it moved onto
`polymarket_simulator.fetch_order_book_for_outcome()` (the same direct order-book fetch the
simulator already uses). `best_bid` is the top of our own re-sorted bid side; the
`indicative_price` fallback chain (`best_bid` -> midpoint of both top-of-book sides, computed only
as a defense against a degenerate/malformed API value since a real book's top bid can't
legitimately be 0 -> `last_trade_price`, a genuine field on the same `/book` response, not a
second call) preserves the exact same contract as before. If the fetch itself fails, that position
simply isn't re-evaluated that cycle (fails soft, not closed) rather than forcing a stale-priced
exit — unchanged behavior, just a different upstream. The same fails-soft principle applies to the
closeout sweep (still bullpen-backed — closeout is an execution action): a market whose lookup
fails is left alone and retried next sweep, and since 2026-07-19 repeated identical lookup failures
are throttled in the event log (first failure logged, then one reminder per ~24 consecutive
failures with a running count — `_closeout_fetch_failures` in `bot.py`) so a bullpen backend outage
can't flood `bot_event_log` with hundreds of duplicate error rows the way one did before the
throttle existed.

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

**A real, material gap found 2026-07-21 while validating a new tracking-feed source (Rule 14):
`size_usd` — used throughout this rule's shortfall math — is the PRE-FEE nominal trade value
(shares × price), not the actual on-chain settled amount.** Polymarket charges a real taker fee
(makers pay zero); the formula, confirmed exactly against `docs.polymarket.com/trading/fees` and
verified against two independent real trades (both backed out to `feeRate = 0.05000`, exactly
matching the documented Sports category rate, to 5 decimal places):

```
fee = shares × feeRate × price × (1 - price)
```

`bullpen`'s own `size_usd` field reports the pre-fee nominal value; Polymarket's own on-chain
`usdcSize` is the real, fee-inclusive settled amount.

**Correction, traced precisely 2026-07-22 rather than assumed: historical shortfall/PnL does NOT
need recalculating.** `compute_shortfall()` (this rule's actual math) works purely on **prices**
(`source_price` vs. `executable_price`) — and price matched exactly between bullpen and the direct
API in every comparison run. The fee gap above only ever lived in `size_usd`, a field
`compute_shortfall()` never reads. So the 6+ historical shortfall numbers were correctly measuring
what they were built to measure (price slippage) — not wrong, just half the story.

**The real, separate gap, found and FIXED the same day**: `measure_paper_shortfall()` calls
`bullpen polymarket preview`, which already returns an explicit `trading_fee` field (confirmed
live: a real preview showed `trading_fee: 1.2` on a $100 trade, with `total_cost: 101.21` correctly
including it) — and the function was silently discarding it, extracting only `preview.get("price")`.
**Fixed 2026-07-22**: `measure_paper_shortfall()` now also captures `trading_fee_usd`,
`network_fee_usd`, and a combined `total_cost_usd` (slippage + both fees — the genuine all-in cost
of a copy), as new fields alongside the existing `shortfall_pct`/`shortfall_usd` (left with their
original meaning unchanged, not redefined). 7 new unit tests (`TestComputeShortfall`,
`TestMeasurePaperShortfall`) cover the pure math and the fee-capture behavior, including graceful
handling when a preview has no fee fields at all.

**System costs & trade-offs (addendum):** the documented fee formula also confirms a real,
independent way to detect maker vs. taker status per trade (a `feeRate` of exactly 0 = maker; a
`feeRate` matching a real category rate = taker) — a stronger signal than the "shortfall close to
the spread" heuristic originally described above, since it comes directly from the actual on-chain
settlement math rather than an inference. Not yet wired into anything (still just available in the
data) — a natural future input to Rule 12's scoring plans.

**Cutover 2026-07-22: `measure_paper_shortfall()` no longer calls bullpen at all.** Now that Rule
14 fully replaced bullpen for tracking, the last bullpen dependency in paper mode was this rule's
`bullpen polymarket preview` call — replaced with `polymarket_simulator.simulate_fill()`, which
walks Polymarket's own public order book directly. Paper trading is now 100% independent of
bullpen; execution (Rule 1) is unaffected and still goes through bullpen's signing path.

**How it works mechanically:** two more public, no-auth Polymarket endpoints, verified live before
building against them:
- `gamma-api.polymarket.com/markets?slug=<slug>` returns `outcomes` and `clobTokenIds` as
  same-order arrays (so an outcome name maps directly to a CLOB token ID), plus `feesEnabled` and
  `feeSchedule.rate` — **the exact per-market taker fee rate, straight from Polymarket's own market
  data.** This resolved the open question from the initial review (whether a market's fee category
  would need deriving): it doesn't. Confirmed across 5 live markets that the rate varies (0.04,
  0.05) and can be disabled entirely (`feesEnabled: false`, no `feeSchedule`) — both handled.
- `clob.polymarket.com/book?token_id=<id>` returns the live order book (`bids`/`asks`, each
  `{price, size}`). **Not trusted at face value**: live testing found bids come back sorted
  ascending (best/highest bid LAST) while asks come back descending (best/lowest ask LAST) —
  inconsistent with each other and undocumented either way, so `polymarket_simulator.py`
  explicitly re-sorts both sides itself rather than relying on that ordering.
- A book-walker (`_walk_asks_for_usd` for BUY, `_walk_bids_for_shares` for SELL) computes the
  real volume-weighted fill price across as many price levels as the requested size needs,
  applying the confirmed fee formula (`fee = shares × feeRate × price × (1-price)`) **per level**
  (summed), not once against the average price — correct since the formula is nonlinear in price.
  A copy trade is always a taker fill (crossing the spread to buy/sell immediately), matching
  every checked market's `feeSchedule.takerOnly: true`, so there's no maker/taker ambiguity to
  resolve for this use case.
- If the visible book can't fill the full requested size, the result is flagged
  `insufficient_liquidity: true` with `shares_filled` — surfaced through to the logged event
  rather than silently reported as a full fill.
- `network_fee` is always `0.0` here, and this is NOT a placeholder: this module never broadcasts
  a transaction (paper mode only), so there's no real gas cost to estimate. bullpen's preview
  number was an estimate of a REAL execution's gas — a different, no-longer-applicable quantity.
- 17 new unit tests (`test_polymarket_simulator.py`) plus 2 more in `TestMeasurePaperShortfall`
  cover market-info parsing, token-ID resolution, book re-sorting, the book-walker's multi-level
  and insufficient-depth math, and end-to-end `simulate_fill()`. Live-verified against a real
  market (`new-rhianna-album-before-gta-vi-926`): a $50 BUY simulated to price 0.51, fee $1.225
  (exactly matching the formula); a warm second call (SELL, walking the bid side) completed in
  ~0.7s using the reused persistent connection.

**System costs & trade-offs:** two HTTP calls per measurement instead of one bullpen subprocess
call — cold ~2.7s, warm (persistent thread-local connections, same pattern as Rule 14) ~0.7s.
Deliberately did NOT share connection/retry code with `polymarket_data_api.py` (Rule 14's LIVE
tracking feed): refactoring that module to share code here would have put the live, currently-
running tracking feed at risk for a nice-to-have, not something this change needed — the pattern
(thread-local per-host connection, 429/5xx exponential backoff, other 4xx not retried) is
intentionally duplicated as a small, separate, independently-reviewable copy in
`polymarket_simulator.py` instead.

**Why it exists:** paper trading's own simulation step no longer needs a third-party CLI wrapper
at all — it's sourced entirely from Polymarket's own public APIs, using the exact same fee
schedule and order book the real exchange would fill against. Live execution (Rule 1) intentionally
stays on bullpen — see `docs/copy-trading/SAFETY.md` §6: building signing/execution into this
codebase would mean holding a private key here, which is the one thing this project has always
ruled out regardless of engineering convenience.

---

## 11. Disciplined Taker: pre-trade slippage ceiling

### Summary: the two problems this solves

To stop a copy from executing against a price the source signal no longer
reflects, this system addresses two distinct failure modes — one already
handled elsewhere, one that needed a new mechanism.

#### 1. The "bad fill" problem

**Challenge:** the market can move between the source trader's fill and our
own order reaching the exchange — submitting blind risks paying a much
worse price than the trade we're supposedly copying.

- **Mechanism: order-level price bound** (pre-existing, not this rule) —
  every live order already carries a `--max-price`/`--min-price` bound
  (`config.SLIPPAGE_TOLERANCE`), so a fill, if it happens, can't be
  arbitrarily bad.

#### 2. The "stale resting order" problem

**Challenge:** a price bound alone doesn't stop the order from being
*submitted* — if it doesn't fill immediately, it can rest on the book at
that limit price and execute hours later, against a signal that's no longer
live at all.

- **Mechanism: pre-trade slippage ceiling.** `check_slippage_ceiling()`
  compares the source trader's own fill price against a fresh preview price
  and **proactively aborts the copy before any order is submitted** if the
  market has already moved more than `SLIPPAGE_TOLERANCE` (5%) against it —
  reusing the same preview call Rule 4 already made, zero extra network
  cost. BUY-only, same "never gate an exit" principle as Rule 6.

---

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

## 13. Auth-failure isolation (halt-and-recover, not silent retry)

**What it does:** when the `bullpen` session dies (expired auth token), the bot stops hammering
the tracker feed every 30s and instead halts cleanly, logs it loudly once, and waits at a much
slower cadence until a human re-runs `bullpen login` — rather than silently retrying at full
speed for however long the session stays broken.

**How it works mechanically:** added 2026-07-21 after a real incident traced through
`bot_event_log`: two separate ~1-2 hour windows on 2026-07-18 where the session was dead and the
bot logged an identical "poll cycle failed" error every ~28 seconds, continuously (264 rows in
one window alone) — indistinguishable in the log from an ordinary transient blip, and directly
responsible for the two worst entries in Rule 10's shortfall data (a trade detected 54 minutes
late showed **-58.7% shortfall**, by far the largest in the dataset — a stale-data problem, not a
speed problem). `bullpen_client.py` now classifies a `bullpen` exit code 2 ("Authentication
failure," the CLI's own documented code) as a distinct `BullpenAuthError`, separate from the
generic `RuntimeError` every other failure raises. `bot.py`'s `fetch_feed_with_auth_recovery()`
catches it specifically: logs one `auth_halted` event, persists `bot_risk_state["auth_halt"]`
(same persisted-flag pattern Rule 6's kill switch already uses, so the halt is visible outside
this one process, not just in that run's own log), then waits at
`config.AUTH_RECHECK_INTERVAL_SECONDS` (120s, vs. the normal 30s poll) and re-attempts only the
feed call as its own recovery probe — repeats are throttled to a reminder every 30th recheck
(~hourly) rather than every 120s, mirroring the existing `_closeout_fetch_failures` throttling
pattern. The moment the feed call succeeds again, it logs `auth_recovered`, clears the flag, and
resumes normal polling immediately — no restart needed. **Deliberately does not attempt any
non-interactive self-repair**: live-checked against `bullpen`'s own diagnostics while a session
was genuinely dead, and it reported `resolution_owner: "user"` / `next_action: "bullpen login"` —
a real login is the only fix, so the bot's job is to notice fast and stop wasting cycles, not to
pretend it can fix this itself. Unit-tested (`test_bullpen_client.py`, 4 tests) — specifically
that exit code 2 raises `BullpenAuthError` and no other exit code does, since that classification
is the one fact this entire fix depends on.

**System costs & trade-offs:** while halted, the bot also skips the TTP and closeout sweeps for
that cycle (they'd fail against the same dead session anyway) — meaning a position needing a
trailing-stop exit or a resolved-market closeout during a halt is delayed until recovery, same as
it already was under the old behavior, just now without the error spam. No alerting channel
beyond the log/console/persisted flag exists yet (no push notification, no dashboard surfacing —
`dashboard.py` doesn't currently read `bot_risk_state` at all, for this flag or for the pre-
existing kill switch either) — a real gap, not hidden: right now, noticing a halt still requires
either watching the console or checking the event log/`bot_risk_state` directly.

**Why it exists:** direct incident, not a hypothetical — this is the single largest lever found in
Rule 10's own shortfall data when the underlying cause was actually investigated (see Rule 10's
data), bigger than anything polling-cadence or maker/taker order-type changes would address at
the current trade volume. A session that's actually dead retried at full speed doesn't just waste
cycles, it actively produces worse outcomes than doing nothing: a trade detected an hour late
because the bot never stopped trying is worse than the same bot cleanly admitting it couldn't
see anything until a human fixed it.

---

## 14. Direct Polymarket tracking feed (replacing bullpen for tracking only) — LIVE since 2026-07-22

**What it does:** a new, in-progress replacement for `bullpen tracker feed` — polls Polymarket's
own public Data API directly, per tracked wallet, instead of going through `bullpen`. Scoped
narrowly: this affects WALLET TRACKING (detection) only. Trade EXECUTION (buy/sell) stays on
`bullpen`'s signing path regardless — self-custodying a key to remove `bullpen` from execution
too would contradict `docs/copy-trading/SAFETY.md` §6 ("why private keys never belong in this
app"), a founding principle, not something this change touches.

**How it works mechanically: built and validated 2026-07-21, NOT yet wired into bot.py's live
poll loop.**

- **Why this was even considered**: raised as an architecture pivot proposal (2026-07-21) to
  remove `bullpen`'s auth dependency from tracking entirely (Rule 13 already patches the
  symptom — silent error storms — but doesn't remove the underlying dependency) and reduce
  detection latency. The original framing was a raw Web3/RPC on-chain listener; investigated and
  corrected before building anything: Polymarket's real-time WebSocket channels are organized by
  MARKET (condition ID/event slug), not by wallet — no genuine "push me every trade by wallet X
  across the whole platform" channel exists in their public infra, so a raw listener would still
  need per-market subscriptions to markets not yet known in advance. The actual viable path,
  verified live: Polymarket's own public REST Data API (`data-api.polymarket.com/activity?user=
  <wallet>`), no authentication required (confirmed via direct call, no API key, no session),
  wallet-filterable, with fields that map 1:1 onto `bullpen`'s own trade shape (confirmed
  `transaction_hash`/`transactionHash` are the literal same value in both, proving `bullpen`
  itself is just a wrapper over this same underlying data for this specific piece).
- **`polymarket_data_api.py`** (new file) — `fetch_wallet_trades()` (one wallet), `fetch_all_
  wallets_concurrent()` (all tracked wallets via a `ThreadPoolExecutor`, since sequential
  per-wallet polling would take ~20s for 20 wallets — barely better than `bullpen`'s 30s poll),
  and `normalize_activity_record()` (the field-mapping adapter, so `process_trade()` needs zero
  changes). Two real environment/access bugs found and fixed during this work, not assumed away:
  this Python installation's SSL certificate bundle was broken (fixed via python.org's own
  standard `Install Certificates.command`), and the API 403s on Python's default `urllib`
  User-Agent string specifically — fixed with a plainly self-identifying header (`polymarket-
  copybot/1.0 (+personal research bot)`), not browser impersonation (same line already held for
  Wunderground, Rule 5 of `docs/weather/WEATHER_RISK_MANAGEMENT.md`).
- **Speed, investigated properly rather than assumed**: bumping thread-pool size (10 → 30 workers)
  barely moved total time — the real cost is per-request TLS/connection setup, confirmed by direct
  comparison (fresh connection ~0.7-0.9s per request; a reused connection ~0.2-0.4s). Fixed via a
  thread-local persistent `http.client.HTTPSConnection` per worker thread, reused across calls —
  but this only pays off across MULTIPLE poll cycles, which requires the caller (bot.py, once
  wired in) to create ONE long-lived `ThreadPoolExecutor` (`make_persistent_executor()`) at
  startup and reuse it for the life of the process, not a fresh one per cycle. **Live-confirmed
  across 3 simulated poll cycles against all 20 real tracked wallets**: fresh-executor-per-cycle
  held steady at ~2.3-2.4s/cycle; persistent-executor paid a higher first cycle (3.78s) then
  dropped to 0.40-0.46s on cycles 2-3 — meaningfully faster than `bullpen`'s own ~1.4s single call
  once warm. Deliberately stayed stdlib-only (`http.client`, not `requests`) to preserve this
  project's existing, explicitly-stated zero-third-party-package policy rather than trade it away
  for less custom code.
- **`validate_direct_feed.py`** (new file) — read-only, side-effect-free comparison tool (never
  touches `bot.py`'s live state: `seen_trade_ids`, `positions`, `bot_event_log`). Matches trades
  between the two sources by `transaction_hash` (an exact key, not fuzzy). **Real findings from
  running it live**: 2 trades where `bullpen` returned an empty `market_slug`/`outcome` while the
  direct API had full data (a point in the direct API's favor); and the `size_usd` discrepancy
  that turned into Rule 10's fee-accounting finding above — validation surfaced something bigger
  than "does this new source work."

**System costs & trade-offs:** no bulk/multi-wallet endpoint exists on Polymarket's Data API
(tested live: both a comma-separated `user` value and a repeated `user=A&user=B` param fail or
silently drop to one wallet) — genuine per-wallet requests are required regardless of approach,
so concurrency is the only lever, not a bulk-query shortcut. The first poll cycle after a restart
is unavoidably slower (cold connections) — a one-time cost, not a per-cycle one. This still
requires `bullpen` for execution, so it does not remove `bullpen` as a dependency for this system
overall, only for the tracking half.

**Why it exists:** direct request, discussed and corrected before building (the original
Web3/RPC-listener framing would have been substantially more complex — ABI decoding, chain-reorg
handling — for no additional benefit once it was confirmed Polymarket's own real-time channels
don't support per-wallet subscription anyway). Removes a real, already-incident-causing
dependency (Rule 13) from the tracking path entirely, not just patches around it.

**LIVE as of 2026-07-22.** `bot.py`'s poll loop now uses the direct Polymarket Data API
exclusively for tracking — `bullpen tracker feed` is no longer called anywhere in the main loop or
bootstrap. Direct instruction: "Bullpen clearly has too many structural blind spots and black-box
bugs. Let's officially set your Direct API tracker as the primary feed... completely replacing
bullpen's tracking feed."

**How the cutover was executed, mechanically:**
- `bot.py` main(): one long-lived `ThreadPoolExecutor` (`make_persistent_executor()`) created ONCE
  at startup, sized to `max(len(wallet_addresses), 10)`, passed into every `fetch_direct_feed()`
  call for the life of the process — required for the persistent-connection speedup (a fresh
  executor every cycle would discard worker threads and their warm connections). Shut down
  cleanly (`executor.shutdown(wait=False)`) when the process stops.
- New `fetch_direct_feed(executor, wallet_addresses)` replaces both the bootstrap block's and the
  main loop's `run_bullpen_json(["tracker","feed",...])` calls. Returns the exact same `{"trades":
  [...]}` shape the old bullpen response had, so `process_trade()` and everything else downstream
  needed zero changes. A single wallet's fetch failure is logged as a normal `error` event, not
  raised — the poll cycle continues normally for every other wallet, and the failed one is simply
  retried next cycle.
- `fetch_feed_with_auth_recovery()` (Rule 13) is no longer called by the main loop — there is no
  bullpen session involved in tracking at all anymore, so it has nothing to recover from for this
  path. Left in the file, still tested, since bullpen remains genuinely in use elsewhere (preview
  calls for shortfall measurement, closeout sweep market checks, and live execution) where the
  same halt-and-recover pattern could still matter.
- **Trade-ID format migration, handled deliberately, not silently.** Bullpen's `trade_id` was a
  UUID; the direct API's adapter produces a composite `tx_hash:asset:side:timestamp` string (Rule
  14's own multi-fill-safety design) — completely different formats for the same underlying trade.
  Switching the feed source without addressing this would have made every one of the ~1,233
  already-seen trades in `bot_seen_trade` look brand new again on the very first poll, replaying
  over a thousand stale paper copies. Handled by treating the cutover exactly like a fresh
  install: stopped `bot.py` (graceful SIGTERM), cleared `bot_seen_trade` (a deliberate, one-time
  migration — not something to repeat), restarted — reusing the EXISTING, already-tested bootstrap
  logic (`bootstrap = not state["seen_trade_ids"]`) rather than adding new migration-detection
  logic to `bot.py` itself. Live-verified: bootstrap correctly baseline-skipped 400 real trades
  using the new feed source, `bot_seen_trade` now contains only the new composite-format IDs, zero
  errors on restart or since.
- New tests: `test_bot_risk_checks.py` gained `TestFetchDirectFeed` (shape compatibility, per-
  wallet error isolation) alongside the new `TestComputeShortfall`/`TestMeasurePaperShortfall`
  classes (see Rule 10 addendum below) — 52 tests passing across the whole suite post-cutover.

**strict-1 verified, live, item 3 of the same request.** This was the wallet that showed the
starkest anomaly in the pre-cutover investigation — already registered with bullpen, yet
consistently invisible in its feed despite heavy real activity. Directly confirmed post-cutover:
fetched strict-1's current trades via the new direct feed, confirmed its most recent
`transactionHash`-derived `trade_id` is present in `bot_seen_trade` (captured during the fresh
bootstrap) — strict-1 is now fully, provably visible. Separately confirmed its apparent "silence"
in the minutes right after cutover was real market quiet time (its most recent trade predated the
new bootstrap by ~26 minutes, confirmed via a fresh direct fetch), not a bug — the process had live
persistent HTTPS connections established throughout (confirmed via `lsof`) and zero errors logged.

**Hardened against rate limits and pagination gaps, 2026-07-22 — reviewed against Polymarket's
official API docs specifically, not assumed.** Two real gaps found and fixed:

1. **429/5xx handling**: `fetch_wallet_trades` previously raised immediately on ANY non-200
   status, with zero retry or backoff — confirmed by re-reading the code, not assumed. Polymarket's
   own docs don't publish a specific rate-limit number for this endpoint at all; a credible
   third-party source (not Polymarket's own docs) claims a Cloudflare-enforced, cross-account limit
   with no `Retry-After` header on a 429. Fixed defensively for the worst case: `_fetch_one_page`
   now retries HTTP 429 and 5xx with exponential backoff (1s, 2s, 4s, 8s — `RATE_LIMIT_MAX_RETRIES
   = 4`), while any OTHER non-200 status (400, 404 — permanent/logic errors) still raises
   immediately, since backing off a request that's inherently wrong wastes time without changing
   the outcome.
2. **Pagination**: Polymarket's own docs confirm `/activity` supports `offset`-based pagination
   (`limit` 0-500, `offset` 0-5000). `fetch_wallet_trades` previously fetched exactly one page —
   meaning a wallet trading more than the requested `limit` within one poll interval would have had
   the oldest of that burst silently, permanently missed (the next poll only sees the newest N,
   never circles back). Fixed: automatically pages through `offset`, concatenating results, until a
   page comes back short (fewer than `limit` — no more data) or `MAX_PAGES_PER_FETCH` (10) is
   reached. At `config.DIRECT_API_PER_WALLET_LIMIT = 20` per page, this comfortably covers up to
   200 trades per wallet per poll before truncating — a very generous margin over any plausible
   30-second burst.

7 new unit tests (`TestPagination`, `TestRateLimitBackoff`) — full pagination across multiple pages,
the max-page safety cap, 429 and 5xx retry-then-succeed, giving up after max retries, and confirming
a permanent 4xx is never retried. Live-verified against the real API with `limit=5` forced against
a high-volume wallet: correctly paged across 10 real requests, 50 real trades returned. Deployed:
`bot.py` restarted with these fixes; a real trade from `geo-denizz` (one of the 10 wallets found
missing from bullpen pre-cutover) was captured and copied correctly in the very next cycle,
`trading_fee_usd`/`network_fee_usd`/`total_cost_usd` all populated as designed — full pipeline
confirmed working end to end in production, not just in tests.

**CRITICAL FINDING from a corrected validation run, 2026-07-22 — fixed the same day, not left
open.** The first validation run (2026-07-21) compared mismatched fetch depths (bullpen's shared
`limit=150` across all wallets vs. the direct API's `limit=50` PER wallet), making the volume
difference look like a depth artifact, not a real gap. Re-ran properly using a shared, explicit
time window instead (the direct API's `start` param, confirmed live to filter correctly
server-side; bullpen has no time-window flag at all — checked `--help` — so its results are
filtered client-side to the same window after a generous fetch). Same window, both sources: **0
trades found by bullpen that the direct API missed; 485 trades found by the direct API that
bullpen missed entirely, over just 24 hours.**

Traced to the root cause, not left as a mystery: `bullpen tracker feed` only returns trades for
wallets registered with **bullpen's own, separate tracking list** — a fact `bot.py`'s own code
comment already warned about ("kept in sync out-of-band"). Checked `bullpen tracker list`
directly against `config.py`'s 20 tracked wallets: **exactly 10 of 20 were never registered with
bullpen's tracker at all** (`geo-pako`, `geo-denizz`, `geo-anon-3`, `expand-1`, `expand-2`,
`expand-3`, `geo-anon-4`, `fed-warren-buffett`, `fed-qmg-core`, `fed-559b`) — confirmed by their
real, substantial on-chain trading activity (17-100 trades each in the 24h window, verified via
the direct API) showing up as flat zero in bullpen's feed. **This means bot.py has been
structurally blind to trades from half its configured wallet list this entire time** — not a
subtle edge case, a 50% blind spot, present since whenever these 10 wallets were added to
`config.py` without a corresponding `bullpen tracker add`.

**Fixed immediately, same session**: all 10 missing wallets registered via `bullpen tracker add`
(a safe, easily-reversible action — `tracker remove` undoes it, no capital or trading logic
touched). Re-verified: `bullpen tracker list` now shows all 20 configured wallets registered,
zero missing.

**This is now the single strongest piece of evidence for the Rule 14 cutover**: the direct API
approach has no equivalent "separate registration list to keep in sync" failure mode at all — it
reads directly from `config.py`'s address list, so this exact class of silent, structural gap
cannot recur once cut over, only patched around (as done here) while still on `bullpen`.

---

## 15. Confidence-weighted position sizing

**What it does:** each copied BUY is sized by the source wallet's `wallet_profile.composite_score`
(from `packages/copy-trading/src/scoreWallets.ts`) instead of a single flat dollar amount for every
trade regardless of trader quality.

**How it works mechanically:** `bot.compute_trade_size_usd(composite_score)` — a pure, unit-tested
function. `composite_score is None` (a wallet was never scored, e.g. one of the manually-added
`fed-*`/`geo-*` traders in `config.TRACKED_TRADERS` that predates a `scan:wallets` run) →
`config.BASE_TRADE_USD` ($5, the old flat behavior, unchanged for wallets with no evidence either
way). Otherwise linearly scaled between `config.MIN_TRADE_USD` ($3, at score 0) and
`config.MAX_TRADE_USD` ($10, at score 1), clamped defensively in case a score ever lands outside
[0, 1]. Scores are fetched once at `bot.py` startup (`db.get_wallet_composite_scores()`) — same
restart-to-pick-up-changes design as `get_tracked_traders()`, and deliberately independent of
`config.TRACKED_TRADERS_SOURCE`: a statically-listed wallet can still have a real score in
`wallet_profile` from a past scan, so this isn't gated on which list-source is active.

**Explicitly NOT literal Kelly sizing, named honestly rather than overclaimed rigor**: Kelly's
`f* = (p-c)/(1-c)` needs a calibrated per-trade probability estimate. This bot copies trades, it
doesn't independently forecast market outcomes, so it has no such estimate to plug in.
`composite_score` is a heuristic confidence signal about the *wallet* (ROI, consistency, win rate,
copyability — see `scoreWallets.ts`), not a probability about any specific trade. The sizing
formula captures the same underlying idea Kelly is after — size up on stronger evidence, size down
on weaker evidence — through the one signal this bot actually has, without dressing a linear scale
up as literal Kelly math.

**Live-verified against the current tracked list** (2026-07-22): sizes ranged $3.00 (score 0.0,
e.g. `strict-3`/`strict-6`/`fed-559b`) to $6.27 (`strict-8`, score 0.467), with unscored wallets
(`geo-pako`, `expand-1`, `fed-warren-buffett`, `fed-qmg-core`) correctly landing at the $5 base —
a sensible, non-degenerate spread, not clustered at one end.

**System costs & trade-offs:** every existing portfolio-level gate (Rule 6's exposure ceilings,
the kill switch, Rule 3's duplicate-exposure guard) already takes `trade_usd` as a parameter rather
than assuming a fixed constant internally, so a variable size is enforced correctly by construction
— nothing needed to change in `risk_manager.py` for this to be safe. The only real new risk is
sizing UP a wallet whose score turns out to be a false positive (a wallet that scored well on
stale/thin data) — bounded by `MAX_TRADE_USD` ($10, 2x the old flat amount) rather than left
unbounded.

**Why it exists:** researched directly (arXiv/industry sources on Kelly criterion for prediction
markets and copy-trading practitioner guides) after noticing `FIXED_TRADE_USD` meant every copy
got identical size regardless of how much the bot's own scoring pipeline already knew about that
wallet's quality — real, computed evidence that was being thrown away at the one point (position
sizing) where it would have mattered most.

---

## 16. Order-book staleness check

**What it does:** refuses to price a paper-shortfall simulation (Rule 10) or a Trailing Take-Profit
check (Rule 7) off an order book whose own server-side timestamp is too old, instead of silently
trusting any book that successfully returns within the HTTP timeout.

**How it works mechanically:** `polymarket_simulator.fetch_order_book()` reads the `/book`
response's own `timestamp` field (ms since epoch — always present in live responses checked
during development) and compares it against wall-clock time. A book older than
`MAX_BOOK_AGE_SECONDS` (15s — generous versus normal round-trip jitter, tight versus a genuinely
degraded snapshot) raises rather than being used. Missing `timestamp` is treated leniently (not an
error) — since real responses always carry it, this isn't inventing a new failure mode for a field
that shouldn't be absent. Because both `simulate_fill()` (Rule 10) and `get_market_prices()`
(Rule 7) call this one function underneath, the fix lands in both places at once — no per-caller
duplication. The raised exception surfaces through each caller's EXISTING fail-soft handling
(`preview_unavailable` for shortfall measurement, a price-check-failed string for TTP) with zero
new branches needed in `bot.py` — verified directly, not just asserted.

**System costs & trade-offs:** this is a genuinely different failure mode from a slow/dead
connection (already handled by the HTTP timeout and connection-retry logic) — a degraded backend
can serve an old cached book quickly, well inside the timeout, which is exactly what this catches.
`MAX_BOOK_AGE_SECONDS=15` is a judgment call, not a verified-safe number; 30s (one full poll
interval) was considered and rejected as too loose for a value that feeds TTP exit pricing
directly. 4 new tests (`TestOrderBookStaleness`) cover fresh/stale/boundary/missing-timestamp
cases; all existing HTTP-mocked book fixtures across the test suite were updated to carry a
realistic timestamp so they don't accidentally bypass the very check being tested. 97 tests pass.

**Why it exists:** found during a resilience review (prompted by a direct question about stale
data risk) — `fetch_order_book()` was already fetching this timestamp field for `last_trade_price`
purposes and discarding the freshness signal sitting right next to it. A real gap, not a
hypothetical one: nothing before this would have distinguished a genuinely fresh book from one a
degraded backend served quickly but late.

---

## 17. Disk-exhaustion hardening: rotating logs + event-log retention

**What it does:** bounds the two distinct sources of unbounded disk growth found during the same
resilience review — `bot.out.log`/`dashboard.out.log` (plain text files) and `bot_event_log`
(a SQLite table) — instead of leaving both to grow forever.

**How it works mechanically — these are two genuinely different mechanisms, not one, because the
two things being bounded are not the same kind of object**:
- **Text files**: `bot.py` and `dashboard.py` each now configure a
  `logging.getLogger(...)` with a `logging.handlers.RotatingFileHandler`
  (`config.LOG_MAX_BYTES` = 50MB, `config.LOG_BACKUP_COUNT` = 5 — so each file plus its rotated
  backups caps at 300MB, not unbounded) alongside a `StreamHandler` so foreground/interactive runs
  still show output live. Every `print()` in both files was replaced with the appropriate
  `logger.info()`/`.warning()`/`.error()`/`.critical()` call. **`db.py`'s `append_log()` had its
  own `print()` too** — the highest-VOLUME one by far (fires on every single event, not just the
  handful of rare operational status lines bot.py/dashboard.py had) — replaced with
  `logging.getLogger("copybot.db").info(...)`, which propagates to bot.py's already-configured
  "copybot" handlers rather than attaching a second, independent `RotatingFileHandler` on the same
  file (two handlers independently tracking one file's size via separate file handles can rotate
  at inconsistent moments — the same class of bug fixed below for `dashboard.py`'s `start_bot()`).
- **`bot_event_log` (SQLite table)**: `logging.handlers.RotatingFileHandler` cannot attach to a
  database table — this table is written via direct SQL `INSERT` in `db.append_log()`, never
  through Python's `logging` module. The correct analog is `db.prune_event_log(retention_days)`:
  an age-based `DELETE FROM bot_event_log WHERE timestamp < cutoff`, defaulting to
  `config.EVENT_LOG_RETENTION_DAYS` (180 days — generous enough for months of shortfall/PnL
  research, still bounded long-run). Wired into `bot.py`'s main loop as a new interval-gated sweep
  (`config.PRUNE_INTERVAL_SECONDS` = once/day), matching the existing TTP-sweep/closeout-sweep
  pattern exactly: its own try/except, its own `persist()`, a logged `event_log_pruned` event with
  the row count when it actually deletes something.
- **A related latent bug found and fixed along the way**: `dashboard.py`'s `start_bot()` used to
  open its own file handle to `BOT_LOG_PATH` and hand it to `bot.py`'s subprocess as `stdout` — once
  `bot.py` owns that file itself via its own `RotatingFileHandler`, keeping this Popen-level
  redirect would have gone **rotation-blind**: that file handle would keep writing to the file's
  OLD inode after `bot.py`'s handler renames it away during rotation, so nothing launched via the
  dashboard's start button would appear in the current `bot.out.log` after the first rotation.
  Fixed by redirecting the subprocess's stdout/stderr to `DEVNULL` instead — `bot.py`'s own logger
  is the sole writer of that file now, regardless of how the process was launched.

**System costs & trade-offs:** `prune_event_log()` deletes real rows — given real destructive
potential, it got its own isolated test suite (`test_db_prune.py`, a temporary SQLite file, never
`data/app.db`) rather than the "verify live" precedent used for read-only `db.py` functions
elsewhere. 180 days is a judgment call, not a verified-safe number — worth revisiting if research
ever needs a longer lookback window. Deployed carefully: restarted through `dashboard.py`'s own
`/api/toggle` this time (which starts `bot.py` via its tracked pidfile path), specifically to avoid
repeating the duplicate-process mistake from earlier the same day.

**Why it exists:** flagged in the same resilience review as Rule 16 — `bot_event_log` had already
reached 127MB/15k+ rows with zero pruning, and the exception-logging path itself (`append_log()`
inside an `except` block) was identified as the one route that could actually crash the whole
process if the underlying disk ever filled — this closes that gap at its root rather than treating
it as a hypothetical.

---

## 18. Category-specific wallet scoring

**What it does:** sizes a copy by the source wallet's OWN edge in the SPECIFIC market category
being traded (Politics/Sports/Crypto/Pop Culture, or "other"), instead of one global
`composite_score` blended across every category a wallet has ever touched — a wallet can be
genuinely sharp in Crypto and bad in Sports, and the bot previously had no way to tell.

**How it works mechanically:** two things were verified live before building this, because they
changed the approach entirely. First, `bullpen wallet-stats` (what the global score is built from)
has NO category/market filter at all — confirmed by reading every one of `scoreWallets.ts`'s
bullpen calls (`summary`/`flow`/`behavior`/`pnl-series` sections, all whole-wallet aggregates).
Second, the decision (made explicitly, not defaulted into) was to compute the category score from
the wallet's OWN raw trade history — a leading indicator matching the global score's philosophy —
rather than from our own limited copy experience, which requires reconstructing their positions
and realized PnL independently:
- **`polymarketDataApi.ts`** (new, TS twin of `polymarket_data_api.py`): fetches a wallet's raw
  `/activity?type=TRADE` history directly from Polymarket's Data API, paginated. **Real bug caught
  and fixed while building this**: an initial `limit=500 × MAX_PAGES=20` would have exceeded
  Polymarket's own 5000-total-offset ceiling — hit live as an actual HTTP 400 ("max historical
  activity offset of 5000 exceeded") during testing, not just read in docs. Fixed: `MAX_PAGES=10`
  (10×500=5000, the exact ceiling, never requesting offset 5000 itself). A wallet trading fast
  enough to exhaust 5000 records well within the rolling window (confirmed live: one wallet's 5000
  most recent records spanned barely 3 days) gets a **logged, honestly-partial** history, not a
  silently-truncated one — and, correctly, produces zero realized closes for that narrow window
  rather than fabricating a signal from positions that are mostly still open and unresolved.
- **`polymarketCategories.ts`** (new): the TS twin of `polymarket_simulator.py`'s
  `resolve_market_category()` — market → parent event (Gamma) → tags → bucket against
  `CATEGORY_TAG_SLUGS` (politics/sports/crypto/pop-culture — must be kept in sync with
  `config.py`'s copy by hand; there's no shared config module across the two languages).
- **`scoreWalletCategories.ts`** (new, run on the same monthly cadence as `scoreWallets.ts`, no new
  schedule): reconstructs each wallet's positions from raw trades using the exact same BUY/SELL
  cost-basis arithmetic `bot.py`'s `process_trade()` already uses, then realizes PnL two ways —
  sold before the window ends (proceeds − cost basis, exact), or still open but the market has
  since resolved (Gamma `closed`+`outcomePrices`, mark to the 1.0/0.0 payout). A still-open,
  **unresolved** position is excluded from PnL entirely — no mark-to-market guessing. A SELL
  against a position opened before the lookback window (cost basis unknowable) is excluded the
  same way — the identical, already-accepted "no visibility into pre-tracking-start positions"
  limitation `bot.py`'s own module docstring already documents, just in a new context. Per-category
  aggregation reuses `scoreWallets.ts`'s own `computeRoiScore`/`computeWinRateScore` (same
  normalization, same thresholds as the global score) blended 50/50 — deliberately simpler than the
  global score's 4-factor formula, since a single category's sample is usually much smaller than a
  wallet's lifetime trade count and a Sharpe-style consistency measure would be shakier here than
  useful. **Minimum sample gate**: a category needs ≥`DEFAULT_RULES.hardMinTrades` (5, the SAME
  constant the global score's own hard minimum already uses, not a new invented number) resolved
  closes before it gets a score at all — below that, the category is simply absent from the
  output, which the Python side already treats as "fall back to the global score," no extra
  handling needed there.
- **Python side** (`db.py`/`bot.py`): `get_wallet_composite_scores()` extended to return
  `{composite, categories: {category: score}}` per wallet instead of a bare float (its one call
  site updated in the same change, not left half-migrated); `compute_trade_size_usd()` gained a
  `category` parameter with a two-tier fallback (category score if present → global composite
  score → `BASE_TRADE_USD`); `process_trade()` resolves the current trade's category by reusing
  the event slug it already resolves for the risk-manager exposure cap, rather than a second
  lookup — persisted via new, separate `load_market_categories()`/`save_market_category()`
  functions on the existing `bot_market_event` table (kept separate from
  `load_market_events()`/`save_market_event()`'s existing `{market_slug: event_slug}` shape, which
  `risk_manager` already depends on and shouldn't need to change just because a column was added).

**System costs & trade-offs:** two new Drizzle-migrated nullable columns
(`bot_market_event.category`, `wallet_profile.category_scores_json`) — additive, no risk to
existing rows. A category score is a lagging-within-a-window signal in one specific sense worth
being honest about: an extremely high-frequency wallet may never accumulate enough *resolved*
closes within the reachable trade-history depth to clear the minimum-sample gate at all, and will
simply keep falling back to its global score — not a bug, just the visible cost of "only count
what's actually realized, never guess."

**Why it exists:** the explicit motivating example — "a wallet can be a genius in Crypto and
terrible in Sports" — was a real, previously-unaddressed gap: `compute_trade_size_usd()` had no way
to size differently for the same wallet depending on what kind of market it was copying.

**Holding-reward contamination — a real, distinct risk, and why the Category Score is immune to it:**
Polymarket pays a genuine 3.25% APR "holding reward" for merely HOLDING a position on eligible
markets, independent of trading skill — a payout that is NOT a trade. The OLD global
`composite_score` (built by `scoreWallets.ts` from `bullpen wallet-stats`' portfolio-VALUE
snapshots, e.g. `pnl-series`) genuinely cannot distinguish a reward payout from a trading gain,
since it only sees aggregate portfolio value moving, not what caused it to move — a real, open risk
for that score. **The Category Score is architecturally immune to this contamination, because our
self-tracking architecture reconstructs it purely from raw trade events**: `scoreWalletCategories.ts`
builds every category close exclusively from Polymarket Data API `/activity?type=TRADE` records
(see above) — we are entirely self-tracking through the Polymarket API (Gamma/CLOB) to reconstruct
these historical trades ourselves, not relying on any third-party aggregator's portfolio-value
rollup. A holding-reward payout is not a `TRADE` activity record, so it structurally cannot enter
this reconstruction at all — not filtered out after the fact, simply never present as an input in
the first place. `bot_market_event.holding_rewards_enabled` (new nullable column, captured
2026-07-23 at zero extra API cost alongside `event_slug` — see Rule 14's `resolve_market_event()`)
exists purely so this immunity claim is independently auditable per-market, not asserted only in
prose: it does not feed `compute_trade_size_usd()` or any scoring formula.

---

## 19. Hard skip on statistically significant category harm

**What it does:** skips a copy entirely — not just floor-sizes it — when there is statistically
significant evidence that a wallet's category performance is genuinely negative, not just
numerically low.

**How it works mechanically:** the sizing floor (Rule 18) already softens a bad category down to
`MIN_TRADE_USD`, but a genuinely harmful category deserves better than "still copy it, just small."
The threshold for a HARD skip is deliberately **not** an arbitrary score cutoff — per an explicit
standing instruction ("all formula or calculation is using papers and has evidence... if dont, then
just u decide") — it's a one-sample **t-test** (Student's t-test, standard since Gosset 1908; the
textbook method for "is this sample mean significantly different from a hypothesized value," here
zero expected profit), one-tailed since we only care about detecting harm (a good category is
already rewarded via Rule 18's sizing, not something to gate on). `scoreWalletCategories.ts`
computes `pnl_t_stat` per category (mean realized PnL, divided by standard error — sample stddev
via Bessel's correction, over √n) alongside the existing score. `bot.should_skip_category()`
compares it against `-config.CATEGORY_SKIP_Z_CRITICAL` (**1.645, the conventional one-tailed 95%
critical value** — citable to any standard statistics reference, not invented). This correctly
tells apart two cases a flat score cutoff would have conflated: the real example that motivated
this (a wallet with a 4.2% win rate over 71 Crypto trades — statistically overwhelming evidence,
✕`t_stat` far past the critical value) versus a small, noisy sample that happens to look bad by
chance (weak `t_stat`, correctly NOT hard-skipped, still just floor-sized).

**A real serialization bug caught and fixed before this shipped**: zero-variance categories (every
close identical) hit a division that would naturally produce `Infinity` — but `JSON.stringify(Infinity)`
silently serializes to `null` (confirmed live, not assumed), which would have flipped "maximally
strong evidence of harm" into "no evidence at all" the moment it round-tripped through
`category_scores_json`. Fixed with a large finite sentinel (`±1e6`) instead — far more extreme than
any realistic critical value, same practical effect, survives JSON round-tripping correctly.

**System costs & trade-offs:** `should_skip_category()` is checked in `process_trade()` BEFORE
`compute_trade_size_usd()` is even called — a skipped copy never computes a size at all, logged as
`skip_poor_category_performance`. Two-tier separation is deliberate: the STATISTICAL bar for a hard
skip is stricter than the bar for score-based sizing, so a wallet doesn't get permanently blacklisted
from a category off a small, noisy sample — only off evidence strong enough to survive a real
significance test.

**Why it exists:** direct follow-through on the Rule 18 discussion — continuing to copy a category
with a confirmed, statistically strong negative edge is avoidable negative EV, not a risk worth
carrying just because the floor is already small.

---

## 20. Category-specialist wallet discovery (reporting tool, not wired into tracking)

**What it does:** `discoverCategorySpecialists.ts` runs the same reconstruction Rule 18 uses
against a broader candidate pool (beyond the 20 currently tracked wallets) to surface wallets
worth adding specifically because of a strong category-specific edge, not necessarily a strong
overall record.

**How it works mechanically:** two bullpen-based category-discovery paths were checked live and
confirmed unusable, not assumed: `bullpen data leaderboard` has no category filter at all;
`bullpen data smart-money --type top_traders --category <X>` has a real, documented filter but
returned genuinely empty results (or timed out) for crypto/politics/sports when tested live —
matching `scanLeaderboard.ts`'s own prior "never confirmed working" caveat. So this reuses
`scoreWalletCategories.ts`'s own exported reconstruction functions directly (`fetchWalletTrades`,
`resolveMarketCategory`, `reconstructRealizedCloses`, `aggregateCategoryScores`) against candidates
already sitting in `wallet_profile` (470+ rows from `scoreWallets.ts`'s existing monthly run,
seeded by `scanLeaderboard.ts`), rather than depending on a bullpen discovery endpoint at all.
Runs with bounded concurrency (`mapWithConcurrency`, limit 5 — matching `scanLeaderboard.ts`'s
existing `PASS1_CONCURRENCY` precedent) since the candidate pool is much larger than 20.
**Candidate pool is a real, stated trade-off**: filtering by a wallet's existing global
`composite_score`/`status` is exactly the signal this feature exists to second-guess (a globally
mediocre wallet could still be a category specialist) — checked live that `status != 'ignore'`
alone leaves only 26 candidates (too narrow), so the default is `composite_score >= 0.2` (107
candidates) instead, configurable via `--min-composite-score` or bypassed with `--all`.
**Ranks by `pnl_t_stat` (strength of statistical evidence), not raw `score`** — the positive-
evidence mirror of Rule 19's hard-skip test, same 1.645 one-tailed critical value, opposite tail.
Live-verified this ranks meaningfully differently from raw score (a wallet with a perfect 1.000
score but a smaller sample ranked BELOW a 0.781-score wallet with 6x the statistical evidence).

**Deliberately a REPORT ONLY**: prints a ranked top-5-per-category list, does not write to
`wallet_profile.status` or touch `config.TRACKED_TRADERS` — matches the existing pattern of human
curation for the tracked list (the current 20 were hand-selected; `TRACKED_TRADERS_SOURCE` also
stays `"static"` pending review per the existing roadmap).

**A real, honest finding from the first live run, worth stating plainly**: several statistically
significant "specialists" showed 100% win rates with tiny average profit ($0.03-$1.91 over up to
2022 trades) — a pattern consistent with high-frequency, thin-margin trading, not necessarily
copyable edge (our own execution would need to match a precision/speed this bot likely can't).
Statistical significance confirms the PATTERN is real, not that it's SAFELY copyable — this is a
report to review individually, not a list to blindly add.

**Why it exists:** direct follow-through on "I want to track more people... find who's the top
performer in different fields" — the category-scoring infrastructure built for sizing decisions
turned out to be directly reusable for discovery too, once pointed at a broader candidate pool.

---

## 21. Transaction Cost Analysis (TCA) filter: statistical significance is not enough

### Summary: the two problems this solves

To stop discovery from surfacing wallets that are statistically real but
economically not worth copying, this filter addresses two distinct
problems Rule 20's significance test alone can't catch.

#### 1. The "real but too-small edge" problem

**Challenge:** a candidate can clear the significance bar (its edge is
genuinely not noise) and still be worthless to copy, if that edge is
smaller than what OUR OWN market order would cost to execute — spread plus
fees eat it entirely.

- **Mechanism: implementation-shortfall-grounded TCA filter.**
  `passesTcaFilter()` requires `roi > estimatedRelativeSpread(price)/2 +
  fee_rate*(1-price) + safety_buffer`, mapped directly to Perold's (1988)
  Implementation Shortfall model — fees computed exactly from the market's
  real fee schedule, spread estimated from Polymarket-specific
  microstructure research (not generic equity numbers), sized to OUR
  $3-$10 order, not the wallet's own $1k-$50k+ trades.

#### 2. The "wrong lens" problem

**Challenge:** judging candidates by raw dollar profit per trade unfairly
penalizes a wallet that simply sizes its own bets small — a $0.03
average-profit wallet LOOKS like noise, even if its actual edge is strong.

- **Mechanism: percentage ROI, not dollar amount.** Live-verified
  correction: the same wallet flagged as "suspiciously thin, $0.03/trade"
  turned out to carry 28.3% ROI, because its capital deployed per trade was
  proportionally tiny too. TCA compares percentage edge against percentage
  cost — the metric that actually matches how this bot sizes its own
  copies as a fraction of a small fixed budget, not the wallet's absolute
  stake.

---

**What it does:** a candidate that clears Rule 20's significance bar (`pnl_t_stat >= 1.645`) can
still be worthless to copy if its historical edge is smaller than what OUR OWN market order would
cost to execute. `discoverCategorySpecialists.ts` now applies a second, independent filter after
significance ranking: `passesTcaFilter()` requires

```
roi > estimatedRelativeSpread(avg_entry_price) / 2 + avg_fee_rate * (1 - avg_entry_price) + safety_buffer_pct
```

where `roi` is the category's total realized PnL over total capital deployed (already computed by
`aggregateCategoryScores`, now surfaced directly).

**Why this exact shape — grounded, not arbitrary:** this maps to Implementation Shortfall (Perold,
1988): `IS = explicit costs (fees) + implicit costs (spread/impact) + delay/opportunity cost`. We
model the first two terms only; delay/opportunity cost is knowingly NOT modeled (our copy trades
execute within seconds of detecting the source trade, at $3-$10 size, so it's not the dominant term
the way it is in institutional execution research — a stated simplification, not an oversight).
- **Fees are exact, not estimated**: `avg_fee_rate` is the cost-basis-weighted average of the
  market's real `feeSchedule.rate` at each close (same verified `fee = shares * feeRate * price *
  (1-price)` formula `polymarket_simulator.py` already confirmed live), giving a fee cost fraction
  of `feeRate * (1 - price)`.
- **Slippage must be estimated** — Polymarket's public API only exposes the CURRENT order book, not
  the book as it stood at each historical trade. `estimatedRelativeSpread(price)` is a
  piecewise-linear interpolation grounded in Polymarket-specific microstructure research (not
  generic equity-market numbers, which don't transfer to prediction markets' longshot bias):
  ~4% (400bps) relative spread for prices in [0.4, 0.6], widening to ~15.5% (midpoint of the
  observed 13-18% range) for longshot prices below 0.10 or above 0.90. A market BUY crosses from
  mid to the far touch — HALF the quoted spread, not the full spread.
- **Sized to OUR order, not theirs**: candidates often trade $1k-$50k+ historically; our copies are
  $3-$10 (`config.MIN_TRADE_USD`/`MAX_TRADE_USD`). A smaller order walks less of the book, so using
  the full historical spread estimate (not scaled down further for our smaller size) is a
  deliberately conservative, not optimistic, assumption.
- **Entry price, not exit/resolution price**: `avg_entry_price` (cost-basis-weighted, from
  `RealizedClose.sharesClosed`) is the price a market order actually crosses the spread AT. A
  position held to resolution pays out at a fixed 1.0/0.0 with no spread involved — using the exit
  price would understate cost for anything that wasn't resolution-marked and misprice risk entirely
  for anything that was.
- **Safety buffer is a configurable PERCENTAGE of price, not a flat USD amount** (`--tca-safety-buffer`,
  default 2%) — a flat $0.50 buffer would be 16x a $0.03 longshot's price and negligible against a
  $0.97 near-certainty; a percentage scales correctly across the full $0.03-$0.97 range.

**Refines, not contradicts, Rule 20's "honest finding"**: the first live run flagged wallets with
100% win rates but tiny AVERAGE DOLLAR profit ($0.03-$1.91/trade) as a possible red flag (thin-margin
HFT-style trading). Live-verified after building this filter: those same wallets often carry strong
ROI% (e.g., 28.3% on the $0.03-avg-profit wallet) because their capital deployed per trade is
proportionally tiny too — dollar amount was the wrong metric for a bot sizing its own copies as a
% of a $3-$10 budget, not matching the source wallet's absolute stake. TCA is the correct filter:
percentage edge vs. percentage cost, not dollar edge vs. an arbitrary dollar floor.

**Runs after significance ranking, not before**: significance asks "is this edge real, not noise";
TCA asks "is this edge big enough to survive our own execution cost" — independent bars run in
that order so TCA isn't wasted filtering candidates significance would already have dropped.

**Still a REPORT only** — same as Rule 20, nothing here writes to `wallet_profile.status` or
`config.TRACKED_TRADERS`.

---

## 22. Decision-outcome review: score snapshots, `outcome_review`, Brier calibration, structural break

**What it does:** `decision_journal`/`outcome_review`/`rule_set`/`rule_change` have existed in the
schema since the original migration, but nothing ever populated `decision_journal.
score_breakdown_json`/`rule_set_version`, linked a decision to the `paper_trade` it produced, or
wrote an `outcome_review` row — verified live before building anything here. This rule covers the
prerequisites now shipped in `bot.py`/`db.py`, plus the design (approved via `EnterPlanMode` before
any code was touched, per explicit request) for `reviewOutcomes.ts`'s two analyses, not yet built.

**Prerequisites shipped now (bot.py/db.py):**
- `db.append_log()` now returns the new `decision_journal` row's id (`None` when the event wasn't a
  copy/skip decision) — a deliberate, documented departure from this module's stated "same shapes
  as the old JSON files" invariant, since `decision_journal`/`outcome_review` have no JSON-file
  equivalent to preserve parity with.
- `process_trade()` snapshots the score that actually drove `compute_trade_size_usd()`'s sizing
  decision — `{category, category_score_detail, composite_score, trade_size_usd, sizing_tier}`,
  where `sizing_tier` mirrors that function's own two-tier fallback logic (duplicated at the call
  site rather than changing its return type, which 14+ existing tests depend on as a bare float) —
  into `decision_journal.score_breakdown_json`.
- `db.get_active_rule_set_version()`: a read-only `SELECT version FROM rule_set WHERE is_active=1`
  against the TS-owned `rule_set` table (populated by `scoreWallets.ts`, never written by
  Python — same cross-layer write boundary as `wallet_profile.status`). Read once at `bot.py`
  startup, cached in `risk_state`, stamped onto every `decision_journal` row. Exists so a later
  structural-break finding (below) can be told apart from "we changed the scoring formula out from
  under it," not just "the wallet's own edge shifted."
- `decision_journal` ↔ `paper_trade` linkage: many-to-one (a position can receive multiple buys,
  `config.MAX_BUYS_PER_TRADER_OUTCOME`) — `paper_trade.decision_journal_id` records only the
  OPENING decision; `decision_journal.linked_paper_trade_id` is set for WHICHEVER decision most
  recently touched the position (open or average-up), every time. Mechanically: the position dict
  in `bot.py`'s in-memory state gains `last_decision_journal_id` (set right after `append_log()`
  returns, in `process_trade()`); `db.save_state()` — which already runs immediately after every
  single `process_trade()` call, so this is always fresh — writes both FKs from it.
- **Honest limitation, stated plainly**: only decisions made AFTER this shipped have a score
  snapshot or FK link. Historical `paper_trade` closes have no `decision_journal` row to join to —
  the (not-yet-built) `outcome_review` generator will correctly show
  `contributing_score_factors_json = NULL` for those rather than reconstructing a score from
  `wallet_profile`'s CURRENT (since-overwritten) values. Both analyses below need real post-shipment
  history to accumulate before producing anything meaningful — expected, not a bug.

**`reviewOutcomes.ts` — all three stages LIVE as of 2026-07-24:**
- **Stage 1, `outcome_review` generator** (LIVE): for each closed `paper_trade` not yet represented
  in `outcome_review`, pulls the OPENING decision's `score_breakdown_json`/`rule_set_version` via
  `paper_trade.decision_journal_id` (the unambiguous 1:1 direction — a refinement over joining via
  `linked_paper_trade_id`, which is many-to-one once a position has averaged up more than once), and
  writes `was_correct_call = (realized_pnl_usd > 0)`, `pnl_usd = realized_pnl_usd` (already computed
  by `bot.py`'s close path — no new network calls, this is a pure local DB join), and copies
  `score_breakdown_json` + `rule_set_version` through into `contributing_score_factors_json`
  (copied, not just referenced, so this survives if `decision_journal` ever gets a retention policy
  the way `bot_event_log` does). Idempotent/incremental — safe to re-run without reprocessing.
  **Live-verified**: ran against the real DB's 16 pre-existing closed trades — all 16 got a row,
  all correctly showing `contributing_score_factors_json = NULL` (they closed before this shipped,
  exactly the "honest limitation" documented above, not a bug); a second run wrote 0 new rows,
  confirming idempotency.
- **Stage 2, Brier score calibration** (LIVE) (Brier, 1950 — the standard forecast-verification
  metric): tests whether the snapshotted win-rate that drove sizing is actually calibrated OUT OF
  SAMPLE against what happens to trades copied AFTER that scoring window closed —
  `mean((forecast_win_rate - actual_outcome)^2)`, grouped by wallet and by (wallet, category),
  gated at `DEFAULT_RULES.hardMinTrades` (5 — the same constant Rule 18/20/21 already use, not a
  new number), reported as a quintile-bucketed calibration table (not just one pooled number) so
  systematic over/under-confidence is visible at a glance. Report only. **Scope refinement, found
  while building**: `db.get_wallet_composite_scores()` only ever surfaced `{score, pnl_t_stat}` per
  category — no `win_rate` at all, and no composite-level win_rate is tracked anywhere. Extended it
  (2026-07-24) to also surface `win_rate`/`trade_count` per category, so the score snapshot actually
  carries a forecast to calibrate. Only decisions where `sizing_tier === "category"` have a win-rate
  forecast — `"composite"`/`"base"`-tier decisions are excluded from Brier calibration entirely
  (never guessed at), a deliberately scoped v1, not silently expanded.
- **Stage 3, structural-break test (regime-shift detection)** (LIVE): a genuinely different test
  from Rule 19's, not a reuse — Rule 19 is a ONE-sample, ONE-tailed t-test against zero ("is mean
  PnL significantly negative"); this is **Welch's TWO-sample, TWO-tailed t-test** (Welch, 1947 —
  the correct choice over Student's equal-variance test specifically because a regime shift
  plausibly changes variance too, not just the mean) comparing an early vs. recent window of the
  same wallet's (or wallet+category's) `outcome_review` PnL history — TWO-tailed, since a shift can
  be an improvement OR a decline, unlike Rule 19's harm-only framing. **Fixed-window split, not a
  full Chow-test/CUSUM breakpoint scan** — a deliberate simplification: with most wallets well
  under 100 resolved outcomes for a long while, scanning every candidate breakpoint and reporting
  the best one is a real multiple-comparisons risk (the classic Quandt likelihood-ratio over-search
  problem) without the sample size to have genuine power. **Refinement over the originally-approved
  plan text, found while building**: the plan named a flat "critical value 1.96" — the NORMAL-
  distribution approximation, valid only for large degrees of freedom. With the fixed window size
  (10, `hardMinTrades × 2`) on both sides, Welch-Satterthwaite degrees of freedom always fall in
  `[9, 18]` — small enough that the TRUE Student's-t critical value (2.10–2.26, standard tabulated
  values) is meaningfully higher than 1.96, and using 1.96 anyway would have systematically
  OVER-flagged (more false positives, not fewer). Implemented with the correct small-df tabulated
  lookup instead, documented inline rather than silently substituted. Per-wallet grouping uses ALL
  of a wallet's `outcome_review` rows regardless of score-snapshot presence (only needs
  `walletAddress`/`resolvedAt`/`pnlUsd`, all top-level columns) — only the finer per-(wallet,
  category) grouping needs a score snapshot (to know the category). Report only — explicitly NOT
  wired to `wallet_profile.circuitBreakerMuted` (Rule 5's circuit breaker is a fast, real-time,
  consecutive-loss signal; this is a slow, periodic, statistical one — conflating them would make a
  tested real-time safety mechanism depend on an unproven batch job). A flagged wallet is a
  candidate a human reviews, same pattern as Rule 20.
- **Live-verified**: ran `review:outcomes` end to end against the real DB — 0 new `outcome_review`
  rows (unchanged, confirming idempotency again), and both stages correctly printed "insufficient
  data" (not a crash, not a fabricated number) — expected, since no decision made after the Part A
  prerequisites has closed yet. The honest-limitation messaging was checked in practice, not just
  asserted in docs.

**Why it exists:** direct follow-through on "I want to review exactly how you plan to implement the
Brier score calibration and the structural break t-test... before touching bot.py" — the
prerequisites above were the real blocker (there was no score/outcome data to analyze at all), so
they shipped first, gated on explicit plan approval before any code was written.

---

## 23. Wash-trading suspicion screen (discovery report annotation, not a filter)

**What it does:** annotates `discoverCategorySpecialists.ts`'s report with a warning when a
candidate's stats match the "economically neutral position" signature research associates with
wash-trading/incentive-farming, rather than genuine skill. A real, externally-documented mechanism
for something this project already caught by hand in Rule 20 (100%-win-rate, $0.03-avg-profit
wallets) — under 1% of Polymarket wallets capture ~50% of profit in politics markets, and roughly
15% of volume in some markets matches self-trading patterns consistent with farming ahead of a
POLY airdrop (CoinDesk, Apr 2026).

**Why our own ranking was structurally vulnerable to this, not just theoretically**:
`rankSpecialistsByCategory()` sorts by `pnl_t_stat` descending, and `aggregateCategoryScores()`'s
t-stat calculation gives a wallet whose closes are all nearly-identical small positive amounts an
EXTREME t-stat (the zero-variance sentinel path — see that function's own comment, documented for
the harm/negative case but symmetric on the positive side). Low-variance, tiny, repeated gains —
exactly what wash trades look like — get structurally REWARDED by pure t-stat ranking, not flagged.

**How it works mechanically:** `flagsWashTradingSuspicion(candidate, thresholds)` — pure, exported,
unit-tested — flags a candidate when ALL THREE hold: `winRate >= 0.90`, `tradeCount >= 20`,
`roi < 0.10`. Each threshold is deliberately grounded, not arbitrary:
- **0.90 win rate**, not the 70% "often a red flag" bar a trader-persistence study (Polyburg, 2026)
  cites — more conservative because this is a WARNING annotation, not an exclusion.
- **20 trades**, not `MIN_CATEGORY_SAMPLE` (5, an inclusionary floor) or Polyburg's cited "200 to
  be bankable" (would almost never fire against today's real candidate pool sizes) — a pragmatic
  middle for an exclusionary judgment, which needs more evidence than an inclusionary one.
- **10% ROI ceiling** — `roi` is cost-basis-weighted average return per close (added for TCA,
  Rule 21), close to "average ROI per trade." The TCA-viable floor for a center-priced market is
  ~6.5% (live-verified during Rule 21's build) — 10% means a candidate can clear TCA yet still get
  flagged if its edge is suspiciously thin relative to what a genuine, confident directional bet in
  a mispriced binary market should net when right.

**Report only, same as Rule 20/21**: a flagged wallet stays in the report (`⚠ WASH-TRADING
SUSPECT` annotation) — never silently dropped. A near-100%-win-rate small-edge wallet could still
be genuinely great; this is "look closer," not "auto-exclude," matching this project's consistent
human-in-the-loop pattern for every discovery/tracking decision.

**Scope, stated explicitly**: covers the wash-trading signal only (win rate + trade count + roi,
all already-computed data — zero new API calls, zero new schema). A SEPARATE finding from the same
research pass — wallet age / single-market-concentration as an insider-trading signature (Polymarket
referred ~100 wallets to authorities over $200M in suspected insider trades, concentrated in
geopolitical markets, per crypto.news/Bloomberg, 2026) — needs genuinely new data (first-seen
timestamp, position concentration) not fetched today, and is deliberately deferred as separate
follow-up work, not folded in here.

**Live-verified**: ran the flag function against the 20 currently-tracked wallets' already-computed
`category_scores_json` (zero extra API calls — pure read + pure-function call against existing
data) — 0 of 19 (wallet, category) entries flagged. A reassuring, real result, not just a formality:
the human-curated tracked list doesn't match the wash-trading signature by this measure.

**Why it exists:** direct follow-through on last night's research pass — turning an externally-
documented mechanism into a concrete check on our own discovery pipeline, rather than leaving Rule
20's "honest finding" as an unexplained hunch.

---

## 24. Category quota discovery — diversify tracking by domain, not just by global rank

**What it does:** replaces "top 20 overall by significance" with a fixed slot count PER CATEGORY —
default 5 slots × the 4 real categories (`politics`, `sports`, `crypto`, `pop-culture` —
`CATEGORY_TAG_SLUGS`, each independently verified live against a real event) = 20 total. An
information edge is category-specific (a crypto specialist has no proven edge in sports); a top-20
that's accidentally all political analysts is one correlated bet dressed up as 20 independent ones,
and breaks the independence assumption position-sizing math (Kelly included) implicitly relies on.

**Correction worth stating plainly**: an earlier version of this request referenced a
"Science/Weather" category — no such category exists in this system. `packages/weather` is a wholly
separate bot/product (temperature-threshold markets, its own EV/Kelly pipeline) — not a Polymarket-
tag-based copy-trading domain. The quota uses the four categories that actually exist.

**How it works mechanically:**
- `filterSignificantByCategory(results, zCritical, targetCategories)` replaces the old
  `rankSpecialistsByCategory` — filters to statistically significant results (Rule 20) and groups by
  category, **UNCAPPED**. Candidates outside `targetCategories` (i.e. `"other"`) are returned
  separately, not silently dropped.
- **A real bug fixed as part of this change, not a separate one**: the OLD function capped at
  top-5-per-category BEFORE the TCA filter (Rule 21) ever ran on the full pool — so a category's
  top-5-by-t-stat could include TCA-rejected candidates while a TCA-viable #6 sat unused, discarded
  before TCA got a look. Now: significance filter (uncapped) → TCA filter runs on the FULL
  significant pool per category → `rankAndCapCategory(survivors, quotaPerCategory)` caps only
  AFTER both bars are cleared. A quota slot always goes to the best QUALIFIED candidate.
- `rankAndCapCategory` sorts by `(washTradingSuspect ascending, pnlTStat descending)` — a clean
  candidate wins a scarce slot over a flagged one (Rule 23), but a flagged candidate can still fill
  a slot if a category has no clean qualifiers — de-prioritized, never excluded, consistent with
  Rule 23's own "warning, not auto-exclude" design.
- **No cross-category backfill**: if `pop-culture` only has 2 qualifying candidates, the report
  shows `pop-culture (2/5 filled)` — never pulled from `politics` to hit 20. Backfilling from an
  over-performing category is exactly the correlated-bet risk this rule exists to prevent.
- **`other` is excluded from the quota, reported separately for transparency** — not a real circle
  of competence (a mix of miscellaneous one-off markets), so it doesn't earn a domain slot, but
  never silently hidden from the report either.
- Configurable via `--quota-per-category` (default 5) and `--categories` (default the four real
  tags), same CLI pattern as every other tunable in this file.

**Report only, same as Rule 20/21/23** — nothing written to `wallet_profile.status` or
`config.TRACKED_TRADERS`.

**Live-verified**: ran against the same 6-wallet batch used to verify Rules 20/21/23
(`--min-composite-score 0.6`) — correctly reported `politics (2/5 filled)`, `sports (1/5 filled)`,
`crypto (0/5 filled) — no qualifying candidates found`, `pop-culture (0/5 filled) — no qualifying
candidates found`, and one wallet correctly routed to the outside-quota `"other"` section — an
honest partial result from a small batch, exactly as designed, not a crash or a fabricated fill.
The real per-category fill rates at full candidate-pool scale are a genuinely open empirical
question this small batch doesn't answer — that's the next live run, not this one.

**Why it exists:** direct follow-through on an explicit architecture request — "we need to ensure
these 20 trackers are strictly diversified by specific fields" — grounded in the same domain-
specialization premise Rule 18 (category-specific scoring) was already built on, now applied to
which wallets get discovered in the first place, not just how an already-tracked wallet is sized.

---

## 25. Half-Kelly position sizing

### Summary: the two problems this solves

To size each copy in proportion to real, evidence-backed edge rather than
an arbitrary confidence blend, this rule addresses two distinct sizing
problems.

#### 1. The "how much to bet" problem

**Challenge:** scaling trade size off a blended 0-1 confidence score
doesn't correspond to any real edge/odds math — it can't say WHY a given
size is correct, and risks systematically overbetting.

- **Mechanism: half-Kelly formula.** `f* = p_shrunk − (1−p_shrunk)/b` where
  `b` is the market-price-implied net odds, then halved
  (`KELLY_FRACTION_MULTIPLIER = 0.5`) — the standard fractional-Kelly
  correction, since full Kelly reliably overbets under the systematic
  overconfidence prediction-market traders show.

#### 2. The "small-sample overconfidence" problem

**Challenge:** a wallet with only 5-10 category-specific trades has a win
rate that's statistically indistinguishable from noise — sizing directly
off that raw number would let a tiny, lucky sample drive a large bet.

- **Mechanism: empirical-Bayes shrinkage toward the market's own price.**
  `p_shrunk = (n·win_rate + k·price) / (n+k)`, `k=25` solved (not guessed)
  so a 5-trade sample is discounted ~83% toward the market's own belief
  while a 200-trade sample is trusted ~89% — at `n=0`, `p_shrunk = price`
  exactly, so "no track record, no assumed edge" falls out of the math
  rather than needing a special case.

---

**What it does:** replaces the 2026-07-22 linear confidence ramp (`compute_trade_size_usd()`
scaling `MIN_TRADE_USD..MAX_TRADE_USD` off a blended 0-1 score) with an actual half-Kelly formula,
using exactly the three inputs requested: the wallet's win rate, the market's own price-implied
odds, and a small-sample shrinkage penalty. `config.py`'s own comment used to explicitly say this
wasn't literal Kelly sizing ("a copy bot doesn't produce a calibrated per-trade probability
estimate") — that's no longer true; the wallet's historical win rate now serves as that estimate,
shrunk toward the market's own belief before being trusted.

**How it works mechanically:**
1. **Shrink the observed win rate toward the market's current price** — `p_shrunk = (n·win_rate +
   k·price) / (n+k)`, a standard empirical-Bayes/Beta-Binomial estimator. Using the MARKET PRICE
   as the shrinkage prior (not a flat 0.5) is deliberate: at `n=0` (zero track record), `p_shrunk =
   price` exactly, which makes the Kelly fraction below come out to precisely 0 — "no track record,
   no assumed edge" falls out of the math, not a special case.
   - `k = config.KELLY_SHRINKAGE_PSEUDO_COUNT = 25`, solved (not guessed): at `n=200` (Polyburg's
     own cited "bankable" sample size, from the same research pass that motivated this rule),
     weight on the observed win rate ≈89%; at `n=5` (this codebase's existing hard minimum sample,
     `DEFAULT_RULES.hardMinTrades`), weight ≈17% — heavily discounted toward the market's price,
     exactly the "small samples should be heavily discounted" requirement.
2. **Kelly fraction**: `f* = p_shrunk − (1−p_shrunk)/b` where `b = (1-price)/price` (net odds for a
   share paying 1.0/0.0). Degenerate `price ≤ 0` or `≥ 1` returns `f* = 0` rather than dividing by
   zero (shouldn't reach this function given `process_trade()`'s own price validation, defended
   anyway).
3. **Half-Kelly**: `f_half = 0.5 · f*` (`config.KELLY_FRACTION_MULTIPLIER`) — the standard
   fractional-Kelly correction; full Kelly reliably overbets under the systematic overconfidence
   prediction-market traders show (same research pass).
4. **Mapped into the SAME bounded `MIN_TRADE_USD..MAX_TRADE_USD` range this bot has always used**,
   not a bankroll-fraction bet — `f_half` clamped to `[0,1]`. True bankroll-fraction Kelly would
   need to know total capital and reshape the portfolio risk manager's exposure-ceiling interaction
   (Rule 6) — a bigger, separate change, not made here.
- **Two-tier fallback** (category → composite → `BASE_TRADE_USD`), same structure as before but now
  selecting a `(win_rate, trade_count)` PAIR instead of a single score. `db.
  get_wallet_composite_scores()` extended to surface `composite_win_rate`/`composite_trade_count`
  (`wallet_profile.win_rate`/`trade_count_all_time`, real columns `scoreWallets.ts` already
  populates, never previously selected by this function) so the composite tier has a pair to work
  with too, not just the category tier.
- `should_skip_category()` — **UNCHANGED**. An independent, stricter gate on statistically
  significant harm; never depended on the sizing formula and doesn't need to change because it did.

**A real, intentional mathematical property, stated plainly rather than left for someone to
discover**: raw Kelly's fraction is bounded above by `p_shrunk ≤ 1`, so HALF-Kelly is bounded above
by 0.5 — it can never clamp to the full `[0,1]` range this maps into `MIN..MAX_TRADE_USD`, even for
a near-certain edge at a huge sample size. Under the default `MIN=$3, MAX=$10, MULTIPLIER=0.5`, the
practical achievable ceiling is `$3 + 0.5×($10−$3) = $6.50`, not $10 — a direct, deliberate
consequence of always halving, not a bug. `MAX_TRADE_USD` remains the absolute hard safety cap
(relevant if `KELLY_FRACTION_MULTIPLIER` is ever raised above 0.5), just not one half-Kelly reaches
under today's settings.

**A second real, intentional consequence**: the COMPOSITE-tier fallback uses the wallet's LIFETIME
trade count as `n`, often in the thousands — so composite-tier sizing barely gets shrunk at all,
while category-tier sizing (typically 5-50 trades) gets shrunk hard. Correct, not a bug: composite
fallback only fires when there's no category-specific evidence, so trusting a much deeper sample
more is the right call.

**Score-snapshot drift, fixed in the same turn it happened**: `process_trade()`'s `score_breakdown`
tier-detection logic (Rule 22) duplicates `compute_trade_size_usd()`'s fallback logic by design (to
avoid changing that function's return type) — `SAFETY.md` §16 already flagged this duplication as
an accepted, watched drift risk. This rule is that drift actually happening; the duplicate logic was
updated in the same change, not left stale. The snapshot now also records `shrunk_win_rate` and
`kelly_fraction` — real values worth keeping for the eventual Brier/structural-break analysis
(Rule 22 Parts C/D) to use.

**Report only in the sense that matters here**: this changes real position sizing for future paper
trades once `bot.py` is restarted to pick it up (same `/api/toggle` procedure as Rule 22) — not a
discovery/reporting tool like Rules 20/21/23/24.

**Why it exists:** direct follow-through on an explicit request to replace the linear sizing ramp
with real half-Kelly, using win rate, price-implied odds, and small-sample shrinkage — all three
inputs requested, each grounded in research already cited this session rather than invented fresh.

---

## 26. Per-wallet exposure cap

**What it does:** caps total USD deployed across ALL open positions tied to a single tracked
TRADER, regardless of which market or category each position is in — `config.MAX_WALLET_EXPOSURE_USD
= $50` by default. Motivated directly by Rule 24 (category quota discovery) allowing one wallet to
occupy multiple category slots when its statistics justify it (a wallet's edge might be process-
driven — speed, arbitrage — rather than domain-specific, so multi-category tracking is intentional,
not restricted): without this cap, a wallet filling 3 quota slots could accumulate exposure across
3 different events simultaneously with no existing control noticing, since each individual copy
clears the per-event cap fine on its own.

**A genuinely different axis from the two existing caps, not a duplicate**:
`MAX_TOTAL_EXPOSURE_USD` (Rule 6) is portfolio-wide; `MAX_EVENT_EXPOSURE_USD` (Rule 6) is per-EVENT
(could span several different wallets betting on the same event); this is per-WALLET (could span
several different events for the same trader). All three are checked independently in `check_buy()`
— any one failing skips the BUY.

**How it works mechanically**: `risk_manager.wallet_exposure_usd(positions, wallet_address)` —
structurally identical to the existing `total_exposure_usd()`/`event_exposure_usd()` (same
cost-basis summation, same "would exceed" semantics in `check_buy()`, same `None`-disables
convention) — not a new pattern, a third instance of one already used twice. `check_buy()` gained
an optional `wallet_address` parameter (defaults to `None`, in which case this check is simply
skipped — no existing call site is forced to change).

**Interaction with Half-Kelly (Rule 25) and the category quota (Rule 24), stated explicitly**:
- Ordering: `compute_trade_size_usd()` (Half-Kelly) runs first and produces `trade_usd`, THEN
  `check_buy()` (including this cap) evaluates whether that already-sized trade fits. An oversized
  Half-Kelly suggestion against a nearly-full wallet budget gets SKIPPED, never silently shrunk —
  the same behavior the existing total/event caps already have, extended, not reinvented.
- Fully orthogonal to Rule 24 on purpose: the quota controls WHICH wallets get discovered/tracked
  (a one-time, human-reviewed decision); this cap controls how much CAPITAL a tracked wallet can
  accumulate at runtime. A wallet occupying 3 quota slots doesn't get 3× the exposure budget — it
  shares the SAME $50 across every position opened under that address, in any category.

**Why it exists:** direct follow-through on an explicit request — "to manage concentration risk, we
need to introduce a MAX_EXPOSURE_PER_WALLET setting" — after reconsidering an earlier flagged
concern (cross-category wallets) and choosing to allow multi-category tracking rather than
restrict it, with this cap as the correct tool for bounding the resulting concentration instead.

---

## 27. TCA entry-price floor

**What it does:** rejects a discovery candidate outright — regardless of how good its ROI looks —
when its category's cost-basis-weighted `avg_entry_price` sits within `TCA_MIN_ENTRY_PRICE` (default
2 cents) of either price extreme. Extends Rule 21's TCA filter; does not replace it.

**The live-verified finding that motivated this**: reviewing the full discovery run's 14 candidates
by hand, `0xc21ea96b...` (occupying 3 category slots) turned out to have `avg_entry_price = $0.001`
— Polymarket's minimum price tick — on **every one of its 1,206+ trades, across all 4 categories,
with a 100% win rate in every one**. That's not longshot-picking skill; it's the signature of
settlement/resolution sniping (buying the already-effectively-certain side for pennies right before
formal on-chain resolution). Rule 21's TCA filter passed it anyway (ROI 100-190% comfortably clears
the ~12-15% modeled cost bar), but the bar itself is unsound at this price point:
1. `estimatedRelativeSpread()` (Rule 21) was calibrated from research anchored at price ≥ $0.10 — it
   flatlines at 15.5% for everything below that, including $0.001, a price 100x more extreme than
   what the estimate was actually validated at. No research grounding exists for trusting that
   number this deep into the tail.
2. Even if the spread estimate were right, the tiny pool of floor-priced liquidity this wallet
   captured is almost certainly gone by the time a lagging copy-bot detects the trade and fires its
   own order — the historical ROI describes a fill we structurally cannot replicate.

**How it works mechanically**: `passesEntryPriceFloor(avgEntryPrice, minEntryPrice)` — pure,
exported, same shape as `passesTcaFilter`/`flagsWashTradingSuspicion`. Symmetric around 0.5
(matching `estimatedRelativeSpread()`'s own existing symmetry — a $0.999 "already-won" floor-price
buy is the identical failure mode mirrored). Strict inequality (landing exactly on the floor doesn't
clear it), matching `passesTcaFilter`'s own convention. Runs BEFORE the TCA filter, not alongside
it — `avg_entry_price` is a direct input to the TCA formula itself, so a candidate whose entry price
isn't trustworthy never has its TCA verdict computed/trusted at all.

**`TCA_MIN_ENTRY_PRICE = 0.02`, an explicit judgment call, not dressed up as more rigorous than it
is**: a full order of magnitude above the confirmed $0.001 platform tick, chosen to separate
"genuinely thin-probability market" from "structurally at the platform floor" — `estimatedRelativeSpread`'s
own `price < 0.10` anchor doesn't further distinguish within that range, so this draws a new,
separate line rather than borrowing false precision from the spread model. Configurable via
`--tca-min-entry-price`, tunable with more evidence, same as every other TCA parameter.

**A hard rejection, not a warning** — unlike Rule 23's wash-trading screen (deliberately a "look
closer" annotation, since the false-positive risk against genuinely disciplined small-edge traders
was real), the false-positive risk here is low: a 100% win rate at the literal platform floor, on
every trade, across every unrelated category, is about as close to definitive evidence of this
specific failure mode as this kind of analysis gets. Rejected candidates get their own report
section (`--- Rejected: entry price too close to the platform floor ---`), separate from TCA
rejections — the reason is different (untrustworthy price point, not "edge too small").

**Live-verified**: re-ran the real `0xc21ea96b` data (already pulled to investigate this) through
the new gate — correctly rejected in all 4 categories (`entryPriceFloor=false` in every case),
confirming it's `passesEntryPriceFloor` doing the rejecting, not something `passesTcaFilter` already
caught (which independently still returns `true` for this wallet on its own).

**Why it exists:** direct follow-through on a trader-level objection to a specific discovered
wallet's paper-thin per-trade dollar profit — investigating it with real numbers (not just the
instinct) surfaced a more specific, more actionable problem than "the edge is thin": the price point
itself is untrustworthy, not just small.

---

## 28. Strategy-Tiered Expanded Discovery & Tracked-Trader Curation (2026-07-24)

**What it does:** a second discovery round, run on top of the already-shipped Rules 20/21/23/24/27
(no new permanent filter code — this is discovery methodology + a real `config.TRACKED_TRADERS`
edit, not a new gate). Two parts:

**Part A — Strategy-Tiered Expanded Discovery.** Rather than filling the remaining category-quota
slots by raw t-stat alone, candidates from a fresh leaderboard scan (`scan:leaderboard` ->
`scan:wallets` -> `discoverCategorySpecialists.ts --exclude <7 already-kept wallets>`) were
classified by trading-strategy profile using a new pure function, `classifyStrategyTiers()` in
`_wallet_deep_dive.ts` (unit-tested, 17 cases). Six tiers, all built from data the pipeline already
computes — no hold-time/execution-speed data exists anywhere in this codebase (`RealizedClose`
carries no timestamps), so any tier implying trade *speed* (originally-requested "momentum") is
honestly relabeled rather than falsely claimed:

- **Political Macro Whale** — `category=politics AND avg_pnl_usd>$50 AND trade_count<100`
- **Sports High-Frequency Scalper** — `category=sports AND trade_count>100 AND avg_pnl_usd<$20`
- **Crypto Specialist** — any crypto candidate, explicitly caveated "momentum/execution-speed NOT
  verified — no data"
- **Cross-Category Quant Generalist** — `category_count>=3 AND top-3-market concentration<30%`
- **Tail-End Yield Farmer** — `win_rate>0.85 AND avg_entry_price>0.85 AND entry_price_CV>20%`. The
  CV floor is a deliberate safeguard added beyond the original spec: buying near-certain outcomes at
  high price on every trade is Rule 27's floor-sniping pattern mirrored at the ceiling, and Rule 27's
  own $0.98 ceiling alone wouldn't catch a candidate sitting at $0.90 every time.
- **Pop-Culture Specialist** — `category=pop-culture`, no additional constraint

A candidate can match multiple tiers (returned as an array, never forced into one label). A
"Weather Model Arbitrage" tier was explicitly requested but dropped: `packages/weather` is a
separate, already-live bot trading temperature markets independently — adding it here risked two of
this project's own systems trading the same market against each other. Revisit only with an
explicit coexistence plan.

**Part B — the actual tracked-wallet curation.** Comparing the 7 kept + 1 new candidate against the
live `config.TRACKED_TRADERS` (20 entries) surfaced that one candidate (`0x7f9e2d1d...`) was already
tracked as `geo-anon-3` — only 7 were genuinely new. Rather than growing the list to 27, the
decision was "replace to stay near ~20": pull each existing wallet's current `wallet_profile.
composite_score`/`status` (most were scored by an older, since-superseded pipeline run — 14 of the
20 sat at `status=ignore` under today's scorer) plus its REAL paper-trading history from this bot's
own `paper_trade` table, and swap out the 7 weakest.

**Confirmed before swapping**: de-tracking a wallet does not orphan its open positions.
`run_closeout_sweep()` and the TTP-exit sweep both iterate over every open position already in
state unconditionally — not filtered by current `TRACKED_TRADERS` membership — so a de-tracked
wallet's existing open trades still get resolved/closed normally; de-tracking only stops copying
*new* buys from that wallet going forward.

**Dropped** (all `composite_score` 0.0-0.129, `status=ignore`, weakest or nonexistent real
paper-trading track record; `strict-10` additionally had the *most* real data of any drop candidate
and it was negative, -$2.65 over 6 closed trades): `strict-3`, `strict-6`, `strict-10`, `expand-2`,
`expand-3`, `geo-anon-4`, `fed-559b`.

**Added** (all individually deep-dive-reviewed for the `0xc21ea96b` mechanical/sniping signature —
entry-price CV, single-market concentration — before being trusted; see `config.py`'s inline
comments for each wallet's specific stats): `quant-generalist-1` (`0x2b3d1e9b...`),
`political-whale-1` (`0x510904c9...`), `quant-generalist-2` (`0xe154165732...`), `yield-farmer-1`
(`0xd52b8dcb...`), `generalist-weak-1` (`0x1e1f1741...` — flagged honestly as the weakest of the
batch, t-stats 1.25-1.89, below the usual 1.96 significance bar; first to cut if trimming later),
`sports-scalper-1` (`0x011f2d377...`), `crypto-specialist-1` (`0xdc767c90...`).

**Explicitly excluded from this round**: `0x94e4639e...` — its two statistically strongest
categories (`other`, `politics`) both have `avg_entry_price` under Rule 27's $0.02 floor (0.0127 and
0.0120 respectively), leaving only a thin 11-trade crypto tail that barely clears it. Not compelling
enough on its own.

**Net effect**: `TRACKED_TRADERS` stays at exactly 20 entries. This edit went live in the same
restart that activated Rule 25 (Half-Kelly sizing) and Rule 26 (per-wallet exposure cap), both
shipped earlier the same day but pending exactly this restart to take effect.

**Why it exists:** direct response to Joey wanting cross-category wallets judged on process-driven
edge rather than artificially restricted to one domain, plus a request to fill remaining slots by
trading-strategy profile instead of category count alone — and, once the candidates were in hand, a
request to curate the tracked list back down to ~20 using real evidence rather than just appending.

---

## 29. "Dip & Rebound" resting paper orders — pilot on strict-4 only (2026-07-24)

### Summary: the three problems this solves

To safely delay execution on a copy-traded whale's buy — instead of copying it
the instant it's observed — without that delay itself becoming a source of
slippage or adverse selection, this system addresses three fundamental
problems.

#### 1. The "Price Strategy" problem

**Challenge:** how to avoid overpaying, or getting dragged into a bad average
price, by one erratic or high-priced fill from the whale we're following.

- **Mechanism: VWAP anchor price.** The bot computes a volume-weighted
  average of the whale's own fills (`bot_source_position.cost_basis_usd` /
  `shares`), not just their latest trade — one small overpriced buy can't by
  itself drag our entry point up.
- **Mechanism: ratchet-down logic.** `anchor_price` is mathematically only
  ever allowed to decrease (`compute_anchor_price()` — `min(existing, new)`).
  We never chase the price up, even if the whale does.

#### 2. The "TTL & timing" problem

**Challenge:** an order can sit unfilled for hours — how do we make sure our
risk limits are still being checked against the world as it actually is at
the moment we finally act, not the world as it was when the signal first
arrived?

- **Mechanism: synchronous state sweep.** A periodic pass over the
  `pending_execution` table (`sweep_pending_executions()`, run every poll
  cycle inside `bot.py`'s existing `while` loop — no asyncio in this
  codebase, confirmed by reading it before designing this). An order that
  exceeds its Time-To-Live (`LIMIT_ORDER_TTL_SECONDS`, 4 hours) without
  triggering is automatically abandoned and logged, not left resting forever.
- **Mechanism: just-in-time (JIT) risk checks.** `_execute_buy()` is a
  strictly separated function. Portfolio-level risk checks (exposure caps,
  drawdown kill switch, muted traders) are **not** evaluated when the order
  is first created — they're evaluated at the exact moment it fires, so a
  multi-hour-old signal can't bypass a risk limit that only became binding
  after it was created.

#### 3. The adverse-selection problem

**Challenge:** in prediction markets, a price dropping to our target level
often means something real changed (genuine bad news for that outcome), not
random noise — a blind limit order is "catching a falling knife."

- **Mechanism: Dip & Rebound (a trailing-stop entry, inverted for buying).**
  The bot never buys on the raw dip. Once price drops below the VWAP anchor,
  it tracks `lowest_seen_price` — the local minimum — and only fires once
  price has **rebounded** off that minimum by a confirmed margin
  (`has_rebounded()`, a hybrid tick-floor/percentage threshold — see below),
  treating that rebound as the market's own signal that the drop is over.
- **Mechanism: whale-hold verification.** *(Not literally on-chain — this
  reads the same polled trade feed the rest of the bot already uses, i.e.
  `source_positions`/`source_cost_basis` built from observed BUY/SELL
  trades since we started tracking, not a live wallet-balance query; "whale
  hold" is the accurate name for what it checks.)* Before firing on a
  confirmed rebound, the bot verifies the whale hasn't sold out from under
  the dip themselves — if their remaining shares have dropped below
  `LIMIT_ORDER_WHALE_HOLD_MIN_FRACTION` (50%) of what they held when the
  order was created, it's invalidated instead of fired. We only buy the
  rebound if the smart money is still holding the bag.

---

**What it does:** a genuinely different execution model for ONE tracked wallet
(`config.LIMIT_ORDER_TRACKED_WALLETS`, currently just `strict-4`). Instead of
copying a BUY the instant it's observed at whatever price is live, the bot
tracks that wallet's own volume-weighted average cost basis and waits — a
resting `pending_execution` — for the market to dip below it AND confirm a
genuine rebound before ever buying, re-verifying the whale hasn't exited in
the meantime. Every other tracked wallet is completely unaffected.

**Why not just buy at the whale's price the moment it's touched (the
original ask)?** That's textbook adverse selection / "getting picked off" —
a resting order that only fills on a dip gets filled disproportionately by
*informed* flow, precisely when the market is moving against the position,
not when it's random noise (see the limit-order/pegged-order literature:
Kelley School's "Pegged Limit Orders," and "Limit Order Strategic Placement
with Adverse Selection Risk," arXiv:1610.00261). This is sharper on
Polymarket than in equities: prices move on **real-world news**, not mostly
order flow, so a dip is more often "the team is now losing" than mean-
reverting noise. Buying the instant price touches the whale's cost basis
means buying disproportionately often right as the edge disappears.

**The four pieces, in the order they run (`sweep_pending_executions`, every
poll cycle — see SAFETY.md §23 for the mechanics):**

1. **VWAP anchor, ratchet-down only ("no high-chasing").** `anchor_price` is
   the whale's own volume-weighted average cost on their currently-held
   shares in this market+outcome (`bot_source_position.cost_basis_usd`,
   maintained with the exact same weighted-average-on-buy /
   proportional-reduce-on-sell model this bot already uses for its own
   positions — just mirrored onto the whale's side). `compute_anchor_price()`
   never raises this anchor once set, even if the whale's own price runs up
   further — only a strictly lower observed price moves it. The order will
   never buy above this price, period (see point 3).
2. **Dip-and-rebound confirmation, not a bare touch.** Once price first
   dips below `anchor_price`, the sweep tracks the local minimum
   (`lowest_seen_price`). The order only fires once price has climbed back
   up by a confirmed margin off that minimum (`has_rebounded()`) — never on
   the raw dip itself. The rebound threshold
   (`compute_rebound_threshold()`) is `max(LIMIT_ORDER_REBOUND_TICK_FLOOR *
   tick_size, LIMIT_ORDER_REBOUND_PCT * lowest_seen_price)` — a hybrid,
   deliberately, not either alone: a pure percentage is meaningless noise
   on a longshot price (5% of $0.05 is a quarter of one tick — smaller than
   the platform can even express), while a pure tick count is
   inconsistently strict across Polymarket's $0.01-$0.99 range (2 ticks is
   a ~4% move at $0.50 but a ~100% move at $0.02). The tick floor governs
   at low prices, the percentage governs in the mid-to-high range, and the
   two agree exactly at the crossover (price=$0.40 with the current
   defaults) — no branching needed at the call site. Defaults:
   `LIMIT_ORDER_REBOUND_TICK_FLOOR=2`, `LIMIT_ORDER_REBOUND_PCT=0.05`.
3. **No-chase ceiling enforced at fire time, not just at anchor-setting
   time.** If a confirmed rebound has already carried price past
   `anchor_price` by the time the sweep checks, firing would itself violate
   the no-high-chasing rule — the order is invalidated
   (`rebound_exceeded_anchor`), not chased.
4. **"Is the whale still holding" — checked every cycle, not just at fire
   time.** `whale_still_holding()` compares the wallet's current remaining
   shares in this market+outcome against a baseline snapshotted at order
   creation (`whale_shares_at_creation`). If it's dropped below
   `LIMIT_ORDER_WHALE_HOLD_MIN_FRACTION` (default 0.5 — must still hold at
   least half), the order is invalidated (`whale_sold`) immediately, on
   whichever sweep cycle first observes it — not deferred until a rebound
   would otherwise have fired. A small routine trim doesn't invalidate
   (0.5 is a real reduction, not "any sale at all") — an explicit judgment
   call between "block on any sale" and "only block on a real exit," not
   derived from data.

**TTL:** `LIMIT_ORDER_TTL_SECONDS = 4 * 3600` (4 hours). An order that never
confirms a rebound (or gets invalidated first) in that window expires,
logged as `limit_order_abandoned` with the full anchor/lowest-seen/last-
price context — an honest missed opportunity, not silently dropped.

**`_execute_buy` extraction — the part of this that matters most for
safety.** All portfolio-level risk gates (exposure ceilings, kill switch —
`risk_manager.check_buy`) are now in one function, `_execute_buy()`,
shared by BOTH the normal immediate-copy path (every other tracked wallet)
and this sweep's fill path. This is not just deduplication: it is what
guarantees risk is evaluated **at the literal moment of execution**, not
frozen back when the original signal arrived — which for a resting order
can be hours earlier, during which portfolio exposure can have changed
substantially. Live-verified: the existing Python suite's
`TestProcessTradeScoreSnapshot` (which exercises the extracted code path
end-to-end) still passes unchanged, plus new direct tests of
`_execute_buy` itself (`TestExecuteBuyExtraction`).

**Roadmap note — PRODUCER half now built (2026-07-24), CONSUMER half still
not:** this pilot deliberately stays on the existing synchronous
poll-and-sweep architecture (`bot.py` has no asyncio anywhere — confirmed by
direct code read before designing this, correcting an earlier assumption
that it did) rather than adopting real-time events. `pending_execution`'s
schema is intentionally event-shaped (one row per tracked signal, its own
lifecycle, cheaply queryable by status+expiry) so that a future migration to
a real-time Polygon RPC websocket feed only has to change WHERE rows get
created/updated FROM — the table, the anchor/rebound/whale-hold logic, and
`_execute_buy` would all carry over unchanged. `wss_listener.py` (new,
standalone script, root of the repo) is the first real step toward that: a
SEPARATE OS process (asyncio, deliberately not merged into `bot.py`) that
subscribes to Polygon WebSocket logs for a tracked wallet's on-chain CTF
trades and writes them into a new `live_whale_event` table — a
Producer-Consumer hand-off through the shared SQLite DB (safe across two
processes specifically because this DB already runs in WAL mode). This is
the PRODUCER side only — `bot.py` does not yet read `live_whale_event` at
all. See `wss_listener.py`'s own module docstring for full detail, including
explicit caveats: its Polymarket contract addresses were web-search-verified
but NOT independently confirmed by a human against PolygonScan, and its
exact live-socket payload shape was not end-to-end tested against a real
WSS endpoint (no credentials available to test with) — verify both before
trusting it against real capital.

**The `token_id` -> `market_slug`/`outcome` resolution gap flagged above is
now closed (2026-07-24)**: `token_sync_worker.py` (new, standalone script,
same Producer-role precedent as `wss_listener.py`) populates a new
`token_registry` table by paging through Polymarket's Gamma API. Live-tested
end-to-end while building it (not just syntax-checked): 300 real markets
fetched across 3 pages, 600 real token rows correctly upserted, re-run
confirmed idempotent (still 600 rows, `updated_at` refreshed). One
significant course-correction made while building this: the endpoint
originally specified (`/markets?active=true`, offset/limit pagination) is
**deprecated with a sunset date of 2026-05-01, already past** (confirmed
live via response headers) — used `/markets/keyset` (cursor-based, via an
`after_cursor` param that took several live guesses to find, since it isn't
documented on the endpoint's own schema page) instead, Polymarket's own
stated replacement. `bot.py` still does not read either `live_whale_event`
or `token_registry` — both remain producer-only until a consumer sweep is
built. See `docs/copy-trading/SAFETY.md` §25 for full technical detail.

**Scope, deliberately narrow:** `config.LIMIT_ORDER_TRACKED_WALLETS`
contains exactly one wallet (`strict-4`) — this is a genuinely different
execution model from every other tracked wallet's immediate-copy behavior,
worth validating narrowly (and specifically against the wallet whose
Dota 2/esports order-sweeping pattern motivated it — see the `failed_trade`
history investigation this design followed from) before ever widening it.

**Why it exists:** direct response to observing `strict-4`'s history of
`failed_trade` events (huge whale orders sweeping thin esports order books
before this bot's small copy could fill) — the original idea was a resting
limit order pegged to the whale's own price, which on reflection is exactly
the adverse-selection failure mode the limit-order literature warns about;
this is the redesigned version built to avoid that specific trap.

---

## 30. Consumer sweep for on-chain trade detection (`live_whale_event`/`token_registry`)

### Summary: the four problems this solves

To connect wss_listener.py's/token_sync_worker.py's producer tables into
`bot.py`'s actual trading pipeline — the piece explicitly left unbuilt when
those two scripts shipped — this sweep addresses four distinct problems,
one of which is flagged rather than solved.

#### 1. The "duplicated decision logic" problem

**Challenge:** an on-chain-detected trade still needs every gate a
polling-detected trade gets (muted trader, duplicate position, the buy-count
cap, category hard-skip, AND — for `strict-4` specifically — the Dip &
Rebound flow from Rule 29). Building a second, bespoke path straight to
`_execute_buy()` would either silently skip all of that, or require
re-implementing it a second time, with every future change now needing to
happen twice and inevitably drifting apart.

- **Mechanism: reshape into the existing `trade` dict, call `process_trade()`
  itself.** Not a new decision path — the exact same one every other
  detected trade already goes through, regardless of which mechanism
  detected it.

#### 2. The "wrong timestamp type" problem

**Challenge:** the literal ask was `UPDATE ... SET consumed_at =
CURRENT_TIMESTAMP` — SQLite's `CURRENT_TIMESTAMP` produces a text ISO8601
string, while every other timestamp column in this database (including
`consumed_at` itself, per its own schema) is a unix-epoch integer. Writing
the former into the latter's column silently creates one inconsistently-typed
row that anything reading `consumed_at` as an integer (Drizzle's `{mode:
"timestamp"}` decoding, any Python arithmetic on it) would mishandle.

- **Mechanism: `unixepoch()` / `_now_ts()`, matching the established
  convention.** Same integer-seconds-since-epoch shape as every other
  timestamp write in `db.py`.

#### 3. The "never lose, never double-process" problem

**Challenge:** a crash partway through handling one event must neither
strand it (retried forever, if never marked consumed) nor lose it (marked
consumed without ever having been acted on).

- **Mechanism: try/finally, unconditional, independently committed.**
  `mark_whale_event_consumed()` runs in a `finally` block — success, a risk
  gate block, a deferral to `pending_execution`, or a raised exception all
  reach it — on its own dedicated connection+commit, separate from whatever
  connection `process_trade()`'s own writes used.

#### 4. The "two detectors, one wallet" problem — flagged, not solved

**Challenge:** if the same wallet is watched by both this WSS-fed path and
the normal polling loop at once, the same real-world trade can be detected
twice through two completely unrelated identifier spaces — on-chain
`tx_hash`+`log_index` here, the polling feed's own `trade_id` there. No
shared key exists to recognize them as the same event.

- **Mechanism (partial): `process_trade()`'s existing duplicate-position/
  `MAX_BUYS_PER_TRADER_OUTCOME` checks.** These block an obvious duplicate
  buy on the same key, but this is not a clean, complete fix.
- **The real fix, not applied automatically:** exclude any
  `wss_listener.py`-watched wallet from `main()`'s polling
  `wallet_addresses` list, so there is exactly one detector per wallet, not
  two racing each other. Left as a deliberate human decision.

**What it does:** `sweep_live_whale_events()`, wired into `main()`'s poll
loop ungated (same "zero-latency, don't wait for an interval" reasoning as
Rule 29's own sweep). Each cycle: `db.get_unconsumed_whale_events()` (an
INNER JOIN of `live_whale_event` against `token_registry` on `token_id`,
capped at `config.WHALE_EVENT_SWEEP_BATCH_LIMIT=50` per cycle) returns every
unprocessed on-chain event that already has a resolved `market_slug`/
`outcome`. A row with no `token_registry` match yet is simply absent from
this query — not force-processed with missing data — so it's naturally
retried on a later sweep once (if) `token_sync_worker.py` catches up. Known
gap, not fixed here: a market that resolves/closes before its token_id is
ever synced (since `token_sync_worker.py` only fetches `active=true`
markets) could leave its `live_whale_event` rows permanently unmatched.

Per event: non-`'buy'`-direction rows and rows with a `NULL`
`price`/`usdc_amount` (no on-chain collateral leg was found — see Rule 29's
roadmap note on `wss_listener.py`) are still marked consumed, just not
turned into a trade — logged, not silently dropped. A wallet not in the
active tracked-traders source is skipped the same way `main()`'s own
polling loop already gates untracked wallets — `process_trade()` itself
does not enforce that boundary, so this sweep must, mirroring the exact
same check.

**Why it exists:** the last unbuilt piece of the Producer-Consumer chain
`wss_listener.py`/`token_sync_worker.py` were built for — without this,
on-chain trade detection produced data nothing ever acted on.

### 2026-07-25 update: global WSS coverage, faster sync, on-demand token
resolution — and ONE explicit refusal

Joey asked for a full cutover to event-driven architecture: 15-minute
`token_sync_worker.py` scheduling, `wss_listener.py` watching every tracked
wallet instead of just the pilot, an on-demand Gamma fallback for tokens
`token_registry` hasn't synced yet, and — the part not built —
**completely deleting `bot.py`'s 30-second polling detection loop.**

#### 1. The "sync freshness vs. API load" problem

**Challenge:** `token_sync_worker.py` originally ran once and exited —
useful for the initial verification, not for keeping `token_registry`
current as new markets appear.

- **Mechanism: a persistent daemon.** `main()` now loops forever — sync,
  sleep `SYNC_INTERVAL_SECONDS` (15 min default, env-overridable), repeat —
  matching `wss_listener.py`'s own long-running structure rather than
  depending on external cron/launchd infra this repo doesn't have set up
  for its own processes. A single failed sync cycle is logged and followed
  by the next scheduled one, not a crash.

#### 2. The "one pilot wallet isn't the whole book" problem

**Challenge:** `wss_listener.py` only watched `strict-4` — real-time
detection for 19 of the 20 currently tracked wallets didn't exist.

- **Mechanism: default to `config.TRACKED_TRADERS`, not a hand-maintained
  list.** `WHALE_WALLET_ADDRESSES` now only needs to be set to narrow the
  watch list (e.g. for testing); left unset, it defaults to every wallet in
  `config.TRACKED_TRADERS` — so a wallet added to the tracked list later is
  picked up automatically on this script's next restart, not silently
  unwatched until someone remembers to update a second, separate list.

#### 3. The "brand-new market beats the sync" problem

**Challenge:** `token_sync_worker.py` only runs every 15 minutes — a trade
on a market younger than that has no `token_registry` row yet, and the
Rule 30 sweep's INNER JOIN would leave it unmatched (correctly retried, but
not actionable) until the next sync cycle happens to catch it.

- **Mechanism: on-demand Gamma fallback, verified live before being
  built.** `polymarket_simulator.fetch_market_by_token_id()` — NOT
  `/tokens/{token_id}` as originally specified (confirmed live: that path
  404s), but `/markets/keyset?clob_token_ids=<id>`, Gamma's own current
  endpoint, verified to correctly resolve a real token_id. On a hit: the
  row is upserted into `token_registry` (so the SAME token_id hits the
  fast path on every future event) and the trade proceeds immediately
  through the identical `process_trade()` path the fast path uses. On a
  miss (Gamma hasn't indexed the market either yet): retried on later
  sweeps, up to `config.WHALE_EVENT_FALLBACK_MAX_AGE_SECONDS` (1h) old,
  past which the sweep gives up rather than retrying a token_id that may
  never resolve.

#### 4. The "is WSS detection even complete enough to trust alone" problem — this is why polling was NOT deleted

**Challenge:** eliminating the double-spend risk by deleting the one
detection mechanism that has been proven, in production, for this entire
project — in favor of one that has never been run against a live socket —
right as the exposure cap goes to $1250, is a materially different kind of
risk than the one being eliminated. Concretely, three separate gaps:

- `wss_listener.py` has never processed a real `eth_subscription` payload.
  Its docstring already states this: the exact runtime shape was verified
  against the installed `web3` package's API surface, never against a live
  socket, for lack of credentials to test with.
- Price derivation is best-effort, not guaranteed — a `TransferSingle`
  without a matching paired collateral transfer in the same transaction
  yields `NULL` price, and `sweep_live_whale_events()` correctly skips
  those, meaning some genuine trades produce no signal at all today.
- **New finding, made while evaluating this specific request:** not every
  CTF `TransferSingle` is a market trade. Gnosis Conditional Tokens'
  `splitPosition` (mints outcome tokens) and `mergePositions`/
  `redeemPositions` (burn them) are commonly represented as
  `TransferSingle` events too, with the zero address as the mint source or
  burn destination. Undetected, either could be mistaken for a genuine
  buy/sell signal. **Fixed as part of this same change** — `_handle_log()`
  now skips any transfer where the relevant counterparty is the zero
  address — but this was an undocumented gap in the original design until
  today, discovered specifically by asking "is this feed complete enough
  to be the ONLY detector," not by observing a real failure.

- **Mechanism: keep both detectors running, decline the deletion.**
  `bot.py`'s polling loop is unchanged. The double-spend risk Rule 30 §4
  already flagged stays partially mitigated the same way it already was
  (`process_trade()`'s duplicate-position/`MAX_BUYS_PER_TRADER_OUTCOME`
  checks), not eliminated — an explicit, known trade-off, not a fix,
  chosen because removing the proven fallback before the new path has ever
  seen live traffic is a strictly worse failure mode: silent, total loss
  of trade detection if any of the three gaps above turns out to matter in
  practice, discovered only after the fact.

**What was NOT done, and remains a live decision for later:** a real
migration off polling — once `wss_listener.py` has run against production
for some real period with its behavior spot-checked against what polling
independently detected, wallet-by-wallet exclusivity (removing a
WSS-validated wallet from the polling list) becomes the actual clean fix
for the double-spend risk, in place of relying on `process_trade()`'s
gates as a partial backstop. Not attempted here — this response only
built the capability (global wallet coverage) and the missing piece
(on-demand resolution), not the cutover itself.

### 2026-07-25 update: 'Dual-Track' CQRS — WSS executes, polling reconciles

The declined-deletion tension above (problem 4) resolved into a genuine
architecture, not a stalemate: **WSS is now the primary EXECUTION trigger**
(speed), **polling becomes RECONCILIATION** (accuracy) rather than a
redundant second executor.

#### 1. The "wait for a price that might never come" problem

**Challenge:** the previous design skipped any `live_whale_event` with no
derivable whale price — meaning WSS's speed advantage was wasted on exactly
the trades where the collateral-transfer correlation failed to find a
match.

- **Mechanism: execute on direction + market alone.** A `NULL` whale price
  no longer blocks execution — `_handle_matched_whale_event()` fetches OUR
  OWN current market price (`get_market_ask_price()`, the same no-custody
  read Rule 29 already uses) and executes against that instead. Tagged
  `price_source="wss_estimated"`.

#### 2. The "estimated price must not corrupt the whale's tracked position" problem

**Challenge:** using our own price as a stand-in is fine for OUR execution,
but `source_positions`/`source_cost_basis` (the whale's own tracked
holdings — what Rule 29's Dip & Rebound anchor and whale-hold guard depend
on) must still be accurate, or a fabricated price would quietly corrupt
data those safety mechanisms rely on.

- **Mechanism: the share count is never estimated in the first place.** A
  `TransferSingle`'s `share_amount` is a raw on-chain event field — always
  known exactly, regardless of whether price is derivable. `size_usd` is
  back-derived as `shares * our_price` specifically so
  `source_shares = size_usd/price` reproduces the EXACT real share count
  even though neither number individually is the whale's true value — only
  the dollar valuation is ever approximate, never the position size itself.

#### 3. The "two feeds, one real trade, don't double-count" problem

**Challenge:** polling's real job now is filling in the accurate price WSS
couldn't derive — but polling observes this as what looks like a brand-new
BUY signal. Naively re-running the normal buy path would add the whale's
shares to `source_positions` a SECOND time for the same real-world trade.

- **Mechanism: reconciliation, not re-execution.** `process_trade()`'s BUY
  branch checks, before anything else: is there already a position at this
  key tagged `price_source="wss_estimated"`, and is THIS event
  polling-sourced? If so, it corrects `source_cost_basis[key]` to
  `source_positions[key] * (the now-known real price)` — using the share
  count that was already exact — and returns, without touching
  `source_positions[key]` and without attempting a duplicate buy. Our own
  executed position (`positions[key]`) is untouched too — it was priced
  correctly off real live market data at execution time, nothing to
  correct there.
- **Known, accepted limitation:** `config.MAX_BUYS_PER_TRADER_OUTCOME`
  allows up to 2 buys before a position caps. If both land via WSS before
  polling catches up on either, this single-correction can't perfectly
  apportion polling's one real price across two separate estimated buys —
  the correction still applies (strictly better than leaving both
  estimated), just not exactly attributed. Given the narrow reconciliation
  window this needs (seconds to tens of seconds) and the small cap, judged
  a rare, low-severity gap — flagged, not fixed.

#### 4. The "which path caught it first" problem

**Challenge:** with two independent detectors now genuinely racing (by
design), knowing which one actually caught a given trade — and how far
behind the other was — matters for evaluating whether this whole
architecture is working.

- **Mechanism: `detected_by` on every relevant log line.** Stamped `"wss"`
  or `"polling"` on every `paper_buy`/`live_buy` decision_journal row, and
  a dedicated `whale_price_reconciled` event fires whenever polling
  corrects a WSS-estimated position — a direct, queryable record of how
  often WSS wins the race and by how much.

**The zero-address mint/burn filter (already built for problem 4 of the
prior update) is unchanged, already positioned correctly** for "ultra-fast":
it runs before `_find_collateral_transfer()`'s network round-trip — the
only genuinely slow operation in the whole path. The ABI decode ahead of it
is pure in-memory computation (microseconds); splitting it further into a
topics-only pre-check would add complexity without a measurable latency
gain, so it wasn't done.

---

## 31. Institutional-standard upgrades: sell-priority ordering, patient exit pegging, theta-decay TP activation (2026-07-26)

Joey framed this round as four "institutional standard" priorities. One
(the wallet audit) was pure analysis, delivered as data, not code — see
its own summary below. Of the other three, one didn't hold up as
originally specified, one got rebuilt with different math than requested,
and one shipped close to as-is. All three ship OPT-IN, default OFF —
none of these change live trading behavior until deliberately enabled.

### 1. The "buy blocks sell" problem — diagnosed differently than requested

**Challenge, as stated:** buy signals blocking sell signals, costing the
bot capital-freeing exits when the exposure cap is tight.

**What the code actually does:** confirmed by direct read, not assumed —
`risk_manager.check_buy()` NEVER gates a sell (Rule 6/11's own stated
principle: a risk layer that traps you in a position adds risk instead of
removing it). There is no mechanism by which a buy could structurally
block a sell in this codebase. No specific instance was reported either.

- **Mechanism (the real, narrower benefit): sell-first ordering within a
  poll cycle.** `main()`'s trade batch now sorts SELL events ahead of BUY
  events (stable sort, chronological order preserved within each side) —
  so if both land in the same 30s cycle, any capital a sell frees is
  already reflected in `positions` before a same-cycle buy's exposure
  check runs against it, instead of the reverse order costing that buy a
  needless `skip_risk_exposure_ceiling`. A real, small win; not a fix for
  a "blocking" bug, because that bug doesn't exist in this codebase.
- **What was NOT built:** `asyncio.PriorityQueue`/`asyncio.gather()`
  concurrent dispatch — would have required converting `bot.py`'s entire
  synchronous main loop to async, the same kind of ground-shaking
  architecture change already declined once this session (deleting the
  polling loop) for the same reason: high blast radius against a working
  production file, for a problem that, on inspection, wasn't the one
  actually present.

### 2. The "fixed-interval limit orders fail in illiquid markets" problem

**Challenge:** a 30s fixed limit-order window either fails to fill in thin
Polymarket books, or a market order gives away edge to slippage.

- **Mechanism: bifurcated dynamic order pegging for SELL/exit execution.**
  `start_patient_exit()`/`sweep_pending_exit_orders()` place a maker
  limit-sell that walks its price down (`compute_pegged_price()`) toward a
  floor over time, with the reprice interval itself adapting to the
  current spread (`compute_reprice_interval_seconds()`: 120s when
  `spread_ratio > 5%`, 30s otherwise).
- **What changed from the original spec, and why:**
  - **The floor formula.** As given, `P_floor = P_mid - P_mid*(0.209/3.5)`.
    `0.209` traces to a real number (this session's own measured edge at
    the time), but `3.5` doesn't correspond to anything derivable in this
    codebase, and dividing edge by an unexplained constant doesn't have
    clean economic meaning the way multiplying by an explicit fraction
    does. Rebuilt as `floor = mid * (1 - protection_fraction *
    live_edge_pct)`, where `live_edge_pct` comes from
    `db.compute_live_edge_pct()` — the SAME calculation as the 2026-07-25
    sizing report, computed LIVE against current data rather than a
    hardcoded snapshot of a number already shown to move within hours —
    and `protection_fraction` (`config.SLIPPAGE_PROTECTION_FRACTION =
    0.30`) is an explicit, labeled judgment call, not dressed up as more
    derived than it is.
  - **The "retain the position" instruction.** As given, an order that hits
    the floor unfilled gets canceled and the position is simply kept,
    unsold. This directly contradicts Rule 6/11's "never delay an exit"
    principle — the same failure mode those rules exist to prevent, now
    on the exit side instead of the entry side. Rebuilt bounded: past
    `config.ORDER_PEG_MAX_TOTAL_WAIT_SECONDS` (600s, an explicit judgment
    call — not the "maybe an hour" originally floated), the resting order
    is canceled and an IMMEDIATE MARKET SELL fires instead. Every
    `pending_exit_order` this mechanism creates is guaranteed to terminate
    in `filled` or `fallback_market_sell` — never left resting
    indefinitely. If the fallback sell itself fails (the one genuinely
    dangerous outcome — canceled the resting order AND the replacement
    sell didn't go through), that's logged as a loud `error` needing
    manual review, not silently retried into a possible double-cancel.
  - **Verified bullpen capability, not assumed either way.** `limit-sell`/
    `limit-buy` (GTC by default), `orders --cancel`, and `poll-order` all
    checked live before building this — the full place/monitor/cancel/
    replace lifecycle is real and supported. What's still UNVERIFIED: the
    exact response field names for a resting order's id/status
    (`extract_order_id`/`extract_order_status` in `bullpen_client.py`,
    same "plausible candidates, honestly unverified" status
    `extract_fill_price` has always carried — no live limit order has
    ever been placed by this bot).
- **Relationship to Rule 29:** no code-level conflict — Rule 29 governs
  entries (strict-4's buys), this governs exits. Wired into exactly one
  call site this round: `close_position_trailing_tp`'s `LIVE_MODE` branch,
  behind `config.ENABLE_PATIENT_EXIT_PEGGING` (default `False`). NOT wired
  into `process_trade()`'s proportional whale-mirroring SELL branch —
  that path's fractional-sell model doesn't map cleanly onto a single
  patient order, and extending it wasn't attempted this round; a real,
  deliberate scope limit, not an oversight.

### 3. The "static 50% TP activation misses early spikes" problem

**Challenge:** real evidence from the 2026-07-25 sizing report — four
resolved positions peaked between 19%-50% unrealized profit and gave it
all back, never protected because they never reached the fixed 50% bar. A
real $11.92 swing on just those four.

- **Mechanism: theta-decay TP activation.** `T_act = T_min + (T_base -
  T_min) * min(1, D_rem/W)` — the activation threshold scales DOWN from
  50% to 15% as a position's market approaches its own resolution date
  (`compute_theta_decay_activation_pct()`), on the intuition a spike far
  from resolution has more time to reverse (demand a bigger move to trust
  it) while a spike near resolution has less time left (trust a smaller
  one). `T_min`/`T_base`/the 7-day window are Joey's own specification,
  carried through as explicit, labeled constants (`config.
  THETA_DECAY_TP_MIN_ACTIVATION_PCT` etc.) — not derived from a larger
  backtest, same "test before fully trusting" status as the finding that
  motivated it.
- **The missing data piece, built fresh:** no market end-date lookup
  existed anywhere in this codebase. `resolve_market_end_date()` (its own
  function, not folded into `resolve_market_event()` — same
  separate-concern precedent as `resolve_market_category`, since this is
  needed at TTP-sweep time on an already-open position, not BUY time)
  makes the same `bullpen polymarket market` call, reading the `endDateIso`
  field — verified live against a real market before building this.
  Resolved once per market, cached in-memory
  (`risk_state["market_to_end_date"]`) and persisted
  (`bot_market_event.end_date_iso`, a new column on the existing table —
  zero new table needed, matching the precedent `holdingRewardsEnabled`
  already set on this exact table).
- **Opt-in:** `config.ENABLE_THETA_DECAY_TP_ACTIVATION`, default `False` —
  `check_trailing_take_profit()` uses the original static
  `TRAILING_TP_ACTIVATION_PCT` unchanged until this is flipped, and a
  market whose end date can't be resolved falls back to the static
  threshold too, never guesses.

### The wallet audit (Priority 1) — summary, full table given inline

Full comparative table of all 20 currently-tracked wallets vs. the 7
dropped in the 2026-07-24 curation, computed fresh from `bot_event_log`
(all-time win rate, EV per dollar staked, total P&L, trade count, and
close-reason mix as the "yield farmer" signal) — delivered directly in
conversation, not restated here. **The literal "100% hold-to-resolution ->
drop" filter doesn't hold up against the data**: `strict-10` (dropped),
`strict-7`, and `strict-9` (both still tracked) all show that exact
pattern and are all strongly positive — `strict-10` specifically is the
best all-time performer of any wallet ever tracked. EV is what predicts
performance, not exit-mechanism mix alone. The one wallet where the
pattern and negative EV genuinely coincide: `strict-1`, at −$59.12
all-time, the worst performer in the dataset.

**Acted on, 2026-07-26**: `strict-1` kicked from `config.TRACKED_TRADERS`,
`strict-10` re-added — net count unchanged at 20, zero open positions on
either wallet at the time (nothing orphaned). `strict-1` isn't actively
re-monitored by any new mechanism — its `wallet_profile` row keeps getting
refreshed by any future `pnpm scan:wallets` run (that pipeline scores
wallets broadly, not just `TRACKED_TRADERS` members), so its real
performance stays visible without paying to copy it; worth a periodic
manual check if reconsidering later. `strict-3`/`strict-6` stay dropped —
nothing in the fuller history contradicts those original calls.

---

## 32. Paper accounting grounded in real fillable prices, not the whale's own fill (2026-07-26)

**Challenge:** Joey asked, pointedly, whether the bot's tracked "buys" were
really buyable at the price recorded, or just optimistic bookkeeping. They
were the latter. `_execute_buy`/`process_trade`'s SELL close both booked
paper trades at the SOURCE trader's reported price — `measure_paper_shortfall()`
already walked the real, live order book to compute what we could actually
fill at, but that measurement was diagnostic-only, logged and discarded,
never fed back into `avg_entry_price`, `cost_basis_usd`, or realized
`pnl_usd`. Every EV/edge number reported this session (the sizing report,
the wallet audit) was built on that optimistic assumption.

Querying `bot_event_log` directly: of 586 buy events, only 145 (25%) had
ANY shortfall measurement — 298 predate the 2026-07-22 feature entirely,
141 failed with `preview_unavailable` (root-caused below), 2 with
`no_executable_price`. Of the 145 that did measure: 70% were adverse
(real fill worse than booked), median gap ~1.9% / ~$0.12 per trade, but
real tail risk — one thin market (`will-lebron-sign-his-next-nba-contract`)
would have cost $890 more than booked, an 188% shortfall.

**Mechanism — three changes, in `bot.py`/`polymarket_simulator.py`:**

1. **Root-caused the 141 `preview_unavailable` failures**: Gamma's default
   `/markets?slug=` listing excludes already-resolved markets (confirmed
   live: the same slug 404s without `&closed=true`, returns data with it).
   `polymarket_simulator.fetch_market_info` now retries once with
   `closed=true` before concluding a slug is genuinely unknown. This was
   the dominant failure mode, not a network flake — fixing it is most of
   what "100% coverage" required, since the function was already called
   unconditionally on every buy.
2. **BUY side wired**: `_execute_buy`'s paper branch now checks
   `shortfall.get("shortfall_status") == "ok"` — when true, `our_shares =
   trade_usd / executable_price` and `actual_cost_usd = trade_usd +
   trading_fee_usd + network_fee_usd` (all-in real cost, not just slippage),
   both feeding into the ledger the same way `fill_price` already does on
   the LIVE_MODE path. When the book genuinely can't be read (thin/closed
   market, transient fetch failure), falls back to the source price exactly
   as before — that fallback stays honestly flagged per-trade via
   `shortfall_status` in the logged event, never silently masquerading as a
   verified fill.
3. **SELL side wired identically**: realized `pnl_usd` is booked at the
   sell, so leaving the exit unwired would have left PnL exactly as
   optimistic as the entry was. `proceeds_usd = shares_closed *
   effective_price - (trading_fee_usd + network_fee_usd)` when measurable,
   same source-price fallback otherwise.

**What did NOT change:** LIVE_MODE's own execution path (`fill_price`
extraction, `require_filled()`) — untouched, out of scope, already grounded
in a real fill. Historical paper trades before this change are not
retroactively corrected (the order book at that past moment can't be
reconstructed) — this only grounds trades going forward.

**Tests:** `TestExecuteBuyExtraction` gained 2 (`ok` shortfall wires real
price+fees into the ledger; unmeasurable shortfall falls back to source
price unchanged), new `TestProcessTradeSellShortfallWiring` (2, same two
cases on the exit side), `test_polymarket_simulator.py` gained 1
(`closed=true` retry resolves a market the plain lookup misses). 293
Python tests passing (was 288).

**Why it exists:** Joey called this "a crucial finding — we cannot base our
quantitative edge on optimistic assumptions that don't reflect real order
book depth" and asked for both the coverage fix and the accounting wiring
in the same turn.

## 33. Entry-side (BUY) Marketable Limit Orders — a live-edge-coupled slippage ceiling, not a fixed one (2026-07-26)

**Challenge:** Joey asked for a "Slippage Ceiling" derived from the real
shortfall data (Rule 32's 145 order-book-measured buys) and Marketable
Limit Orders (IOC/FOK) enforcing it — motivated directly by the $890
LeBron-market blowout that Rule 32 surfaced. She explicitly invited
pushback ("evidence should be based on calculus").

**Why a single fixed number from that distribution doesn't work:** the
real percentiles are p50=3.0%, p75=27.8%, p90=100%, p95=170%, p99=811% —
heavily fat-tailed because the data mixes wildly different liquidity
regimes (a handful of illiquid one-off markets vs. the liquid bulk). No
single constant is both tight enough to block the tail and loose enough to
not constantly reject normal trades.

**Mechanism, agreed with Joey before building:**
1. **`bullpen polymarket limit-buy --expiration fak`** is a genuine
   Fill-And-Kill (confirmed live via `--help`: *"fills as much as possible
   immediately, rest cancelled"*) — different from the existing `buy
   --max-price`, which rejects the whole order rather than partial-filling.
   This changes the tradeoff: with FAK, a TIGHT ceiling is nearly costless
   (still captures partial size on a good copy) while a LOOSE one directly
   exposes the exact blowouts Rule 32 found. Verified against the real CLI
   in `--preview` mode (no funds moved) — command syntax confirmed correct,
   `shares` present in the real response schema.
2. **`bot.compute_entry_slippage_ceiling_pct(live_edge_pct)`**:
   `clamp(SLIPPAGE_PROTECTION_FRACTION * live_edge_pct, floor=SLIPPAGE_TOLERANCE,
   cap=ENTRY_SLIPPAGE_CEILING_CAP_PCT)` — reuses the exit side's existing
   `SLIPPAGE_PROTECTION_FRACTION` (0.30) and `db.compute_live_edge_pct()`
   rather than inventing new machinery: paying more in entry slippage than
   the strategy's own realized edge is a guaranteed loser regardless of
   resolution outcome. `floor=5%` (never less protective than the
   pre-existing static tolerance), `cap=30%` (~p90 of the non-outlier bulk,
   114 of 145 trades) so a strong live edge can't license an absurd
   ceiling.
3. **`_execute_buy`**: `config.ENABLE_ENTRY_SLIPPAGE_CEILING_FAK` (opt-in,
   default OFF) forks the LIVE_MODE buy path — when on, computes
   `ceiling_price = price * (1 + ceiling_pct)`, requests
   `trade_usd / ceiling_price` shares (worst-case spend capped at exactly
   `trade_usd`, never more), and books the ledger from
   `extract_filled_shares()`/`extract_fill_price()` — critically, the
   ACTUAL filled share count when the response reports one, not an
   assumed full-budget fill, since FAK can legitimately partial-fill. Falls
   back to the pre-existing full-budget assumption (flagged via
   `fill_accounting`) only when the response doesn't expose a share count —
   a pre-existing limitation inherited from the plain market-buy path, not
   a new one.

**A hard blocker found while verifying readiness to go live, unrelated to
any of this code:** `bullpen polymarket preflight` fails —
`POLYMARKET_WALLET_SELECTION_UNAVAILABLE`, `terminal: true`,
`retryable: false`. Bullpen's own account-level wallet routing is broken
(`wallet_type=3` unsupported for the write path) and its own output
explicitly says not to retry any money-moving command until Bullpen
support confirms the selected wallet route. **`LIVE_MODE` was NOT
flipped** — going live right now would just fail this same way, and
retrying against a `terminal`/non-retryable error is exactly what Bullpen's
own guidance says not to do.

**Tests:** `TestComputeEntrySlippageCeilingPct` (6), `TestExtractFilledShares`
(5, `bullpen_client.py`), `TestExecuteBuyFakIntegration` (5 — ceiling-price
command construction, partial-fill books only what filled, full fill,
shares-unknown fallback flagged, a fully-killed order raises rather than
silently recording a position). **311 Python tests passing (was 293).**

## 34. Wallet-address casing fragmentation — a live gap in the per-wallet exposure cap (2026-07-26)

**Challenge:** while pulling per-wallet EV numbers for Joey (a routine "how
are our wallets performing" check), 7 of 20 tracked wallets showed up
TWICE with wildly different stats — the same real wallet split across an
EIP-55 checksummed address string and its lowercase equivalent, each
accumulating a separate, incomplete slice of that wallet's real trade
history. Root cause: `bot.position_key()` built position dict keys from
whatever casing a given detection source reported for `trader` (a
checksummed address from the polling feed vs. raw lowercase hex from an
on-chain event, for the same real wallet), and `risk_manager.wallet_exposure_usd()`
— the function enforcing Rule 26's $50-per-wallet cap — compared addresses
with plain `==`, explicitly documented on the (false) assumption that
casing stays consistent per wallet.

**This was not cosmetic — it was live.** `fed-qmg-core`, actively `track`-
status and eligible for new buys, had **$82.60 in true open exposure**
across 16 positions split across both casings — 65% over the $50 cap —
and `wallet_exposure_usd()` had never once seen the combined total, only
ever whichever casing a given check happened to query.

**Mechanism:**
1. `bot.position_key(trader, market_slug, outcome)` now lowercases
   `trader` before building the key — the single choke point every
   position/source-position/source-cost-basis key in `bot.py` already
   flowed through (confirmed by grep before touching anything: only two
   call sites, both funnel through this helper).
2. `risk_manager.wallet_exposure_usd()` now compares both sides
   lowercased, defensively, rather than relying on caller/key discipline
   alone.
3. `bot.find_cross_trader_position()` (Rule 3, duplicate-exposure guard)
   given the same defensive fix — a stale differently-cased key could
   otherwise be misread as a genuinely different trader.
4. `config.TRACKED_TRADERS`'s own key casing was deliberately left
   untouched — confirmed live that `trader` is sourced from each trade's
   own feed data, not from this dict's key casing, so normalizing it
   wouldn't have fixed anything and would have been pure diff noise on a
   live-trading config file.

**Data migration** (one-time, run with `bot.py` stopped — see the safety
note below): `paper_trade.wallet_address`, `bot_event_log.trader_address`,
`decision_journal.wallet_address` lowercased. Verified beforehand that zero
open positions shared a `(wallet_lower, market_slug, outcome)` key across
both casings (so a plain `UPDATE ... SET wallet_address = LOWER(...)`
never needed to merge two rows into one). Row counts and dollar sums
(`SUM(cost_basis_usd)` on open, `SUM(realized_pnl_usd)` on closed)
verified byte-for-byte identical before/after — this migration only
rewrites an address string, never touches a shares/cost/pnl column.

**Why `bot.py` had to be stopped first, not just have its code patched
live:** `db.save_state()`'s diff-and-close fail-safe treats any currently-
open DB row whose `(wallet, market, outcome)` key doesn't appear in the
in-memory `positions` dict as `reconciled_missing_from_state` and closes
it. A running old-code process holds positions keyed with the OLD casing;
migrating the DB out from under it would make its next save fail to match
its own (stale-cased) keys against the freshly-lowercased DB rows,
silently closing real open positions that never actually closed. Correct
order: stop bot.py -> fix the code -> migrate the DB -> restart. Verified
after restart: exactly 104 open positions before and after (zero closed
via the fail-safe), zero errors in the fresh startup log.

**Tests:** `TestPositionKeyCasing` (3), `TestFindCrossTraderPositionCasing`
(2), `TestExposure::test_wallet_exposure_matches_regardless_of_casing` (1,
`test_risk_manager.py`). **317 Python tests passing (was 311).**

**Why it exists:** Joey asked for wallet-performance-adjustment
recommendations; the numbers backing that request turned out to be
unreliable for 7 wallets until this shipped, so the fix took priority over
the analysis it was blocking.

## 35. First mute review, and a found gap: mutes never expire (2026-07-26)

**Challenge:** with clean per-wallet numbers finally available (Rule 34),
Joey asked directly: of the 20 tracked wallets, how many are actually
copying right now, not just nominally tracked? Answer: only 12 of 20 —
8 were circuit-breaker-muted (Rule 25's `MUTE_CONSECUTIVE_LOSS_STREAK = 3`),
and `check_circuit_breaker()`'s mute is a ONE-WAY LATCH
(`if key in muted_traders: return` before ever reassessing) — there is no
`reset_kill_switch.py`-equivalent for a per-wallet mute, and no time-based
or performance-based auto-recovery. Joey's own framing: *"a permanent mute
without an auto-recovery mechanism will eventually drain our active pool
to zero due to normal variance."*

**Individual review of the 8, evidence-based (full trade history pulled
per wallet before deciding, not a blanket call):**

- **`strict-4`/`strict-10` — REINSTATED, and a real process gap found**:
  both were muted **2026-07-16**, 10 days before this session's Rule 31
  `strict-10` re-add. Re-adding a wallet to `TRACKED_TRADERS` does NOT
  clear an existing `wallet_profile.circuit_breaker_muted` flag — two
  independent mechanisms. So `strict-10` was never actually trading since
  its re-add, despite being reported as reinstated at the time. Caught
  only now, via this explicit review.
- **`crypto-specialist-1` — REINSTATED**: muted early in its copy history
  (a real 3-loss streak), but its current rolling 10-trade window (post-
  streak) is 10/10 wins — the mute never got reassessed after the streak
  ended, exactly the "one-way latch" gap Joey flagged.
- **`generalist-weak-1`, `strict-2`, `geo-denizz` — KICKED**: each shows a
  consistent, explained negative pattern (3 of 4-6 closed trades were
  total -100% losses), not noise. `generalist-weak-1` was flagged
  "weakest of this batch" at original add time — this confirms rather
  than contradicts that call.
- **`yield-farmer-1` — left muted**: only 1 closed trade on record, too
  thin to judge either way.
- **`fed-warren-buffett` — left muted, NOT resolved**: a genuine,
  unexplained discrepancy — its only 2 closed `paper_trade` rows are both
  wins (+$11.60 net), but `wallet_profile.recent_results_json` shows a
  4-loss streak that doesn't correspond to any closed row found, and it
  has 5 positions stuck at `status='open'` with no `closed_at` (~$46 cost
  basis). Deliberately NOT touched until traced down — reinstating or
  dropping on data I don't understand yet would be worse than leaving it
  muted a while longer.

**Execution, done with `bot.py` stopped first** (same restart-safety
reasoning as Rule 34 — `save_state()` would otherwise re-persist a
currently-running process's stale in-memory `muted_traders` state over any
direct DB clear): `config.TRACKED_TRADERS` edited (3 removed, comments
added explaining each; 17 wallets remain), `wallet_profile.circuit_breaker_muted`
directly cleared for the 3 reinstated wallets (`consecutive_losses` was
already 0 for all three at clear time — confirmed before deciding not to
also reset it — so no immediate re-mute risk from stale counters).
Verified after restart: `config.TRACKED_TRADERS` count 17, zero errors,
15 of 17 actively copying (2 — `fed-warren-buffett`, `yield-farmer-1` —
correctly still muted).

**Roadmap agreed with Joey, not yet built** (a real, multi-piece
architecture, deliberately scoped as a separate follow-up rather than
bundled into this review):
1. Switch `config.TRACKED_TRADERS_SOURCE` from `"static"` to `"db"` —
   `db.get_tracked_traders()`'s db-backed path already exists
   (`wallet_profile.status='track' AND circuit_breaker_muted=0`,
   `config.MIN_TRACKED_TRADERS` as a fail-loud floor) and was simply never
   turned on. Makes wallet swaps a DB write, not a `config.py` edit +
   restart.
2. Evidence-based rehabilitation instead of a blind timer: keep observing
   a muted wallet's real trades (the source feed already does — muting
   only blocks OUR copy, confirmed by reading `process_trade`'s mute
   check) and simulate what a copy would have earned, logged under a new
   `paper_trade.strategy = 'shadow_rehab'` value (the schema already
   supports a second non-live strategy tag via
   `blind_leaderboard_benchmark`, so this never touches real
   exposure/risk calculations). Auto-clear the mute after 3 consecutive
   shadow wins — deliberately symmetric to the existing
   `MUTE_CONSECUTIVE_LOSS_STREAK = 3`, not a new invented number.
3. A periodic job to keep the active (tracked, unmuted) pool near 20:
   when it drops below target, propose (not silently auto-promote) the
   next-best untried candidate from `wallet_profile`, already vetted by
   the existing discovery pipeline's safety rules (Rules 20/21/23/24/27).

**Tests:** no new tests this round — this entry is a config/data change
(wallet membership + mute-flag clears), not new code logic; the casing
fix's own tests (Rule 34) already cover the mechanism these edits rely on.
**317 Python tests passing, unchanged.**

## 36. fed-warren-buffett bookkeeping traced; VIP exposure cap; EV-based circuit breaker (2026-07-26)

**Challenge:** three follow-ups from Rule 35's review. (1) `fed-warren-buffett`
was left muted pending investigation — its only 2 closed trades were both
wins, yet `wallet_profile.recent_results_json` showed a 4-loss streak.
(2) Joey wanted a manually-curated higher exposure cap for proven, high-
volume wallets (`strict-7`, `political-whale-1`) rather than the flat $50.
(3) Joey asked to replace the 3-loss-streak mute trigger with an EV/win-
loss-ratio monitor, inviting pushback if unwarranted.

**fed-warren-buffett traced — a real, general bug, not lost data.** Its
"4 losses" are real `paper_sell` events, but dust: fractions of a cent
(`-$0.0184`, `-$0.0007`, `-$0.0000065`, `-$0.0029`), generated as the
wallet unwinds its own position in many tiny increments that our
proportional copy-sell correctly mirrors. `check_circuit_breaker()`'s old
`is_win = pnl_usd > 0` had no minimum economically-meaningful trade size —
a `-$0.000006` "loss" counted identically to a real $50 one. Not specific
to this wallet: any wallet that sells down in small increments could
trigger a false mute this way. Reinstated (mute cleared) alongside
`strict-7` (per Joey's direct instruction — verified first that it really
was muted, 17 minutes prior, a genuine fresh 3-loss streak, not a
mis-report).

**VIP exposure cap** (`risk_manager.wallet_exposure_cap_usd()`):
`config.VIP_WALLET_EXPOSURE_CAP_USD`, a manually-curated address-keyed
override of `MAX_WALLET_EXPOSURE_USD`, checked ahead of the flat cap in
`check_buy`. `strict-7` -> $150 (31 trades, +44.6% EV, +$56.83, highest-
volume strong performer), `political-whale-1` -> $100 (4 trades, +183.9%
EV, smaller sample so a more conservative cap). `MAX_TOTAL_EXPOSURE_USD`
(the portfolio ceiling) still applies on top regardless — this only
raises the per-wallet sub-limit. Deliberately manual, not scored/automatic:
with the EV circuit breaker and Shadow Rehab still new, there isn't yet a
trustworthy automatic signal to key a bigger allocation off.

**EV-based circuit breaker — agreed with pushback on scope, not on the
core idea.** The redesign is well-justified: confirmed live, twice
(`strict-7`, `crypto-specialist-1`), that the old 3-loss-streak trigger
false-positives on genuinely good wallets. Replaced with a one-sample
t-test (`bot.compute_wallet_ev_t_statistic()`) on the wallet's own recent
REAL (non-dust) per-dollar-staked returns — mirrors `should_skip_category()`'s
existing `pnl_t_stat` approach exactly (same `CATEGORY_SKIP_Z_CRITICAL`
critical value, reused for consistency rather than inventing a second
threshold for the identical kind of decision), rather than a raw EV-ratio
cutoff with the same "arbitrary threshold" problem the old design had.
Dust filter (`config.MUTE_MIN_TRADE_COST_USD = 1.0`) added directly from
the `fed-warren-buffett` finding above.

**The honest limitation, found by actually checking rather than assuming**:
I initially claimed this design would ALSO catch `geo-anon-3`'s pattern
(83% win rate, -11% EV from rare catastrophic losses) — verifying against
its real 12-trade history before shipping, the t-statistic is **-0.92**,
nowhere near the -1.645 critical value. A t-test needs a *consistent*
edge; one huge outlier inflates variance enough that a handful of samples
can't distinguish a fat-tailed/rare-catastrophic-loss wallet from noise.
That claim was corrected in the code comments and tests before merging,
not left overstated. `geo-anon-3`-shaped wallets still need manual review
(exactly how it was actually caught) — Shadow Rehab, still on the roadmap,
doesn't change this either; it only affects what happens to an
*already-muted* wallet, not detection of a wallet like `geo-anon-3` that
was never muted in the first place.

**Persistence, no schema migration needed**: the DB column stays
`recent_results_json`/`consecutive_losses` unchanged; what's stored inside
the JSON list changed from booleans (win/loss) to floats (the actual
per-trade return), and the in-memory dict key renamed `recent_results` ->
`recent_returns` for clarity. `consecutive_losses` is no longer computed
by the new trigger — always written as 0 now, column kept but vestigial.

**Tests:** `TestVipWalletExposureCap` (5, `test_risk_manager.py`),
`TestComputeWalletEvTStatistic` (5), `TestCheckCircuitBreakerEvBased` (8 —
dust filter, no false-positive on a short streak within a strong history,
genuine statistical significance mutes, the honest geo-anon-3 limitation,
already-muted wallets don't get a second mute reason but their return
history keeps updating, below-minimum-samples never mutes). **336 Python
tests passing (was 317).**

**Not yet built** (unchanged from Rule 35's roadmap): Shadow Rehab (keep
observing a muted wallet's real trades and simulate a copy under
`paper_trade.strategy='shadow_rehab'`, auto-clear after 3 consecutive
shadow wins) and the dynamic pool-refill job. Both still agreed, not yet
started.

## 37. Shadow Rehab and the pool-refill proposal script (2026-07-27)

**Challenge:** the two roadmap items Rule 35 deferred. A mute is a
one-way latch (`check_circuit_breaker()`'s `if key in muted_traders:
return` never re-evaluates), and once muted, a wallet generates no new
real copy-trades — the exact evidence that would ever justify lifting the
mute can never accumulate again. Separately, `TRACKED_TRADERS` sits at 17
(after Rule 34's drops), and Joey's stated goal is an ACTIVE pool near 20.

**Design note, agreed adjustment from the original roadmap wording:**
Rule 35's roadmap said "auto-clear after 3 consecutive shadow wins,"
written when the mute trigger itself was still the old streak-based
design. Since Rule 36 replaced that with a t-test, the REHAB trigger was
built symmetrically too — the same t-test machinery, run in reverse
(significantly POSITIVE edge, not a raw win streak) — rather than leaving
the mute and rehab sides using two different, inconsistent statistical
standards.

**Shadow Rehab mechanism:**
1. A muted wallet's real trades are still visible (a mute blocks only OUR
   copy, not detection — `source_positions`/`source_cost_basis` tracking
   already ran unconditionally before this). `process_trade()`'s mute
   check now ALSO calls `bot._execute_shadow_buy()` before returning —
   books a hypothetical copy at a fixed `config.SHADOW_REHAB_TRADE_USD`
   ($5, not tiered wallet-score sizing: the rehab decision is driven by
   RETURN ratios, which are scale-invariant) into an isolated
   `shadow_positions` dict, using the same `measure_paper_shortfall()`
   fill-fidelity Rule 32 established for real paper trades.
2. On the corresponding SELL, the SAME `fraction_sold` (a property of the
   whale's own trade) closes/reduces the shadow position too — this runs
   UNCONDITIONALLY, not gated on current mute status, so a wallet
   reinstated mid-lifecycle doesn't leave an orphaned shadow position.
3. Persisted to `paper_trade.strategy="shadow_rehab"` — confirmed by grep
   before building that every real risk/exposure calculation
   (`total_exposure_usd`, `wallet_exposure_usd`, `load_state()`'s
   positions) filters on `strategy="bot_filtered"` exclusively, so a
   wrong shadow number can only affect a future rehab decision, never
   real capital.
4. `bot.sweep_shadow_rehab()`, run every poll cycle: for each muted
   wallet, once `config.MUTE_EV_MIN_SAMPLES` real shadow closes exist,
   runs `compute_wallet_ev_t_statistic()` on them; a t-stat
   `>= config.CATEGORY_SKIP_Z_CRITICAL` (positive-side, the same critical
   value Rule 36 uses negative-side for muting) clears the mute —
   deletes the entry from the in-memory `muted_traders` dict, which the
   very next `persist()`/`save_state()` call naturally writes through to
   `wallet_profile.circuit_breaker_muted=0` via the exact mechanism that
   originally recorded the mute. No separate DB write needed.

**`ENABLE_SHADOW_REHAB` defaults `True`**, unlike Priority 3/4's
execution-affecting flags: shadow trades never touch real capital or risk
state, so the worst case of a wrong rehab decision is "we resume copying
a wallet a human could also have manually reinstated" — the same risk
already accepted for every manual reinstate this session.

**Deliberate simplifications, stated plainly, not hidden**:
`_execute_shadow_buy()` skips `MAX_BUYS_PER_TRADER_OUTCOME` and the
cross-trader duplicate-position guard — both manage OUR portfolio's real
capital allocation, which doesn't apply to an isolated simulation;
multiple shadow buys into the same market simply average up freely.

**Pool-refill proposal script** (`propose_pool_refill.py`): reads
`bot_risk_state["tracked_traders"]` (the real, live list bot.py publishes
at every startup — the same source the dashboard now reads, Rule 36) and
`wallet_profile.circuit_breaker_muted` to compute the true active count,
compares against `config.TARGET_ACTIVE_TRADER_COUNT` (20), and — if
below — proposes the next-best untried candidate(s) from `wallet_profile`
(`composite_score >= config.POOL_REFILL_MIN_COMPOSITE_SCORE`, reusing the
discovery pipeline's own existing 0.2 default rather than inventing a
second bar). Excludes every wallet EVER tracked, not just currently
tracked — a previously-kicked wallet is never re-proposed on the strength
of the same record that got it kicked (`db.get_ever_tracked_wallets()`,
union of currently-tracked and any wallet with a
`strategy='bot_filtered'` `paper_trade` row).

**Deliberately proposes only, never auto-promotes** — matching every
`TRACKED_TRADERS` change this session, which has always gone through
explicit human review before being made. Run live against the real DB
before considering this done: correctly reported 16/17 active against
the 20 target and proposed 4 real, previously-untried candidates.

**Tests:** `TestExecuteShadowBuy` (3), `TestProcessTradeShadowRehabWiring`
(5), `TestSweepShadowRehab` (5, `test_bot_risk_checks.py`) —
`TestShadowPositionPersistence` (5), `TestGetShadowRehabReturns` (5),
`TestMaybeClosePaperTradeStrategyScoping` (2), `TestGetMutedWallets` (2),
`TestGetEverTrackedWallets` (2), `TestGetPoolRefillCandidates` (5,
`test_db_rule37.py`, a real temporary SQLite DB). **370 Python tests
passing (was 336).**

## 38. Full audit: `bot_source_position` had the same casing bug Rule 34 fixed elsewhere (2026-07-27)

**Challenge:** Joey asked for a full one-time bug audit across everything,
not just the newest code. Runtime health, the test suite, and static
review (bare excepts, mutable defaults, SQL injection, resource leaks,
config duplicate-constant checks, TS build/lint) all came back clean. One
real, live bug was found.

**`bot_source_position` was still fragmenting by address casing, hours
after Rule 34's fix.** Confirmed live: 150 (wallet, market, outcome)
triples had BOTH a stale mixed-case row and a fresh lowercase row
simultaneously — `source_positions`/`source_cost_basis` held two
inconsistent views of the same whale's real holdings, feeding directly
into `fraction_sold`'s accuracy on every SELL for those wallets.

**Why Rule 34 missed this**: `bot_source_position` is fully replaced
(`DELETE` + re-insert) on every `save_state()` call, which correctly
looked "self-healing" — new writes already flowed through the fixed
`position_key()`. The actual gap was one level up: `load_state()` read
`bot_source_position.key` back into memory verbatim, with no lowercase
normalization. A stale pre-fix row just kept getting loaded and
re-persisted forever, and any fresh post-fix trade for that same wallet
created a SEPARATE lowercase entry alongside it rather than replacing it
— the "fully replaced" property was true of the SAVE side, not the LOAD
side, and only the load side needed the fix.

**Fix**: `load_state()` now lowercases the trader portion of the key on
read, and — since duplicates already existed in the DB, unlike Rule 34's
`paper_trade` migration where zero existed — SUMS `shares`/`cost_basis_usd`
across any two rows that fold to the same lowercase key, rather than one
silently overwriting the other and discarding real trade history. No
separate SQL migration needed: the very next `save_state()` call
naturally writes the corrected, merged, lowercase-only dict back,
overwriting the stale duplicates for good (same restart-required
mechanics as Rule 34 — verified via a real restart: 1642 rows, 0
mixed-case, 0 duplicate groups, 0 errors afterward).

**One other finding, real but currently inert, not fixed**: the FAK
entry-side integration (Rule 33) doesn't check bullpen's own 5-CLOB-share
minimum on `limit-buy` before submitting — a $3 trade on a market priced
above ~$0.60 would request under 5 shares and get rejected. Not fixed
because `config.ENABLE_ENTRY_SLIPPAGE_CEILING_FAK` is still `False` (this
path has never executed against anything, paper or live) — flagged for
whenever that flag is turned on, not urgent before then.

**Tests:** `test_duplicate_casing_rows_are_merged_on_load_not_overwritten`
(new), plus 2 existing `TestSourceCostBasisRoundTrip` tests updated for
the now-lowercased round-trip key (`test_db_pending_execution.py`).
**371 Python tests passing (was 370).**

## 39. Pool-refill script had no bot detection — fixed, and the discovery pool turned out to be mostly bots (2026-07-27)

**Challenge:** reviewing Rule 37's 4 proposed candidates
(gloriafoster/pizzaallosqualo/pizzabillgates/Asperatus) with the same
rigor as every other wallet decision this session — pulling real
`bullpen polymarket wallet-stats`, not just the DB's `wallet_profile`
columns — found `is_likely_bot: true` for all 4. `wallet_profile.is_likely_bot`
itself was `NULL` for every one of them: freshly-discovered candidates
never get that field populated, so `get_pool_refill_candidates()`'s
`composite_score` filter had no way to catch this on its own.

**Fix**: `propose_pool_refill.py` now calls `bullpen polymarket wallet-stats`
per candidate before ever proposing it. A confirmed bot is excluded and
listed separately (never silently dropped with no explanation). An
inconclusive result (CLI/network failure) is NOT treated as "safe" —
the candidate is still surfaced, but flagged `UNVERIFIED`, so a human
knows to check manually rather than trusting an unchecked pass.
Fetches `config.POOL_REFILL_CANDIDATE_FETCH_MULTIPLIER` (4x the actual
gap) candidates up front as a buffer against rejections — a judgment
call, not fit to a real bot-rate estimate (one prior data point, 0-for-4,
isn't a sample).

**A real bug found and fixed on this script's own first live run**:
`run_bullpen_json()` already appends `--output json` itself; the initial
bot-check implementation passed it again, and bullpen rejected the
duplicate flag on every single call. The `except Exception: return None`
fallback silently converted every one of those failures into "inconclusive"
rather than surfacing the bug — caught only by noticing all 4 candidates
came back `UNVERIFIED` instead of the expected confirmed-bot result,
not by the script itself.

**After the actual fix, re-running against the real DB found something
much bigger than the original 4**: **16 of 16** screened candidates in
the current discovery pool are confirmed bots — including
`0xc21ea96be762bb55041529af6e386e7c53b80215` ("JustCrazy"), the exact
floor-sniping wallet Rule 27's TCA entry-price floor was originally built
around as the cautionary example. It was sitting in the candidate pool
with a high enough `composite_score` to have been eligible for proposal.
**No refill is currently possible from the existing scanned pool** — a
fresh `pnpm scan:leaderboard && pnpm scan:wallets` run is needed before
this script can find any real (non-bot) candidates. Not run as part of
this fix — a genuinely separate, longer-running action, not bundled in.

**Tests:** none new — `propose_pool_refill.py` is a live-integration
script (same category as `reset_kill_switch.py`), verified by actually
running it against the real DB and real `bullpen` CLI both before and
after the fix, not unit-tested. `db.py`'s own Rule 37 functions this
script calls are unchanged and already covered by `test_db_rule37.py`.
**371 Python tests passing, unchanged.**

## 40. Being algorithmic isn't disqualifying — being unreplicable is (2026-07-27)

**Challenge:** Joey's correction to Rule 39's premise: `is_likely_bot`
conflates "automated" with "edge we structurally can't replicate." A
directional algo trader is fine to copy; the real disqualifier is a
wallet whose profit comes from liquidity rewards, market-making rebates,
or micro-arbitrage that copy-lag and taker fees make unreplicable —
`is_likely_bot` can be wrong in both directions on that question.

**Verified her framing directly against real trade data before changing
anything** (not just agreed in the abstract): pulled `bullpen polymarket
activity` for gloriafoster and Asperatus (2 of the 4 original
candidates). Both showed the SAME concrete, checkable pattern: repeated
SELLs at the identical near-zero price in the same market, sizes varying
wildly (0.06/78.0/0.08 shares) minutes apart — e.g. 29 fills at the exact
same `(will-jasmine-crockett-win-the-2028-democratic-presidential-nomination, 0.007)`
quote. That's liquidity provision on long-shot "will X win a 2028
nomination" markets, not directional conviction — the disqualifier
Joey named, confirmed with real evidence, not the blunt bot flag.

**Fix: replaced auto-filtering with evidence-gathering.**
`propose_pool_refill.py` no longer hard-rejects on `is_likely_bot=true`.
Every candidate up to the gap gets BOTH signals pulled and printed:
`is_likely_bot` (kept, labeled as one input, not a verdict) and a real
activity-pattern summary (`fetch_recent_trades()` +
`summarize_liquidity_farming_signal()`) — % of trades at extreme prices
(<0.05 or >0.95), buy/sell balance, and the single most-repeated
(market, price) quote in the sample. Nothing is silently dropped; a
warning is printed inline when the evidence is strong, but the actual
accept/reject call is left to the human reviewing the output, same
"propose only" philosophy as the rest of this script.

**Re-run against the real DB and bullpen CLI after the fix — same
verdict on all 4 original candidates, now with transparent, checkable
evidence instead of a black-box flag:**

| Candidate | Extreme price % | Most-repeated quote |
|---|---|---|
| gloriafoster | 100% | 29x at 0.007 |
| pizzaallosqualo | 73.5% | 13x at 0.05 |
| pizzabillgates | 100% (98% SELL) | 34x at 0.007 |
| Asperatus | 100% (100% SELL) | 18x at 0.005 |

All four show the identical signature. The reject decision doesn't
change, but the reasoning is now evidenced rather than delegated to a
label — and the NEXT candidate that's genuinely algorithmic-but-
directional (more varied prices, less quote repetition, meaningful
buy-side activity) won't get auto-killed by `is_likely_bot=true` the way
this version would have.

**Tests:** none new — same live-integration-script category as Rule 39,
verified by re-running against the real DB/CLI, not unit-tested.
**371 Python tests passing, unchanged.**

## 41. Liquidity-farming hard gate in the wallet scorer, and "zombie position" dump exit (2026-07-27)

**Challenge (scorer):** Rule 40 fixed `propose_pool_refill.py` to surface
liquidity-farming evidence instead of auto-rejecting on `is_likely_bot` —
but that only helps a human reviewing the refill script's output. The
`compositeScore` formula the scorer actually ranks candidates by (30% ROI +
20% consistency + 30% win rate + 20% copyability) doesn't penalize
liquidity-farming at all — selling long-shot NO tokens at scale genuinely
produces a smooth, high ROI and win rate on paper. Confirmed live: after a
fresh `scan:leaderboard` run, the same 4 known liquidity-farming wallets
(gloriafoster, pizzaallosqualo, pizzabillgates, Asperatus) still outranked
the 2 genuinely new candidates by compositeScore (0.639-0.735 vs.
0.552-0.565) — meaning `propose_pool_refill.py`'s top-N approach would keep
resurfacing them regardless of scan freshness, until a hard gate runs
upstream in the scorer itself, independent of compositeScore.

**Mechanism (scorer):** a fourth hard gate in `scoreWallets.ts`
(`checkLiquidityFarmingGate`, rule_set v4), mirroring the existing
toxic-flow/recency gates' exact `{status: "ignore", reason} | null` shape
and slotting into `finalizeAndWrite`'s same force-ignore-and-write-
immediately pipeline. A new pass-2 fetch (`fetchRecentTrades`, genuinely
new — `bullpen polymarket activity --address`, not reusable from the
existing `wallet-stats --section activity` timestamps-only call) samples up
to 50 recent trades; `computeLiquidityFarmingSignal` computes what fraction
sit at an extreme price (<0.05 or >0.95) and how many times the single
most-repeated (market, price) pair recurs. Gated on BOTH conditions
together (≥50% extreme price AND ≥5x repeated quote, sample ≥20) — not
either alone, since a wallet legitimately buying several DIFFERENT
longshots isn't farming any single one. Fails open on missing/thin data
(fetch failure, or sample below the minimum), same "unknown isn't confirmed
toxic" reasoning as the toxic-flow gate's `insufficientConsistencyData`
exemption — confirmed necessary live: 6 of 191 pass-2 candidates hit a
`bullpen` CLI/server version mismatch on this exact call during the
verification run and correctly passed through un-gated rather than erroring
the whole scan.

**Being algorithmic still isn't the disqualifier** (Rule 40's framing
carried through unchanged) — this gate looks at the trade PATTERN
(extreme-price concentration + quote repetition), never at `is_likely_bot`
itself.

**Tests:** 56 new/updated unit tests in `scoreWallets.test.ts` (gate
boundary cases, both-conditions-required cases, the missing-data fail-open
case, `computeLiquidityFarmingSignal`'s own math) — 195 TS tests passing
total. 371 Python tests unaffected (TS-only change).

---

**Challenge (zombie positions):** investigating why the live bot "looked
frozen" traced to `MAX_BOOK_AGE_SECONDS=15` (Rule 16, order-book staleness
check) correctly, routinely refusing individual
TTP price reads on thin markets — a real but self-correcting ~10-30%
per-sweep miss rate, confirmed via two live passes over the same 40 open
positions a minute apart (12/40 then 4/40 stale, mostly different markets
each time, no client-side rate-limiting/slowdown found). Decision: leave
`MAX_BOOK_AGE_SECONDS` untouched — protecting against stale/slippage-prone
fills is the priority, and the miss rate is fine on its own. But 3 of 40
positions failed BOTH passes identically (`will-minnesota-timberwolves-
win-the-2027-nba-finals`, `will-tom-brady-win-the-2028-republican-
presidential-nomination`, `will-david-malukas-win-the-2026-ntt-indycar-
series`) — genuinely dead order books with no realistic path to ever
passing the staleness check again, plus a separate class entirely (3
`israel-x-iran-ceasefire-...` positions returning "no market found for
slug," i.e. delisted/renamed, not merely stale) — both classes trap capital
indefinitely with no fix from waiting.

**Mechanism (zombie positions):** a deliberately SEPARATE escape hatch,
never a loosening of `MAX_BOOK_AGE_SECONDS`/`check_spread_tolerance` for
everything else:
- **Detection** piggybacks on `check_trailing_take_profit`'s existing
  5-minute sweep (zero new network calls) — `pos["last_priced_at"]` is set
  on every successful read. Persisted (new `paper_trade.last_priced_at`
  column) rather than in-memory, since the bot restarts often enough that
  an in-memory 24h clock would rarely fire.
- **`sweep_zombie_positions`** runs on its own 6-hour interval
  (`ZOMBIE_SWEEP_INTERVAL_SECONDS`), separate from and far rarer than the
  TTP sweep. A position with no successful read in
  `ZOMBIE_POSITION_THRESHOLD_SECONDS` (24h) becomes eligible.
- Splits the two failure classes: a market that's real but chronically
  stale gets a forced exit attempt (`get_market_prices(...,
  ignore_staleness=True)`, a new opt-in param threaded through
  `fetch_order_book`/`fetch_order_book_for_outcome`/`get_market_prices`,
  default `False` everywhere else). A market whose lookup itself fails
  (delisted/renamed) gets a distinct, throttled `zombie_position_
  unresolvable` alert instead — no automated exit is possible, retrying a
  doomed sell every 6 hours forever would only spam the log.
- **`close_position_zombie_dump`** is a deliberately SEPARATE closer from
  `close_position_trailing_tp`, not a shared function with a flag: reusing
  the existing one would mean reusing `check_spread_tolerance` too, which
  would very plausibly reject exactly the trade this exists to force
  through (a wide spread is likely why the market went stale in the first
  place). No patient-exit-pegging either. Still not reckless — a real,
  looser price floor (`ZOMBIE_EXIT_MAX_SLIPPAGE = 0.25`, vs. the normal 5%
  `SLIPPAGE_TOLERANCE`), "aggressive," never "sell at any price."
- **`config.ENABLE_ZOMBIE_POSITION_DUMP = False` by default** — detection
  and both log paths (throttled unresolvable alert, and a `zombie_
  position_would_dump` dry-run line while the flag is off) always run
  regardless, so the first rollout is pure observability before any real
  position is force-closed this way.

**Tests:** 13 new unit tests (`test_bot_risk_checks.py`,
`test_polymarket_simulator.py`) covering the threshold boundary, the
missing-`last_priced_at` skip, both dry-run and flag-on dump paths, the
unresolvable throttle counter (set AND clear), the live-mode slippage floor
using `ZOMBIE_EXIT_MAX_SLIPPAGE` not `SLIPPAGE_TOLERANCE`, the skipped
spread check, and a failed sell leaving the position open for retry.
**384 Python tests passing** (371 + 13 new).

## What is intentionally still simple

The current setup is conservative by design — it focuses on avoiding obvious bad fills, bad
liquidity, and weak traders, rather than being a full multi-factor execution engine. Rule 11
(Disciplined Taker) and Rule 6 (portfolio risk manager) are meaningful steps toward one; Rule 12
(Phase 2 scoring) is the planned next step beyond that.

## Roadmap / status of previously "likely future changes"

- Stronger portfolio-level risk controls — **done** (Rule 6), 2026-07-18.
- More advanced maker/taker logic — **in progress**: shortfall tracking is live (Rule 10) as a
  proxy signal; scoring it into the wallet scorer is still pending enough real data. **Real
  maker/taker detection is now possible directly** (2026-07-21, Rule 10 addendum) via Polymarket's
  own confirmed fee formula (`feeRate` of exactly 0 = maker), stronger than the original
  shortfall-based heuristic.
- Historical shortfall/PnL recalculation — **resolved, not needed** (Rule 10 addendum,
  2026-07-22): traced precisely rather than assumed, `compute_shortfall()` works purely on prices,
  which matched exactly between sources.
- `trading_fee`/`network_fee` capture in shortfall measurement — **done** (Rule 10 addendum,
  2026-07-22): `measure_paper_shortfall()` now captures both plus a combined `total_cost_usd`.
- Cutting `bot.py` over from `bullpen tracker feed` to the direct Polymarket Data API — **done**
  (Rule 14, 2026-07-22). Along the way: the validation methodology gap was fixed (shared time
  window via the direct API's `start` param), which surfaced a much bigger finding than expected
  (10 of 20 tracked wallets were never registered with bullpen's own tracker at all — now
  irrelevant, since tracking no longer depends on bullpen's registration list at all). Live-
  verified post-cutover: clean bootstrap using the new feed, zero errors, `strict-1` (the wallet
  that showed the starkest pre-cutover anomaly) confirmed fully visible.
- More explicit scoring rules for entry/exit decisions — **in progress**: the pre-trade slippage
  ceiling is done (Rule 11); the Phase 2 scoring concepts (Rule 12) are logged, not built.
- Optional live-trading rollout, only after review — **still fully pending**;
  `TRACKED_TRADERS_SOURCE` also stays `"static"` until a scored wallet batch is reviewed (Rule 2).
- Not yet started: the weather arbitrage bot (separate, isolated system — see
  `docs/CURRENT_STATE.md`); remaining Next.js dashboard pages (only Overview exists today);
  `reviewOutcomes.ts`/`updateRules.ts` (the score-vs-actual-outcome validation loop that would let
  scoring rules self-tune from real data instead of hand-picked thresholds).
- Confidence-weighted position sizing — **done** (Rule 15, 2026-07-22): `FIXED_TRADE_USD` replaced
  with `compute_trade_size_usd()`, scaling $3-$10/trade by `wallet_profile.composite_score`.
  Researched first (Kelly criterion for prediction markets, copy-trading practitioner sources) —
  see Rule 15 for why this is deliberately NOT literal Kelly. Category-specific wallet scoring
  (a trader's edge in one market category doesn't imply edge in another — flagged by the same
  research pass) remains a real, unaddressed gap, bigger than this change, not yet started.
- Order-book staleness check — **done** (Rule 16, 2026-07-22): `fetch_order_book()` now refuses a
  book older than `MAX_BOOK_AGE_SECONDS` (15s), covering both Rule 7 (TTP) and Rule 10 (paper
  shortfall simulation) at once since both call through this one function.
- Disk-exhaustion hardening — **done** (Rule 17, 2026-07-22): `RotatingFileHandler` (50MB × 5
  backups) for `bot.out.log`/`dashboard.out.log`; age-based `prune_event_log()` (180-day retention,
  daily sweep) for `bot_event_log`, which cannot use a file-rotation handler at all since it's a
  SQLite table, not a file.
- Category-specific wallet scoring — **done** (Rule 18, 2026-07-23): `compute_trade_size_usd()`
  now sizes by the wallet's OWN edge in the specific category being traded (Politics/Sports/
  Crypto/Pop Culture/other), reconstructed from raw trade history (bullpen's wallet-stats has no
  category filter — confirmed by reading its code), falling back to the global composite_score
  when no category-specific signal exists yet.
- Hard skip on statistically significant category harm — **done** (Rule 19, 2026-07-23): a
  one-sample t-test on category PnL (not an arbitrary score cutoff), `should_skip_category()`
  checked before sizing, skips a copy entirely once there's statistically strong evidence of harm.
