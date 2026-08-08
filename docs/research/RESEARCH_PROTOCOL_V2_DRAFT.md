# Copy Alpha Research Protocol v2 — Semantic Draft

Status: `DRAFT_NOT_FROZEN`. This document and the evaluator must be frozen in
the same commit before the epoch clock can start. Nothing in a PASS result can
authorize Live; PASS authorizes calibration work only.

## Estimand and primary domain

The primary estimand is net executable copy return for source-aligned dynamic
exits, with resolution terminations retained as a competing regime. Primary
entry evidence is restricted, before looking at v2 results, to executable VWAP
prices from 0.35 through 0.85 inclusive. Signals outside the domain remain in
raw shadow evidence but cannot migrate into the primary result after the fact.

The sampling variance uses the complete law of total variance:

`w Var(resolution) + (1-w) Var(exit) + w(1-w)(mu_resolution-mu_exit)^2`.

The between-regime term is expected to be material because the exit policy
selects profitable paths into TTP. When uncertainty in `w` is propagated, the
evaluator scans the entire preregistered confidence interval, including
interior extrema; checking endpoints alone is forbidden.

## Competing termination causes

Every termination is captured at event time as exactly one of:

- `INTENDED_SOURCE_RESOLUTION`
- `SOURCE_EXIT`
- `TTP_EXIT`
- `TTP_ELIGIBLE_BUT_PRICING_FAILED`
- `EXIT_SIGNAL_BUT_UNEXECUTABLE`
- `SYSTEM_CENSORED`
- `UNKNOWN`

System failures are reported separately and cannot enter the normal strategy
mixture weight. Historical resolution events without an event-time source
inventory snapshot remain `UNKNOWN`; they are never reconstructed by guess.

## Epoch prerequisites

The epoch cannot start until all are true for a continuous seven-day window:

1. durable event-to-tax-lot PnL ledger is authoritative with zero unresolved
   allocations;
2. TTP price-read success and executable-bid rates are each at least 99%;
3. event-time termination classifier is deployed and `UNKNOWN <= 1%` among
   final `bot_filtered` lot terminations whose allocation source is `live`
   and whose event time falls inside the qualification window. Historical
   backfill and partial reductions are excluded from this denominator;
4. there are zero active `QUARANTINED_UNPRICEABLE` positions. Quarantine
   suppresses retry noise but never turns an unknown mark into a successful
   observation or accounting finality;
5. frozen latency ECDF, entity clustering version, termination classifier,
   dataset schema, protocol, evaluator, MC seed/code, and reference parameter
   table are present in one SHA-256 manifest;
6. selected and randomized matched-control cohorts are fixed.

## Statistical units and controls

Addresses are not independent units. The primary unit is a PnL-blind,
versioned entity cluster, first linked by common proxy owner EOA and then by a
frozen secondary algorithm. Design effects are evaluated at both entity and
signal-cluster layers and the more conservative requirement is used. At least
30 independent entities and 50 independent signal clusters are required;
fewer than 30 entities can never trigger PASS from a confidence interval
alone. Wild cluster bootstrap uses Rademacher weights.

The selected cohort is compared with a non-PnL-selected control matched on
activity, category, entry-price distribution, time of day, size, and
liquidity. This tests whether wallet selection adds value separately from
whether copying in general adds value.

## Latency and drift

The primary model samples a frozen empirical latency ECDF. P99 is stress only:
it may veto PASS but cannot inflate the primary sampling variance. Rolling
seven-day `w` control intervals and rolling KS comparisons against the frozen
latency ECDF require two consecutive breaches before a documented protocol
deviation. The frozen ECDF itself is never silently updated mid-epoch.

## Decisions

System-integrity kills do not wait for statistical power. Strategy kills are
deliberately OR-combined and accept a higher false-rejection rate because
promoting a negative-EV strategy is more costly than rejecting a positive one.
The primary strategy criterion is a positive wallet-clustered net-return lower
bound. Rejected-cohort counterfactuals test whether gates are reverse-alpha
filters.

The epoch loss budget is USD 100 and epoch-scoped. A budget breach before
adequate power is `REJECTED` for this epoch plus insufficient evidence; it does
not grant a fresh budget automatically. At day 30 without required effective
sample size, the result is `INSTRUMENT_INSUFFICIENT`, not an indefinitely
extendable `INCONCLUSIVE`.

Terminal states are `SYSTEM_INTEGRITY_KILL`, `PRECONDITION_FAILED`,
`INSUFFICIENT_EVIDENCE`, `INCONCLUSIVE`, `REJECTED`, `PASS`, and
`INSTRUMENT_INSUFFICIENT`. After REJECTED, copy-direction promotion stops and
a post-mortem is due within seven days.
