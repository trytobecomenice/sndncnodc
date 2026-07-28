# Roadmap

Forward-looking task list for the copy-trading system. `PROJECT_SUMMARY_FOR_REVIEW.md`
stays a narrative writeup of what's shipped; this is the working list of what's next.

## Phase 1 — Shipped

- Half-Kelly sizing with empirical-Bayes shrinkage, independent portfolio-level risk
  management layer, `PositionTracker` (in-house PnL reconstruction).
- Depth-Aware Trade Sizing (Rule 47) — per-trade clamp against real order-book depth,
  opt-in, default off.
- Parameter Sensitivity Harness — real-data backtest for Kelly sizing constants, plus
  structural-only sensitivity checks where no real trade history exists yet.

## Phase 2 — In Progress / Next Up

- **Correlation Tagging System** (e.g. `#US_Election`) — cross-event/cross-market
  concentration is currently unmodeled: two economically-linked bets split across
  different events (or two different BTC-strike markets) can each independently max
  out `MAX_EVENT_EXPOSURE_USD` with no cross-check. Plan: a manually-tagged
  "correlation cluster" with its own exposure cap, applied alongside the existing
  per-event/per-wallet caps, not replacing them.
- **Forward-Looking Scenario Simulator / Stress Testing** — the kill switch is
  reactive only (halts after a real drawdown already happened). Plan: a prospective
  historical-scenario stress test ("if the N worst-performing tracked wallets all had
  a bad week simultaneously, what's the hit to the portfolio") using data already on
  hand.

## Phase 3 — Future

- **Unified Capital Allocation (Sharpe-ratio based, across trading bots)** — the copy
  bot and Weather Bot are currently two fully separate capital silos. Once both have
  enough real track record, allocate capital between them by risk-adjusted return —
  the same idea as the existing per-wallet Sharpe-based capital multiplier
  (`scoreWallets.ts`'s `computeCapitalMultiplier`), one level up.
- **UMA Oracle Dispute Check** — Polymarket markets resolve via UMA's optimistic
  oracle and can be formally disputed. No current check exists for "this market's
  resolution is currently contested" before treating a settlement price as final —
  probably low-frequency, but a real tail risk.
- **Automated Wallet Discovery Engine** — extends the existing discovery pipeline
  (`discoverCategorySpecialists.ts`, `propose_pool_refill.py`) toward a more automated
  end-to-end flow. Every promotion into `TRACKED_TRADERS` still goes through explicit
  human review today (a deliberate design choice, not an oversight) — this phase is
  about tightening the pipeline feeding that review, not removing the review itself.
