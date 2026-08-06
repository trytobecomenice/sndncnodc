# Copy Bot Strategy and Expansion Decision Record — 2026-08-06

Status: jointly accepted research/strategy direction; a minimal Phase 0 attribution walking
skeleton is implemented locally but feature-disabled, and the broader policy is **not implemented**
or production-active. The current Copy Bot remains paper-only. This record does not authorize live
orders, key custody, larger sizing, an AWS resize, a C++ rewrite, a new data vendor, or another
strategy deployment.

Read with:

- `docs/research/SHADOW_REPLAY_ARCHITECTURE_2026-08-06.md` for journal/replay, process isolation,
  settlement finality, liquidity survival, and honest Paper PnL;
- `docs/copy-trading/RISK_MANAGEMENT.md` for implemented risk rules and historical rationale; and
- `CLAUDE_HANDOFF.md` for current AWS/runtime truth.

## 1. Agreed strategy identity

The target is no longer a dumb action copier:

> A followed-wallet action is a probabilistic feature that updates our view of an event. It is not
> an instruction to trade. The system trades only when independently estimated residual alpha
> remains positive after observable friction and portfolio risk.

The immediate economic objective remains narrow: prove that the existing `$3-$10` Copy Bot retains
positive **net copyable alpha** after source impact, observation delay, local delay, executable
price, fees, fill uncertainty, settlement risk, exit liquidity, concentration, and capital shadow
cost. More capital, more strategies, and faster infrastructure do not create alpha.

The preferred future description is a **wallet-conditioned signal engine**. Copy signals may later
be combined with primary-source, logical-payoff, or relative-value evidence, but every strategy
keeps independent attribution, risk capital, and a kill switch.

## 2. Non-negotiable decision pipeline

Every proposed Copy entry must pass, in order:

```text
1. source-action intent classification
2. wallet alpha posterior and uncertainty
3. signal age / alpha-decay gate
4. source-impact and chasing gate
5. size-specific executable book/VWAP
6. net residual-alpha lower bound
7. logical/factor/scenario portfolio risk
8. fractional-Kelly and capital availability
9. market quality, toxicity, TTR, and system health
10. settlement/auth/rate/allowance readiness
11. final dispatch-time price, generation, lease, and deadline recheck
12. price-bounded FOK or FAK
```

Failure at any layer means `NO_TRADE`. A Maker order is not a rescue path for an entry whose Taker
economics fail.

## 3. Wallet evidence and residual alpha

### 3.1 Source intent comes before direction

A public BUY can be a new directional entry, an increase, a hedge, an inventory rebalance, or one
leg of a wider event portfolio. The first model output is therefore one of:

```text
NEW_ENTRY | INCREASE | REDUCE | EXIT | HEDGE | INVENTORY_REBALANCE | UNKNOWN
```

`UNKNOWN` does not automatically create an entry. Portfolio context, opposite token/event activity,
prior position, trade sequence, and source size relative to its history are evidence, not perfect
knowledge.

### 3.2 Do not pretend to know a wallet's target price

Observed stopping behavior cannot uniquely reveal private fair value: the wallet may stop because
of capital, risk, liquidity, a hedge, or a strategy boundary. Offline profiling produces a
distribution, not a scalar target:

```text
wallet/model/version
market/category/regime/side
intent classification and confidence
fair-probability posterior and conservative lower bound
alpha half-life distribution
post-trade markouts by horizon
source size/impact and chase-capacity curve
sample count, calibration, and uncertainty
```

Training and evaluation are separated by time/market with walk-forward testing. Wallet selection,
leaderboard and survivorship bias, large-win concentration, regime decay, and repeated signals from
the same information cluster remain visible.

### 3.3 Signal time is an interval

Polling does not provide an exchange-quality event timestamp. Store source-reported time, the last
poll without the trade, first local receipt, poll cadence, clock uncertainty, and lower/upper signal
age bounds. An arbitrary universal millisecond cutoff is rejected. Each wallet/market/regime must
earn an empirical alpha-decay curve; a strategy whose lower-bound alpha half-life is shorter than
end-to-end p99 latency is not deployable on that path.

### 3.4 Chasing and net-edge equations

At actual size, side-adjusted chase is measured against source execution evidence using executable
VWAP, not a headline BBO percentage. Near-zero/near-one markets make percentage moves misleading.

For a BUY of a binary token:

```text
net_edge_per_share =
    conservative_payout_probability
    - executable_buy_vwap(size)
    - taker_fee_per_share
    - capital_shadow_cost_per_share
    - settlement/model-risk_buffer
```

Executable VWAP already contains the crossed spread and visible multi-level price impact; subtracting
the full spread again would double count. Source impact, signal age, quote survival, and fill
uncertainty affect the conservative probability, allowable size, or explicit risk buffers. Entry
requires the lower confidence bound of net edge to exceed a versioned minimum hurdle at decision
and immediately before dispatch.

## 4. Entry/exit execution policy

### 4.1 Copy entries are Taker-only

Accepted first-live policy:

```text
COPY_ENTRY_ALLOWED = price-bounded FOK or FAK
RESTING_MAKER_COPY_ENTRY = prohibited
```

A delayed Copy strategy is an information consumer. Resting entry orders surrender timing to an
informed counterparty and can miss correct signals while being filled on reversals. Taker cost is
an information toll; if the remaining alpha cannot pay executable price and current venue fees,
the signal is discarded.

FOK is used when full-size integrity matters; FAK is used only when partial positions remain
economically valid and bounded slices can be reconciled safely. Neither order type permits an
unbounded price. The BUY collar is the smaller of the allowed book move and the conservative fair
value remaining after hurdle/fees/risk. T checks the current VWAP for the requested size, book
generation, freshness, alpha deadline, and risk lease immediately before submission.

### 4.2 Exits are intentionally asymmetric

```text
emergency / hard-risk / near-critical-TTR exit -> aggressive price-bounded FAK
ordinary low-urgency reduction                -> optional short passive attempt, then FAK
new Copy entry                                -> never passive
```

Passive exits are allowed only after cancellation latency and post-fill markouts are measured,
away from scheduled catalysts, with fresh books and healthy execution. Local cancel remains a race;
the current venue's GTD security buffer cannot be treated as a sub-second expiry. Spread widening,
event-phase change, stale data, risk acceleration, or cancel-latency degradation stops passive
replacement and escalates urgency. TWAP is not a safety mechanism in a jump market.

### 4.3 No automatic Iceberg/TWAP capacity fiction

Client-side slicing does not create liquidity and may reveal repeated intent or allow alpha to
decay. It is useful only when measured alpha half-life materially exceeds the execution horizon,
book replenishment is genuine, and cumulative cost/markout beats immediate bounded execution.
Otherwise reduce size or skip. Strategy capacity ends where the conservative residual edge turns
non-positive; it is not determined by account balance or server size.

## 5. Market quality and honest valuation

### 5.1 Market quality is size-specific

Fixed universal rules such as `spread > $0.05`, `$50` depth within an arbitrary percentage band,
or daily volume below `$1,000` are rejected as final policy. They may be initial shadow features or
stress buckets, but price region, intended size, category, TTR, quote survival, and actual exit
capacity matter.

The gate uses at least:

```text
entry capacity at/better than the maximum BUY price
exit capacity at/better than the emergency SELL floor
entry and exit fill/liquidation ratios at actual size
spread relative to conservative gross edge and remaining payoff room
quote age, depth survival, book generation/quality flags
time to event/resolution and expected capital holding time
```

Failed markets receive `REJECT_ILLIQUID_AT_SIZE` and a versioned temporary quarantine/TTL, not a
permanent accusation. Historical volume is only a prior; it is not executable liquidity.

### 5.2 Risk uses liquidation equity

Mid and last-trade marks may be research views. Drawdown, capital, and scale gates use size-aware
executable liquidation value:

```text
research/model equity = cash + model/mid marks
liquidation equity    = cash + executable exit proceeds at actual position sizes
liquidity discount    = research equity - liquidation equity
```

Missing bids, dust depth, stale/crossed books, and positions requiring resolution rather than a
credible exit are explicit. A small trade that moves the last price does not create realizable PnL.

## 6. Paper, pre-live, and live truth

Paper mode reports optimistic, latency-adjusted, persistence-qualified, and stress PnL separately;
the conservative result is the headline. It cannot know exact operator priority or prove live fill
probability. All no-fills, rejects, no-submits, gaps, and missing volatile periods remain in the
coverage denominator.

Pre-live may build and sign exact order-shaped payloads locally without posting them, benchmark the
real SDK/signature path, warm static metadata and connections, and inject auth/rate/timeout faults.
It cannot reproduce matching priority, authenticated production throttling, or settlement outcome.

Do not cache a pool of fully signed executable orders. Cache the EIP-712 domain/type data, signer
context, token/tick/fee/negative-risk metadata, HMAC setup, and warm HTTP/TLS state; fill the exact
price, size, salt/timestamp and metadata only after decision, then sign and recheck the deadline.
Sub-millisecond signing is an evidence-gated SLO, not an assumption.

Current CLOB V2 ordinary orders do not use a locally incremented per-order nonce; API-credential L1
nonce and CTF Relayer transaction state are separate concerns. Ordinary order readiness checks
confirmed balance, allowance, reservations, credentials, local rate budget, and reconciled order
state. Matched trades remain provisional until confirmed settlement as specified in the companion
architecture record.

Local priority/rate lanes protect cancel and exit from entry/research retry storms. A throttled or
429 entry is not blindly retried because its alpha may have expired; it is rebuilt only after a full
fresh decision. Exit/cancel ambiguity receives priority reconciliation. Venue limits are versioned
configuration, not hardcoded folklore about guaranteed IP bans.

A later micro-live canary requires separate approval. Calibration is size-conditioned and advances
through a risk-controlled size ladder; results at `$10` never validate `$500`. Medium/large probing
is not an automatic random percentage of orders. Target size must lie within supported live
calibration, and every probe is a genuine risk-bearing order charged to a capped research budget.

## 7. Portfolio correlation and capital

### 7.1 Covariance is supplemental, not sufficient

Binary/event outcomes are nonlinear, sparse, and regime-dependent. Daily static return covariance
alone can miss logical and tail dependence. The accepted risk stack is:

1. exact/curated logical relations (`MUTUALLY_EXCLUSIVE`, `IMPLIES`, `SAME_EVENT`, common
   resolution source);
2. sparse factor exposure and cluster caps;
3. explicit scenario losses and liquidity shocks;
4. shrinkage covariance with version, sample/uncertainty, regime, and TTL; and
5. uncertainty/liquidity-adjusted fractional Kelly.

R prechecks a small sparse factor vector; S/T consume cached factor headroom and scenario limits
without calculating a dense matrix on the execution path. Stale/unknown dependence is conservative,
not zero. Several wallets expressing the same event thesis are not independent diversification.

### 7.2 CTF pairs are potential collateral, not free cash

For a standard binary condition, confirmed unreserved equal YES/NO quantities are mergeable. Use
an O(1) token -> condition registry and condition bucket; maintain confirmed balances, provisional
balances, SELL reservations, merge reservations/inflight, mergeable complete sets, and residual
directional quantities incrementally. Periodic full reconciliation remains off the exit path.

Only a confirmed Merge increases spendable cash. Provisional/matched pairs do not release buying
power. Standard conditions, negative-risk event conversion, and augmented negative-risk outcomes
are different contracts/graphs; semantic mutual exclusion alone never authorizes cross-market
netting.

### 7.3 Merge uses a cash-band and EV activation gate

The capital snapshot separates confirmed spendable cash, operational reserve, low/high cash
watermarks, confirmed mergeable collateral, merge inflight, forecast demand, completion latency,
route cost/failure, pair alternative value, and merge-EV lower bound.

Merge is considered only when confirmed cash falls below a low watermark and can refill toward a
high watermark without churning. The confirmed complete-set amount must exceed a minimum batch,
have no conflicting reservation, the ledger/Relayer must be healthy, and the lower bound of
incremental Merge EV must exceed an activation margin. The comparison is complete-pair Merge versus
holding/redeeming, separately selling both legs, eligible rewards, or keeping usable inventory—not
the isolated future upside of one leg.

A fast Copy signal whose remaining TTL is shorter than p95 Merge confirmation plus margin is
rejected for unavailable cash. A slow chain action cannot rescue decayed alpha.

## 8. TTR, toxicity, and settlement

TTR is a nonlinear independent risk dimension, not merely generic volatility. Use category/event
phase plus source freshness rather than trusting `endDate` as exact resolution. A versioned risk
lease carries event phase and entry-not-after deadline; stale/unknown/critical phases fail closed
for entries while cancel/reduce/exit remain available. A future resolution-arbitrage strategy
requires a separate model and risk budget and cannot bypass Copy policy.

Order toxicity uses classified FOK outcomes and controlled side-adjusted markouts at several
horizons. Reject-plus-adverse-move does not prove spoofing, operator front-running, or VIP priority.
Temporary reduction/shadow/quarantine requires samples, uncertainty, controls, TTL, and hysteresis.

Economic exposure is counted conservatively from `MATCHED`; only confirmed settlement updates the
final settled ledger. `RETRYING`/unknown capital stays frozen and `FAILED` is reversed with an
append-only reconciliation event.

## 9. Expansion decisions

Expansion is evidence-gated depth before breadth. New bots/services must not be mixed into this
Copy Bot's code, ledger, PnL, credentials, or deployment. A common language-neutral market-data,
intent, execution, risk, settlement, and audit protocol may be shared; strategy state remains
isolated.

### Phase 0 — prove current Copy economics

- residual-alpha and source-intent attribution;
- size-aware market quality and liquidation PnL;
- conservative replay and real-burst safety;
- separately approved micro-live calibration at actual `$3-$10` size; and
- capacity curve showing where lower-bound net alpha reaches zero.

No expansion compensates for failure at this gate.

### Phase 1 — wallet intelligence

Upgrade actions into wallet-conditioned posteriors, decay, intent, impact, and signal-cluster
evidence. This has the highest near-term reuse of existing data and infrastructure.

### Phase 2 — event/payoff knowledge graph and internal scanner

Build market resolution, outcome, implication, mutual-exclusion, CTF/negative-risk relation, and
scenario-payoff data. Separate exact logical arbitrage from statistical relative value. Start
shadow-only.

Multi-leg/batch submission is not assumed atomic. Every package reserves all capital and prices
orphan-leg worst loss, unwind depth/fees, maximum inter-leg time, and rule mismatch before the first
leg. Headline win rate never replaces tail-loss analysis.

### Phase 3 — one niche primary-source vertical

Do not enter a generic public social-media/Bloomberg latency race from a public-Internet Python
host. Choose one allowlisted primary-source domain whose information is difficult to interpret and
whose lower-bound alpha half-life exceeds pipeline p99 by a safe margin. NLP/LLMs perform untrusted
schema extraction, entity/rule mapping, translation, contradiction detection, and research; they
never possess order authority or bypass deterministic/risk checks.

### Phase 4 — package/leg-risk execution

Only after Phase 2 shadow evidence: package state machine, all-leg capital reservation, orphan
hedge/unwind policy, and worst-case scenario limits. Cross-platform positions are fully funded on
each venue; there is no assumed atomicity, shared collateral, common resolution, or prime-broker
netting.

### Phase 5 — central strategy allocator

Only independently calibrated strategies receive separate capital/risk budgets. Allocation uses
net edge, capacity, liquidity, common factors, shared venue/infrastructure failure, scenario loss,
and drawdown—not an equal split or the number of bot names. Copy, relative value, and liquidity
provision can fail together during an information/liquidity shock.

### Phase 6 — liquidity provision and external hedging

Liquidity provision is a separate business with spread/rebate revenue, adverse-selection,
inventory, cancel-latency, and hedge attribution. It is not an execution feature of Copy Bot.

External leveraged hedging is last. Fully collateralized prediction positions and mark-to-market
perpetual margin create cash-flow, basis, funding, counterparty, and liquidation mismatch. A hedge
does not automatically justify a larger Kelly fraction. Prefer directly provable same-condition/
same-event payoff relations; any later external hedge requires independently prefunded margin,
low/no leverage, stable causal beta, severe stress survival, and its own kill switch.

## 10. Explicitly rejected or unproved assumptions

- A followed wallet is always informed or every BUY is directional.
- Observed stopping price equals the wallet's true target.
- More than a universal number of milliseconds makes every signal worthless.
- Maker fills are literally `95%`/`99%` toxic without measured markouts.
- A Maker entry can rescue negative Taker EV.
- Mid/last price is realizable PnL in a thin market.
- Fixed spread, daily-volume, or percentage-depth thresholds are universally correct.
- Daily static covariance alone controls binary-event tail dependence.
- All mutually exclusive markets can be CTF-netted.
- `MATCHED` means settled or provisional pairs are spendable cash.
- Every Merge necessarily charges the user Polygon gas; route and Relayer state matter.
- Current CLOB V2 requires a locally incremented nonce or gas check before every order.
- Caching fully signed executable orders is a safe latency optimization.
- Batch submission means atomic multi-leg execution.
- TWAP/client-side slicing creates capacity.
- Tiny Canary fills validate materially larger order sizes.
- Paper PnL alone proves live execution alpha.
- A generic Twitter sentiment bot on the current host can beat institutional news latency.
- Multiple strategies or an external hedge automatically diversify risk or permit larger Kelly.
- A language rewrite, AWS resize, colocation, or more capital creates edge without evidence.

## 11. Implementation truth and next evidence

This file is a decision record, not a feature list. As of 2026-08-06:

- Copy Bot is paper-only and its live AWS process does not enforce the proposed residual-alpha,
  factor/scenario, size-aware market-quality, TTR, settlement-finality, or Taker-only-live policies;
- no private-key execution path, micro-live Canary, C++ market-data process, multi-leg engine,
  primary-data strategy, cross-platform strategy, LP strategy, external hedge, or allocator is
  authorized or deployed; and
- the local, feature-disabled walking skeleton now records observed position action separately from
  unknown economic intent; reported source-to-receive time without inventing poll/clock bounds;
  actual-copy-size entry VWAP, current same-request fee metadata, chase, visible two-sided depth,
  immediate executable liquidation, wallet/model version fields, event/category factor IDs, and
  exact shadow gate traces;
- the standalone Phase 0 soak path now preserves economically distinct incremental wallet fills,
  BUY and SELL signals, source-position reduction, conservative per-entry shadow tax lots,
  executable bid-side exits, realized versus unrealized PnL, and online T+100/T+500 book evidence;
  it uses the WS state already known at signal ingress and never relabels later REST state as T0;
- source share accounting prefers the raw reported token `size`; the fee-affected cash/price
  calculation is a labeled fallback. The source-reported timestamp difference remains a visibility
  proxy only, and absent public exchange sequence IDs remain absent rather than synthesized;
- its wallet probability is explicitly a point observation only. The conservative residual-alpha
  lower bound, scenario model, source-impact model, calibrated decay bounds, settlement discount,
  and capacity curve remain `unknown`/unimplemented rather than silently zero; and
- the in-process passive-shadow seam still covers only BUY signals that reach that seam. The
  standalone public recorder independently captures observed BUY/SELL wallet activity but still
  cannot prove fill priority, complete causal order, or foregone-EV for every production reject.
  These paths must not be merged into a false claim of complete counterfactual coverage.

The next evidence step is to collect and replay this bounded schema, quantify coverage gaps, and
calibrate the missing conservative models without increasing the current `$3-$10` size. This local
implementation cannot submit an order and has not been activated or deployed.

## Current official venue references

- CLOB model/authentication: <https://docs.polymarket.com/trading/overview>
- Orders/FOK/FAK/GTD: <https://docs.polymarket.com/trading/orders/create>
- Order lifecycle/settlement: <https://docs.polymarket.com/concepts/order-lifecycle>
- Current fee model: <https://docs.polymarket.com/trading/fees>
- Current API limits: <https://docs.polymarket.com/api-reference/rate-limits>
- CLOB V2 migration/order fields: <https://docs.polymarket.com/v2-migration>
- Conditional Token Framework: <https://docs.polymarket.com/trading/ctf/overview>
- Merge: <https://docs.polymarket.com/trading/ctf/merge>
- Negative risk: <https://docs.polymarket.com/advanced/neg-risk>
- Gasless Relayer operations: <https://docs.polymarket.com/trading/gasless>

## 12. Phase-0 virtual matching decisions — 2026-08-06

The offline virtual CLOB now evaluates the documented nonlinear taker-fee formula independently at
every consumed book level. It does not use one average price or deduct one fee after walking the
whole book. Fixed-point cash, price, and share calculations use 1e-6 units; simulated fill amounts
are floored at that unit and fee is floored to the declared 0.00001-USDC precision. These rounding
rules remain a versioned research assumption until compared with venue/SDK golden vectors.

Two BUY cash interpretations remain explicit and must not be conflated:

- `ORDER_NOTIONAL` sizes the displayed order notional and attributes fee at match, consistent with
  the current documentation boundary that fees are not part of the submitted order amount.
- `ALL_IN_CAPITAL` embeds every marginal fee inside a conservative capital budget. It is useful for
  treasury/risk planning but is not evidence that the venue itself would reject the corresponding
  FOK for insufficient fee cash.

Missing fee rates stay unknown, not zero. Exact live acceptance also depends on deployed SDK/order
precision, tick size, signing fields, matching, and fee application; the research engine therefore
remains a displayed-depth upper bound.

Aggregate one-second public trade tape is rejected as fill ground truth because it combines other
participants, queue position, cancels, and replenishment. A `<5%` readiness gate requires
attributable micro-live fills or official/venue-version golden vectors. No qualifying corpus exists
in the local workspace yet, so validation is `FAIL_CLOSED`, not passed.

Signal clustering retains both adjacent-gap and maximum-diameter caps. The proposed message-rate or
volume-time cascade boundary is not estimated from signal-triggered T0/T+ checkpoints because they
are not a continuous market-message series. It requires monotonic-time message buckets, a declared
rolling baseline, and a hard maximum diameter even after adaptive termination is introduced.

Open lots that cross an observed event-membership change receive `TOPOLOGY_RISK`; open event
topologies remain on `MUTABLE_TOPOLOGY_WATCH`. No arbitrary numerical uncertainty haircut is
applied while executable MtM and a calibrated haircut policy are both unavailable. The correct
current action is to freeze the valuation claim and surface the evidence gap prominently.

Weather research/model code remains a separate project and is not reintroduced into this Copy Bot
repository or deployment.
