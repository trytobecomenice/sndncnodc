# Friction and wallet-archetype hypotheses (2026-08-09)

Status: research hypotheses only. Nothing here changes the epoch-v2 price domain, exit policy,
roster, risk limits, paper accounting or Live authorization.

## What is established

- Paper BUY and SELL accounting uses executable order-book prices and reported fees whenever the
  preview succeeds. The old `measure_paper_shortfall()` "measurement only" docstring was false and
  was corrected.
- Polymarket fees are market-specific. A single global 5% fee-rate assumption is not valid evidence
  for the tracked cohort; authoritative attribution must use the fee metadata captured for each
  exact fill observation.
- Phase-0's decision book is known after the source trade becomes visible to the recorder. It is not
  a causal source pre-trade BBO, so it cannot prove whether the source wallet was maker or taker.

## What is not established

- `clean net PnL + median friction` is not an estimate of gross alpha. It mixes estimators and, in
  the cited calculation, different database populations. Median shortfall cannot be added to a
  cost-weighted cumulative return.
- Removing friction is not a passive counterfactual: it changes which orders fill and can condition
  the sample on adverse selection. Any future gross-alpha figure must be event/tax-lot allocated on
  one fact-clean population with explicit unknown coverage.
- Moving the primary price domain, adding a flat exit hurdle, or switching to maker execution changes
  the strategy estimand. These are not arithmetic-only optimizations and remain frozen pending a
  preregistered offline comparison.

## Safe artifact

`research/wallet_archetype_evidence.py` produces a static, PnL-blind profile containing continuous
flow/timing evidence and explicit UNKNOWN capability states. It does not classify maker/taker,
write the bot database, or influence any roster/risk decision. A future classifier requires either
venue-provided role data or a causally ordered pre-trade BBO/trade tape.

## Next evidence after ledger-v2 cutover

1. Allocate fee, price shortfall and realized PnL to the same clean tax lots.
2. Report sums and cost-weighted rates, not an aggregate ROI plus a median percentage.
3. Separate measured fee, measured entry/exit shortfall, unknown-book fallback and resolution exits.
4. Only then test whether any wallet archetype proxy predicts *copyable net* outcomes out of sample.
