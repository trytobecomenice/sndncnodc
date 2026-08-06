# Paper Ledger Integrity Incident Review — 2026-08-07

Status: **P0 independently reproduced and isolated on AWS Paper; strategy remains rejected for
Live.** This document records the complete structured review received on 2026-08-07 and
the immediate operating decisions that follow from it. It does not silently rewrite historical
rows, authorize a production database mutation, or assert that an external calculation is already
verified production truth.

Read with:

- `CLAUDE_HANDOFF.md` for the current operator boundary;
- `docs/CURRENT_STATE.md` for the corrected status of reported versus clean economic figures;
- `docs/TODO_2026-08-06.md` for the superseding P0 work queue; and
- `docs/research/SHADOW_REPLAY_ARCHITECTURE_2026-08-06.md` for the evidence standard that Paper can
  reject a strategy but cannot authorize Live.

## 1. Executive conclusion and immediate decision

An external quant review alleges that a large part of historical Paper PnL came from stale/replayed
source trades entering markets whose outcomes were already known, followed by near-immediate
`resolved` closes. If reproducible, this is look-ahead/fill-simulation contamination rather than
copy alpha.

The review's central candidate result is:

```text
raw/local closed sample:       457 rows, +$519.16, +24.58% ROI
candidate clean sample:        127 rows, -$59.86,  -9.38% ROI
candidate clean win rate:      61.4%
post-stale-guard sample:        14 rows, -$40.74, -54.77% ROI
```

These exact figures are **external-review/local-snapshot claims and are not the authoritative AWS
result**. Codex independently confirmed the core failure, but not this exact decomposition.

### 1.1 Independent verification — 2026-08-07

Read-only AWS query at approximately 00:37 HKT, checkout `1d36f63`:

```text
raw bot_filtered closes:                  641
raw paper_trade realized PnL:       +$620.40
raw cost basis:                     $4,332.84

causal v1 candidates:                     455
candidate PnL:                       +$669.00
candidate cost:                     $3,277.10

candidate-clean remainder:                186
candidate-clean PnL:                  -$48.60
candidate-clean ROI:                   -4.60%
candidate-clean win rate:              48.39%

post-2026-07-31 UTC closes:                 65
post-guard PnL / ROI:              -$33.06 / -8.57%
AWS equity_hwm:                     $2,173.60
```

The classifier is intentionally causal rather than purely duration-based:

1. a new Paper row opened after this local ledger had already resolved the same market; or
2. a `resolved` row whose slug contains a valid event date and whose opening was at least one full
   UTC day later.

On AWS, rule 1 identified 407 rows, rule 2 identified 310, with 262 overlap; the union is 455.
The exact cited market was also present on AWS, with nine repeated identical `0.32` entries on
2026-07-30 and repeated resolution profits. That independently proves the time-machine mechanism.

The external review's 127-row clean result dropped all 330 sub-ten-minute closes. That is useful
anomaly evidence but too broad to become a destructive classifier: legitimate trailing exits can
also close quickly. On the exact local 457-row DB, causal v1 instead flags 316 rows carrying
`+$524.59`, leaving 141 rows / `-$5.43` / `-0.75%` ROI. Both defensible clean views reject the old
profit claim; neither proves a profitable strategy.

The following operating decisions therefore take effect immediately:

1. Historical reported PnL is quarantined from claims of demonstrated alpha.
2. No old PnL-dependent wallet ranking, cap, circuit-breaker decision, challenger promotion,
   category conclusion, or equity high-water mark is trusted without a clean recomputation.
3. Copy Bot remains Paper-only. No Live enablement, larger sizing, new strategy, or roster expansion
   is allowed from the contaminated evidence.
4. Historical rows must remain auditable. Mark or classify contamination; do not delete or rebase
   it.
5. The first engineering task is independent forensic reproduction and contamination isolation,
   not a broad architecture rewrite.

## 2. Finding A — candidate phantom PnL

### 2.1 Holding-time evidence reported by the reviewer

| Holding time | Trades | Cost | PnL | ROI |
|---|---:|---:|---:|---:|
| under 10 minutes | 330 | $1,473.87 | +$579.02 | +39.3% |
| 10–60 minutes | 4 | $21.00 | -$11.98 | -57.0% |
| 1 hour–1 day | 43 | $205.63 | -$87.78 | -42.7% |
| 1–7 days | 24 | $130.11 | -$47.31 | -36.4% |
| over 7 days | 11 | $72.62 | -$5.19 | -7.1% |

The alleged anomaly is not merely a high short-horizon return. The under-ten-minute cohort alone
earns more than the total reported PnL while every longer-holding bucket is negative.

### 2.2 Market-date evidence reported by the reviewer

The reviewer parsed event dates embedded in market slugs and reported:

```text
entries after the event date:  285 trades, +$328.06
normal chronology:              45 trades,  -$66.39
median event-date lag:                     24 days
maximum event-date lag:                    43 days
```

The cited example is `fifwc-nld-mar-2026-06-29-draw`: six allegedly identical entries were opened
between 2026-07-25 and 2026-07-27 at price `0.32`, then resolved roughly five minutes later for
`+$12.75` each, despite the encoded match date being 2026-06-29.

### 2.3 Candidate mechanism

The hypothesized chain is:

```text
historical source trade reappears in a polling/feed replay
  -> dedup boundary does not reject it
  -> Paper simulator accepts a frozen/stale last-trade price
  -> market outcome is already known
  -> resolver closes the Paper position almost immediately
  -> guaranteed-looking profit is booked as realized PnL
```

This mechanism is consistent with the earlier replay/dedup incident fixed on 2026-08-05 plus the
stale-market guard added on 2026-07-31. The repeated open-after-local-resolution rows and dated
post-event rows independently reproduce it on AWS. Source-feed provenance remains a separate
follow-up, but the economic contamination itself is proven sufficiently to quarantine decisions.

## 3. Finding B — candidate feedback contamination

The greater risk is downstream feedback, not the display error itself. The reviewer reports the
following wallet-level decomposition:

| Wallet | Reported PnL | Candidate phantom PnL | Candidate clean PnL | Phantom/total |
|---|---:|---:|---:|---:|
| `0x65018f9f` / `strict-7` | +$349.48 | +$376.32 | -$26.85 | 178/186 |
| `0x510904c9` / `political-whale-1` | +$176.35 | +$180.63 | -$4.29 | 23/30 |
| `0x6211f97a` / `strict-9` | +$55.38 | +$55.38 | $0.00 | 28/28 |
| `0x5966db1f` / `strict-8` | +$24.29 | +$24.29 | $0.00 | 15/15 |
| `0xdc767c90` / `crypto-specialist-1` | -$9.12 | -$17.48 | +$8.37 | 23/31 |
| `0xbaa2bcb5` | +$3.84 | -$14.91 | +$18.75 | 2/12 |
| `0xf0318c32` | -$39.12 | -$39.12 | $0.00 | 9/9 |

If verified, contamination reaches:

- `db.get_wallet_realized_ev_stats()`;
- `compute_wallet_ev_cap_usd()` and `WALLET_EV_CAP`;
- Rule 35 EV circuit-breaker decisions;
- category scores and wallet rankings;
- challenger promotion/replacement evidence;
- clean-evaluation baselines; and
- `bot_risk_state.equity_hwm` and drawdown calculations.

The reported HWM is `$1,853.36` against a `$1,125` bankroll. It must be treated as quarantined
until reconstructed from clean equity evidence. Do not simply subtract `$579` by hand: recompute
from the chronological clean ledger so the HWM path, open marks, deposits, and realized events
remain internally consistent.

## 4. Finding C — candidate strategy economics after cleaning

The reviewer reports the following clean-sample economics:

```text
win rate:              61.4%
average winning ROI:  +44.3%
average losing ROI:   -89.9%
break-even win rate:   67.0%
estimated expectancy: -7.5% per trade
```

The arithmetic break-even rate is `loss / (win + loss)`, but this estimate still requires
independent reproduction with clear weighting, dust handling, fee treatment, and tax-lot rules.

The reviewer also reports:

| Close path | Trades | ROI | Average entry price | Average peak profit |
|---|---:|---:|---:|---:|
| `trailing_tp` | 34 | +52.0% | 0.181 | +88.4% |
| clean `resolved` | 82 | -35.5% | 0.537 | +7.5% |

This is **not** valid evidence that trailing exits beat resolution: TTP membership is conditioned
on first becoming a winner, so the comparison contains survivorship/selection bias. The useful
hypothesis is narrower: profitable copyability may be concentrated in low-entry-price moves and
short-lived price impact rather than source-wallet resolution accuracy.

The earlier entry-price buckets also require a clean rerun because their reported shape still
contains the alleged phantom cohort. No bucket threshold becomes a trading gate from the old
analysis.

## 5. Finding D — Kelly sizing does not implement bankroll Kelly

The current mapping is reported as:

```python
clamped = min(1.0, half_kelly_fraction)
size = min_trade_usd + (max_trade_usd - min_trade_usd) * clamped
```

Because half-Kelly is structurally at most `0.5`, a `$3–$10` interval has a mathematical ceiling
of `$6.50` on this path. The reviewer observed a maximum Kelly-path order near `$6.13`, while
`$10.00` occurred only through other paths. The mapping compresses large changes in estimated edge
into small changes in order size and leaves many trades on the `$3` floor.

The candidate correction is:

```python
target_usd = half_kelly_fraction * effective_bankroll_usd
size_usd = max(min_trade_usd, min(max_trade_usd, target_usd))
```

This correction must be tested in observation-only Paper mode first. It is not authorization to
increase size. Before implementation, verify the present code, all alternate sizing paths,
bankroll definition, portfolio correlation, liquidity capacity, and how negative/uncertain edge is
handled. A Kelly formula cannot repair a negative or unproven edge.

## 6. Finding E — TTP and operational error saturation

The external review reports roughly 3,000–4,000 daily errors and 41,725 historical error events,
including:

```text
2026-08-03: 2,759
2026-08-04: 4,276
2026-08-05: 2,938
2026-08-06: 3,166
```

| Candidate class | Reported count | Risk |
|---|---:|---|
| TTP price fetch / `No route to host` | 4,672 | positive exit path becomes unavailable |
| market slug not found | 3,988 | zombie position retries forever |
| authenticated session expired | 1,816 | full poll cycles become blind |
| closeout market check failed | 1,502 | closeout coverage degrades |
| string/int comparison | 759 | one malformed value may abort a sweep batch |
| token order-book errors | about 9,000 | repeated pricing failure and alert noise |

Required response:

1. Independently group errors by exact normalized cause, day, market, and affected positions.
2. Add a regression test proving a blank/string `last_trade_price` cannot abort the batch.
3. Measure `eligible TTP checks`, `successful prices`, `failed prices`, and `positions skipped` so
   the exit-pricing failure rate has a denominator.
4. Define an initial Paper SLO and Telegram alert policy after measuring the baseline. Do not turn
   an unvalidated example threshold into production authority.
5. Deduplicate alerts by cause/market and preserve occurrence counters plus first/last timestamps.
6. Add a bounded retry/quarantine state for persistently unresolvable markets, with one actionable
   alert and an explicit manual-reconciliation queue.
7. Treat authentication expiry as a data-gap event and journal the missing observation window.

## 7. Finding F — Shadow Rehab may be economically non-functional

The reviewer reports:

```text
shadow_rehab open:       698 rows
shadow_rehab closed:       4 rows
recorded open cost:   $15,225.44
oldest open:          2026-07-26
```

These figures conflict with an earlier AWS handoff snapshot that reported 297 open rows and a much
larger recorded cost basis. The discrepancy itself is a reason to verify strategy filters,
database source, units, and query definitions before accepting either number.

If the 698:4 lifecycle is reproduced, Shadow Rehab is not producing enough completed evidence to
decide rehabilitation. Required design response:

- report lifecycle completion rate, age distribution, unpriceable count, and source-exit coverage;
- enforce strategy isolation inside risk-manager entry points, not solely at call sites;
- reject any `shadow_rehab` row from normal exposure/equity/capital inputs with tests;
- define retention only after economic lifecycle correctness is established.

## 8. Finding G — two quiet risk-design failures

### 8.1 Unpriceable positions are not zero-risk

Carrying an unpriceable position at cost is neutral for HWM inflation but optimistic for drawdown
detection when missing prices correlate with dead markets, illiquidity, or near-zero value.

The agreed target design separates:

```text
markable_equity       -> evidence/HWM reporting
conservative_equity   -> drawdown and kill-switch assessment
unpriceable_cost      -> explicit exposure metric and alert input
```

The haircut/last-known-price policy must be versioned and sensitivity-tested. Missing data is not
silently converted to zero loss or a fabricated liquidation price.

### 8.2 Capital saturation creates arrival-order allocation

The reviewer reports:

```text
skip_risk_exposure_ceiling: 19,239
skip_risk_event_cap:         8,540
skip_risk_wallet_cap:        6,607
paper_buy:                   1,044
```

If verified, capital is frequently saturated and admission becomes substantially first-come,
first-served even though upstream code computes EV/Kelly/category evidence. This does not
automatically justify replacing existing positions: forced turnover introduces spread, fees,
liquidity risk, and selection bias.

Immediate requirement: promote early-rejection/foregone-opportunity evidence from P3 to P0
research. Record the bounded raw signal, rejection reason, contemporaneous executable book, and
rank relative to held positions; calculate foregone EV offline using source-aligned exits. Do not
run full production EV/Kelly computation when capital is unavailable.

## 9. Independent verification protocol

No destructive DB change is allowed before these read-only steps pass:

1. Identify the authoritative AWS commit, process start, DB file, schema version, row counts, and
   query cutoff. Record UTC and HKT.
2. Export immutable identifiers and fields needed for reproduction: position ID, wallet, strategy,
   market slug/ID, token/outcome, source trade ID, open/close times, entry/exit price, shares, cost,
   PnL, and close reason.
3. Reproduce holding-time buckets without relying on reviewer-provided aggregates.
4. Parse market dates only where the slug format is unambiguous; record unknown/ambiguous slugs
   separately. Cross-check official event metadata where available.
5. Test the proposed phantom classifier against known good and known contaminated examples.
   `resolved` plus holding time under 600 seconds is a high-recall candidate rule, not automatically
   a perfect ground-truth label.
6. Produce a row-level manifest and deterministic query/script hash so totals are auditable.
7. Recompute reported and clean PnL, ROI, win/loss distribution, wallet ranks, categories,
   circuit-breaker evidence, and chronological equity/HWM.
8. Compare local and AWS separately. Never merge their totals or call the laptop DB production.
9. Only after review, add a forward schema field or immutable classification table through the
   TypeScript/Drizzle migration owner. Do not hand-edit historical rows ad hoc.

Suggested classification states are more honest than a single irreversible boolean:

```text
UNREVIEWED | PHANTOM_HIGH_CONFIDENCE | PHANTOM_POSSIBLE | CLEAN_CONFIRMED
```

If the existing design requires `is_phantom`, retain a reason, classifier version, evidence JSON,
classified timestamp, and reversible audit trail.

## 10. Superseding priority order

### P0 — contain and reproduce

1. Quarantine historical reported PnL and all PnL-fed automation claims.
2. Run the independent read-only forensic reproduction.
3. Build a tested, auditable contamination classifier; do not delete rows.
4. Publish raw/reported and clean/candidate-clean figures side by side.
5. Recompute chronological HWM/equity from the reviewed clean ledger.

### P0.5 — cut the feedback loop

1. Make wallet realized-EV queries exclude reviewed contamination.
2. Re-evaluate wallet caps, circuit breakers, category scores, and promotion state.
3. Review `strict-7`, `crypto-specialist-1`, and `0xbaa2bcb5` as hypotheses, not automatic status
   changes; sample sizes and uncertainty remain visible.
4. Enforce Shadow Rehab isolation at the risk-manager boundary.

### P1 — restore exit observability

1. Fix and regression-test batch-aborting price type failures.
2. Add TTP success/failure denominators, SLO reporting, deduplicated alerts, and zombie quarantine.
3. Record auth-expiry data gaps and verify closeout coverage.
4. Add conservative-equity and unpriceable-cost reporting.

### P2 — revalidate the strategy

1. Rerun all wallet, category, entry-price, and expectancy research on clean rows only.
2. Design a prospective randomized/paired Paper exit experiment. A fixed `T+6h/T+24h` exit must
   not overwrite source-exit-aligned attribution; report both as separate policies.
3. Observe corrected Kelly sizing without increasing the `$3–$10` safety bounds.
4. Measure rejected/foregone signals and capacity saturation offline.

## 11. Explicit non-actions

- Do not enable Live.
- Do not increase trade size or bankroll.
- Do not add wallets, strategies, bots, or infrastructure complexity to escape the result.
- Do not delete/rebase historical PnL.
- Do not manually unmute, promote, or retire a wallet from the external table alone.
- Do not rewrite the architecture before reproducing the data-integrity failure.
- Do not call TTP-versus-resolution cohorts an A/B test.

## 12. What remains genuinely strong

The review also records the following strengths:

1. The project remained `LIVE_MODE=False`; the suspected defect produced an audit problem rather
   than a real-money loss.
2. The reviewer saw 783 passing tests at review time; the repository subsequently reached 784
   passing tests in the 2026-08-06 post-mortem-script verification. Test count alone does not prove
   economic correctness, but it gives the repair work a strong regression base.
3. Comments around empirical-Bayes shrinkage and the 2028-election phantom equity swing preserve
   failure evidence and design rationale rather than only final code.
4. Existing research rules already say Paper evidence can reject but not authorize Live, unknown
   values cannot silently become zero, and thin-book mid/last prices are not realizable equity.
5. `clean_evaluation_epoch` shows that historical-evidence contamination was already recognized as
   a risk, even though the classifier boundary was incomplete.
6. The stale guard appears to have stopped the instant-resolve pattern after 2026-07-28/31. That
   must still be verified row-by-row, but it is evidence that the containment direction worked.

A negative clean result is useful evidence. It resets the research baseline; it does not
invalidate the engineering discipline that discovered and contained the problem.

## 13. Change record

1. **When:** 2026-08-07 HKT.
2. **Files adjusted:**
   - `docs/research/PAPER_LEDGER_INTEGRITY_INCIDENT_2026-08-07.md` — created as the canonical
     incident/evidence record.
   - `docs/CURRENT_STATE.md` — quarantined unqualified historical PnL claims.
   - `docs/TODO_2026-08-06.md` — inserted the superseding P0 incident queue.
   - `CLAUDE_HANDOFF.md` — added the urgent handoff summary and exact document route.
3. **What changed:**
   - Recorded the full external quant review, its numbers, limitations, feedback-contamination
     map, operational findings, and recommended sequence.
   - Separated external claims from independently verified production truth.
   - Changed documentation authority only. No bot logic, DB row, wallet state, risk state, AWS
     process, `.env`, key, credential, GitHub branch, or deployment was changed.
