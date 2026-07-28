# Polymarket Copy-Trading System — Technical Summary

A systematic copy-trading bot for Polymarket prediction markets: tracks a
curated set of wallets, sizes positions with a risk-adjusted Kelly formula,
and runs its own independent risk-management layer on top of every trade.
Currently paper-trading only (no live capital) — the focus so far has been
getting the sizing math, the risk gates, and the verification discipline
right before that switch ever flips.

Below is a look at the parts I'd most want a second set of eyes on: the
sizing methodology, a couple of investigations that changed the design, and
how the system checks its own work.

## Current state

- 17 wallets tracked, selected from a ~700-wallet scan-and-score pipeline,
  spread across politics, sports, crypto, and cross-category "generalist"
  profiles rather than concentrated in one edge type.
- 382 closed paper trades logged to date.
- 404 Python tests / 239 TypeScript tests passing.
- Paper trading only. `LIVE_MODE` is off; nothing here has traded with
  real funds.

## 1. Position sizing: half-Kelly with empirical-Bayes shrinkage

The original version of this bot sized trades off a blended 0–1 "confidence
score" — intuitive, but not connected to any real edge/odds math, and with
no principled answer to "why this size and not double it."

It's now real fractional Kelly:

```
f*      = p_shrunk − (1 − p_shrunk) / b        # b = net odds implied by market price
f_half  = 0.5 · f*                              # standard fractional-Kelly correction
```

The interesting part is `p_shrunk`. A wallet's raw win rate over 5–10
category-specific trades is statistically indistinguishable from noise —
sizing directly off that number lets a tiny, lucky sample drive a large
bet. So the win rate going into Kelly is shrunk toward the market's own
implied probability first, using an empirical-Bayes / Beta-Binomial
estimator:

```
p_shrunk = (n · win_rate + k · market_price) / (n + k)
```

`k = 25` isn't a guess — it's solved so that a 5-trade sample (this
system's hard minimum) gets discounted to ~17% trust in the observed win
rate, while a 200-trade sample (the threshold cited in the research that
motivated this) is trusted at ~89%. Using the *market price* as the
shrinkage prior rather than a flat 0.5 has a clean side effect: at `n = 0`,
`p_shrunk` collapses to exactly the market price, which makes the Kelly
fraction come out to exactly zero. "No track record, no assumed edge"
falls out of the math instead of needing a special case.

Halving is deliberate and has a real, stated consequence I'd rather surface
than leave for someone to discover: raw Kelly is bounded above by
`p_shrunk ≤ 1`, so half-Kelly can never clamp to the top of the trade-size
range even at a huge sample and a near-certain edge. Under current bounds
(`$3`–`$10`), the practical ceiling is `$6.50`, not `$10` — a direct
consequence of always halving, not a bug to fix.

## 2. A Sharpe-based capital multiplier, kept structurally separate from Kelly

The next ask was: allocate more capital to the best-performing wallets,
using a standard quant formula. The tempting shortcut would have been to
fold that into the Kelly sizing itself — but Kelly's fraction depends on
the market price *at trade time*, which a periodic wallet-scoring job never
has access to. So instead of a new sizing formula, this is a per-wallet
multiplier on the *range* the existing Kelly math operates within:

```
multiplier = 1 + clamp(sharpe / saturation, 0, 1) × (max − 1)
```

with `saturation = 0.35`, `max = 2.0`. One detail worth calling out: the
Sharpe proxy used here is deliberately the *raw, unsaturated* value, not
the version already computed elsewhere in the scorer for a "consistency"
sub-score — that version is clamped at 0.15 for a different purpose, and
reusing it here would have pinned every wallet that merely qualifies for
tracking at the same multiplier, killing the differentiation the feature
exists to produce. The multiplier also never drops below 1.0 — a losing or
unproven wallet gets the unmodified base range; this only ever rewards
documented outperformance, never penalizes on top of what Kelly and the
risk gates already do.

## 3. Case study: catching a false positive in an adversarial-pattern filter

This is the piece I'd most want to walk through, because it's less "here's
a feature" and more "here's how a mistake got caught before it did damage."

**The problem.** A copy-trading system needs to distinguish two things that
look similar in aggregate stats but are economically opposite: a wallet
with genuine directional edge (fine to copy) versus a wallet farming
liquidity-provider rebates or micro-arbitrage by repeatedly quoting the
same near-zero price (structurally *unreplicable* once copy-lag and taker
fees are added — the profit only exists for the original quoter). An
early heuristic (an upstream "is this wallet a bot" flag) conflated
"automated" with "unreplicable," which is the wrong axis — a purely
algorithmic directional trader is fine to copy; the underlying rebate
farmer is not, whether or not it's automated.

**The fix.** Built a hard gate directly on the trade *pattern* instead of
the bot label: flag a wallet only when its recent trades satisfy three
conditions together — a majority sit at an extreme price (<0.05 or >0.95),
the single most-repeated (market, price) pair recurs at least 5 times, and
at least half of those extreme-price trades are specifically on the sell
side (selling into a near-zero price is the rebate-farming signature;
repeatedly *buying* longshots is a directional bet, not liquidity
provision). Verified against four confirmed bad actors before shipping —
all four showed the identical signature (e.g., 29 fills at the exact same
`(market, $0.007)` quote, minutes apart, varying size).

**The catch.** The first version of that gate shipped with only two of the
three conditions (extreme-price concentration + quote repetition, no
buy/sell check). The very next scoring run flagged 9 wallets. Rather than
trust the gate, each one got checked by hand — and one of them,
`quant-generalist-2` (a wallet I'd already deliberately curated into the
live-tracked set), didn't fit the pattern at all: 44 of 47 recent trades
were *buys*, a 47.6% win rate (a real farmer wins nearly every trade,
selling near-certain outcomes — a near-coin-flip rate is the opposite
signature), and its "7x repeated quote" turned out to be seven buys
sweeping a thin ask at the same price within a five-second window — a
directional bet against a shallow order book, not farming.

**The root cause and fix.** The gate was measuring price concentration and
repetition but never checking which side of the trade it was on — exactly
the distinction the whole filter exists to draw. Added the third condition
(majority sell-side among the extreme-price trades) with a deliberately
loose threshold (50%, not a tight cutoff): the confirmed bad wallets sit at
98–100% sell, the false positive sits at ~5% — there was no knife-edge
number to get wrong, a wide margin separates the two cases cleanly. Both
the reproduction of the false positive and the fix are covered by unit
tests, and the fix was re-verified against the live database rather than
just the synthetic case.

The reason I'm including this rather than a clean success story: the
process — verify against real data before shipping, don't trust a new gate
blindly on its first production run, check the *specific* wallet you
already have conviction on when something unexpected happens, find the
actual root cause rather than patching the symptom — is the part I think
is more informative than the gate itself.

## 4. Case study: building an independent verification layer, and a $450K discrepancy that turned out not to be a bug

Wallet scoring up to this point trusted an upstream provider's computed
analytics (win rate, lifetime PnL, trader classification) — which section
3 had just shown could be wrong in ways that mattered. The fix was to stop
trusting computed analytics from anywhere upstream and reconstruct wallet
performance from raw trade history directly: `PositionTracker`, an
in-house engine that replays a wallet's fills from a public, unauthenticated
market-data endpoint, tracks weighted-average cost basis, matches
resolutions against a second independent API, and computes realized PnL
from first principles.

To trust it, it had to be checked against a known-good source — bullpen's
own reported lifetime PnL for the same 17 tracked wallets. The first full
reconciliation run showed 14 of 17 wallets off by 60–120%, two with the
wrong sign. That's a big enough gap to be a real bug, and it was tempting
to assume one. Instead of debugging the arithmetic first, I looked for the
actual pattern: every close match came from a wallet whose entire trade
history fit under a 5,000-trade fetch cap; every large diff came from a
wallet that hit that cap. The comparison itself was wrong — a capped,
recent reconstruction was being diffed against the provider's *true
lifetime* figure. Rebinding both sides to the same 30-day rolling window
(matching a windowing choice already made elsewhere in the scoring system,
for consistency rather than convenience) fixed most of it: 3 of 17 wallets
with no open positions in the window matched to within 2%, which is real
confirmation the underlying fee/cost-basis/resolution math is correct.

One wallet still didn't reconcile after that fix: a $458K gap on a wallet
nicknamed `yield-farmer-1`. Rather than write it off as noise or assume a
lingering bug, I tested two concrete hypotheses in order and ruled each one
out with numbers before accepting a third: boundary-straddling positions
(trades opened before the window, closed inside it) accounted for -$167,
nowhere close; resolution-based closes missed by the windowing logic
accounted for another -$11K, still nowhere close. The actual explanation,
reached by elimination: the upstream provider's PnL figure is very likely
*mark-to-market* — portfolio value change including unrealized gains on
open positions — while this system's realized-PnL engine deliberately
excludes anything not yet closed, on the principle that an open position's
outcome is genuinely unknown until it resolves. `yield-farmer-1`'s own
profile (many cheap longshot positions that appreciate in price without
necessarily resolving) is exactly the shape where those two definitions
diverge hardest. Not a bug — two systems correctly answering different
questions — but a real, useful finding: any score built on realized PnL
should probably be confidence-weighted by how much of a wallet's recent
activity is still open, rather than trusted equally regardless.

## 5. Independent risk-management layer

Sizing is only half the system — the other half is a portfolio-level risk
manager that runs independently of the sizing logic and can veto or shrink
any trade regardless of how attractive the score looks:

- **Circuit breakers on statistical significance, not raw streaks.** A
  wallet gets muted when its recent losses are significant under a t-test
  against zero expected value — a 3-loss streak on a wallet with genuine
  edge is expected variance, not evidence of a problem; the breaker is
  built to tell the two apart.
- **Per-wallet and portfolio-level exposure caps**, enforced independently
  of position sizing.
- **A duplicate-exposure guard, spread/liquidity checks, and order-book
  staleness checks** before any fill is accepted.
- **A "zombie position" sweep** for the genuine edge case where a market's
  order book goes permanently stale or the market itself gets delisted —
  deliberately a separate, narrower exit path from normal position
  management (a wider slippage floor, no patient-exit pegging) rather than
  a loosened version of the normal rules, and shipped behind a feature
  flag so the first rollout is pure observability before anything gets
  force-closed this way.

## 6. Engineering practices

- Every risk-relevant change ships with unit tests covering the boundary
  cases, not just the happy path (the false-positive fix above is a direct
  example: the reproduction case is now a permanent regression test).
- New logic gets verified against real, live data before being trusted —
  a new gate's first production run gets manually checked, not assumed
  correct because the tests pass.
- Two independent, unmodified-language-parity data structures (a composite
  trade ID format shared between the Python and TypeScript sides) so the
  same trade is never double-counted across the two halves of the system.
- A living risk-management ledger (`RISK_MANAGEMENT.md`, 45 dated entries)
  documenting every risk-relevant decision with its motivating problem,
  mechanism, and verification — not just a changelog, a record of *why*.

## What I'd value feedback on

- Whether the half-Kelly + empirical-Bayes shrinkage approach is the right
  level of sophistication for this problem, or overbuilt/underbuilt
  relative to what a real allocator would do here.
- The capital-multiplier saturation/cap choice (0.35 Sharpe → 2x) — picked
  deliberately but without a rigorous basis beyond "well above the
  tracking threshold."
- Anything in the verification approach (sections 3–4) that reads as
  process I should be doing differently, not just conclusions I should
  double-check.
