# Lean Shadow Recorder and Replay Architecture — 2026-08-06

Status: Phase A polling vertical slice, minimal Phase 0 attribution, and a standalone public-WS
soak recorder were implemented locally on 2026-08-06. The recorder is paper/read-only and awaits
the controlled AWS service deployment recorded below. The compact schema, bounded JSONL writer,
virtual-clock replay, passive signal-time measurement, exact gate trace, size-aware entry/exit
observation, conservative tax-lot shadow ledger, recoverable BUY interlock, and persistent
malformed-risk panic path exist. No C++ service or live order path is running.

Implemented files:

- `shadow_replay.py`: v1 envelope, integer BBO/top-three/`$3/$5/$10` VWAP features, four named
  causal checkpoints, non-blocking bounded writer health, deterministic replay, and an exact
  blocking/observation gate trace.
- `phase0_attribution.py`: pure, fixed-point actual-size entry/exit observations, current fee and
  chase evidence, wallet/model and sparse event/category context, and explicit unknowns for every
  uncalibrated lower-bound input. It has no network, DB, key, clock, or order dependency.
- `phase0_soak.py`, `phase0_soak_recorder.py`, and `inspect_phase0_soak.py`: pure incremental
  conviction/tax-lot state, bounded public wallet/WS collection, online delayed observations, and a
  streaming coverage/resource report. The recorder imports no signing or order module.
- `entry_interlock.py`: pure immediate-trip/hysteretic-recovery state machine.
- `passive_integrity.py` and `shadow_capture.py`: signal-triggered queue/book-age measurement and a
  real polling signal -> current REST book -> replayable shadow-event adapter, with no network or
  order dependency of their own.
- `risk_manager.py`, `bot.py`, and `db.py`: the persisted interlock value is recognized by the
  existing sole BUY gate and decision journal; malformed core local state persistently hard-kills
  entries, invalidates delayed BUY intents, preserves SELL exits, and alerts an operator.
- `polymarket_simulator.py`: the current direct REST adapter now preserves the server book
  timestamp, book hash, fee-enabled state, and fee rate needed by shadow attribution.

The in-process polling-path producer is implemented but intentionally defaults off pending burst
testing. The separate soak process does not activate that producer or change the trading process.
It passively derives signal queue age and book freshness at decision time without a timer or
duplicate REST request. Recorder CPU/RSS, AWS CPU credits, a public WebSocket shadow process, and
full exchange-sequence reconciliation still need Phase B implementation. A disabled gate must not be
described as operational protection.

## Canonical scope and design-history coverage

This is the single detailed handoff for the full architecture discussion, from the first
deterministic-journal proposal through the process-isolated design. Later agents must not infer
implementation merely because a design appears here. Every section labels the present boundary,
the proposed boundary, and the evidence needed to cross it.

Strategy, execution-policy, portfolio-risk, and expansion conclusions that were agreed after this
architecture discussion are canonical in
`docs/research/STRATEGY_AND_EXPANSION_DECISIONS_2026-08-06.md`. This file remains authoritative for
journal/replay, process isolation, settlement/liquidity evidence, and Paper-validation mechanics;
the strategy record remains authoritative for what may consume that evidence.

The design history covered here is:

1. raw event journal, virtual clock, deterministic replay, and look-ahead prevention;
2. official-SDK facade/shadow comparison and version isolation;
3. public WebSocket heartbeat, silent-disconnect detection, REST resync, generation/sequence race;
4. Telegram human approval as a labeled research dataset, not a permanent strategy dependency;
5. copy-alpha attribution including source-whale market impact and rejected-trade counterfactuals;
6. external environment, process lag, network jitter, RPC/chain applicability, and clock honesty;
7. `t3.small` CPU-credit/RAM/GC constraints, degradation ladder, BUY kill switch, and storage I/O;
8. passive safety measurement, exact raw-ingress boundary, and real-corpus burst benchmark;
9. Python GIL/GC limits and the measured path toward C++/separate processes;
10. M/T/J/S ownership, cross-platform IPC, coalescing, SPSC journal egress, and risk leases;
11. canonical fixed-point precision and C++/Python golden vectors;
12. capital shadow cost, observer-safe early rejection, ambiguous order reservations, and
    reconciliation;
13. honest user-space RTT versus optional kernel timestamping; and
14. evidence-gated rollout, failure matrix, and explicit non-goals.

Domain-specific code, schema, dependencies, or research belonging to separately maintained bots
must not be reintroduced here. The generally applicable conclusions retained here are limited to
portfolio capital scarcity, model/version replay, and execution-system safety.

## Objective

Measure whether source-wallet signals retain positive **net copy alpha after observable friction**
before investing months in a full-depth replay platform. The first implementation must collect
enough causal data for future attribution while remaining bounded on the current AWS host and
never delaying the existing Copy Bot.

Success is not message throughput, low latency in isolation, number of followed wallets, or a
large gross source PnL. Success means a statistically credible positive distribution of **net
copyable alpha** at our actual size after source impact, public detection delay, local delay,
spread, fees, fill uncertainty, exit mismatch, and capital opportunity cost. If shadow evidence
rejects that hypothesis, the correct result is an early pivot to slower/high-conviction research.

## Deterministic journal and replay contract

The journal is the first dependency of attribution, not an afterthought. Raw input, normalization,
decision, intended action, execution observation, and reconciliation are separate causally linked
events. An event stores both wall time for external correlation and monotonic time for local
ordering. Timestamp precision does not imply accuracy; clock source and uncertainty travel with
the record.

Minimum invariants:

- raw payload is retained before normalization with exact source bytes/text or an explicit
  reconstruction flag;
- receive ordering is immutable and duplicate/idempotency keys are stable;
- `correlation_id` links signal -> checkpoints -> decision -> attempt -> fill/reject -> capital
  release, while `causation_id` records the immediate parent;
- code commit, config hash, roster version, policy/model version, schema version, and environment
  snapshot are captured at the decision boundary;
- missing book, sequence gap, fallback, stale state, parse failure, and journal loss are data, not
  silently repaired history;
- replay advances a virtual monotonic clock in receive order and rejects time regression;
- a decision may use only events causally visible by its virtual time; later REST snapshots,
  fills, resolutions, and operator actions cannot leak backward;
- original inter-arrival timing is replayable at 1x; controlled 5x/10x acceleration changes only
  arrival pressure, not causal order;
- deterministic golden records produce the same decision digest or fail with an explicit schema/
  policy incompatibility.

Raw JSON alone is insufficient if its framing, receive time, source sequence, loss boundary, and
runtime/config context are absent. Conversely, logging every full-depth object is not justified if
it harms execution; bounded raw frames and explicit gaps are scientifically superior to a recorder
that silently delays the trader.

## Verified production constraint

Read-only EC2 measurements at 2026-08-06 16:35 HKT:

- Instance: AWS `t3.small`, x86-64, 2 vCPU, 1.9 GiB RAM, no swap.
- Memory: 994 MiB used, 910 MiB available at the sample; only 83 MiB completely free because Linux
  appropriately uses page cache.
- Copy Bot PID `83881`: approximately 96 MiB RSS, 3.2% CPU, 19 threads.
- Host load average: 0.11 / 0.18 / 0.18 at the quiet-time sample.
- Disk: 30 GiB total, 18 GiB used, 13 GiB available; `data/app.db` is already 4.6 GiB.
- CPU credit balance and burst-time event-loop lag were not available from this shell sample and
  must be added to CloudWatch/Prometheus before a sustained WebSocket capture rollout.

The quiet-time numbers do **not** disprove the capacity risk. A burstable instance can look idle
most of the day and still be throttled at the exact news-driven moment when message rate and copy
value are highest.

## Decisions and disagreements

### 1. Do not capture full depth continuously on the current host

The initial recorder captures only tracked, challenger, and held markets. Its fixed-width market
features are more useful for the current `$3-$10` sizing than an unbounded Python object graph:

- best bid and ask, spread, server book timestamp and book hash;
- top three levels on each side;
- cumulative visible depth and executable VWAP for `$3`, `$5`, and `$10`;
- source trade price/size/side and immediate post-source BBO;
- explicit quality flags for stale book, sequence gap, parse fallback, missing field, and REST
  fallback.

A bounded in-memory ring holds only the recent pre-signal window. When a followed-wallet signal
arrives, the recorder flushes that market's recent window and continues a bounded post-signal
window. Quiet unrelated markets do not receive continuous full-depth capture.

Timestamp precision must not be confused with timestamp accuracy. Store epoch milliseconds and
monotonic nanoseconds, but also store clock offset/uncertainty and receive ordering; public network
data cannot honestly reconstruct a whale's book one microsecond before execution.

### 2. The recorder must degrade before it can delay trading

The hot path must enqueue a compact immutable event into a bounded queue and return. Serialization,
compression, and disk writes belong to a separate bounded writer. Synchronous SQLite writes and
per-message `fsync` are forbidden in the WebSocket callback.

Degradation ladder:

1. `NORMAL`: BBO, top-three levels, fixed-size VWAP/depth features, and the signal-window buffer.
2. `PRESSURE`: BBO and fixed-size VWAP/depth features only; drop non-signal level deltas.
3. `CRITICAL`: signal, decision, latency, and data-loss/gap markers only. If the cause threatens
   decision/execution integrity — event-loop lag, stale market data, decision-queue age, sequence
   uncertainty, or loss of the minimum audit trail — engage the entry interlock immediately.

Every downgrade, queue overflow, dropped event, and recovery is journaled and exported as a
metric. Silent loss is not allowed. Initial queue/memory/CPU limits must be chosen by a replayed
burst benchmark rather than guessed from quiet-time averages.

Python cyclic GC is a risk to measure, not an assumed root cause. Compact tuples/arrays and bounded
queues may create little cyclic garbage; JSON allocation, callbacks, and logging may still cause
event-loop lag. Instrument GC pauses and allocation pressure before deciding whether a C++ hot
path is justified.

The degradation ladder has two separate safety outcomes:

- **Entry interlock (automatic, hysteretic):** rejects every new entry/BUY, cancels pending entry
  orders in a future live mode, and leaves SELL/reduce-only/closeout paths available. It clears
  only after lag, freshness, queue age, and audit continuity remain healthy for a configured
  recovery window.
- **Hard kill (persistent/manual review):** reserved for capital-loss limits, impossible position
  or order invariants, custody/execution uncertainty, or repeated failed recovery. Recorder disk
  backpressure alone must first drop optional capture; it does not deserve a permanent capital
  kill if market data, execution, and the minimum decision audit remain provably healthy.

This separation avoids both failure modes: blindly buying while the execution path is late, and
needlessly hard-killing a healthy trading path because an optional research payload could not be
written.

Observer-effect constraint: the current Python bot must not run a 10 ms watchdog. Source events
receive wall/monotonic timestamps immediately after the raw HTTP body completes and before
UTF-8/JSON decode; parse start/complete and post-normalization enqueue are separate checkpoints.
The BUY safety age runs from raw-body ingress to decision, so a GIL-bound decoder stall cannot hide
behind a young queue timestamp. A REST book uses the same opt-in boundary; effective freshness is
server age at receipt plus local monotonic residence. The producer enqueues only a lightweight
capsule; Decimal VWAP, canonical JSON, and envelope normalization happen in the writer. The writer
blocks on `queue.get()` when idle and the producer only calls `put_nowait()`. Low-cadence host
telemetry may be added separately, but it must not be confused with an active per-book safety
poller.

This boundary is application-space, not kernel packet-arrival time. It cannot reconstruct time
spent in a socket buffer before `response.read()` returns. More importantly, the current writer is
an isolated Python thread, not an isolated process, so deferred materialization can still contend
for the same GIL. This is acceptable only as a feature-disabled polling vertical slice. A public
WebSocket burst recorder must capture/replay real raw frames from an external injector and move
per-message decode/materialization behind a separate process or host before activation.

Panic semantics: current live BUY execution is FAK/market-style and does not intentionally leave
managed resting entry orders. Managed resting exchange orders are SELL exits. Therefore malformed
core risk state latches the hard kill and invalidates delayed local entry intents, but does not
issue a blind venue cancel-all that could remove protection and trap positions. Telegram explicitly
requires manual venue position/open-order reconciliation. Automatic authenticated reconciliation
can replace that manual step only after the OMS exposes trustworthy side/reduce-only ownership.

### JSON decoding is benchmark-gated, not a blind one-line substitution

The production payload corpus must benchmark Python `json`, `orjson`, and a typed decoder such as
`msgspec` on the actual EC2 architecture. Record p50/p95/p99 decode time, peak RSS/allocation rate,
event-loop impact, malformed-input behavior, and normalized-output equivalence.

`orjson` is a strong candidate because its native implementation can materially reduce decode
cost, but it is not assumed to be a drop-in replacement: bytes versus string output, Decimal/
datetime/default-hook behavior, non-finite numbers, key handling, wheel availability, and error
semantics must be contract-tested. Pin the chosen version and artifact. Filtering irrelevant
markets before full normalization and avoiding unnecessary object creation may matter more than
swapping decoders alone.

### 3. Self-inflicted latency is a first-class attribution term

For every signal, capture separate monotonic spans for:

- socket receive to parse complete;
- parse complete to strategy scheduled;
- event-loop scheduling delay;
- strategy/risk evaluation;
- queue wait;
- execution-quote request start, connect/TLS, first byte, and complete;
- hypothetical order-ready timestamp;
- REST/RPC/relayer latency only when that operation actually depends on it.

For `$3`, `$5`, and `$10`, attach book sequence/hash and data-quality flags to four executable
VWAP checkpoints:

1. `source_pre_trade_vwap`: last coherent book before the source fill, when causally available;
2. `signal_visible_vwap`: first coherent book visible after the source signal has parsed;
3. `decision_commit_vwap`: coherent book when risk/state checks finish and the order intent is
   committed;
4. `execution_vwap`: preview/acknowledgement/fill-time executable or realized VWAP.

The time and price difference between checkpoints 2 and 3 is the direct measure of latency
exposure inside our decision path; checkpoint 4 adds outbound network, exchange, and our own size
impact. Call these checkpoints, not "one microsecond before": monotonic nanoseconds provide
ordering precision, while the journal separately records the public feed's timing uncertainty.
Risk checks are not automatically waste merely because prices move while they run — attribution
must compare their protective benefit against their latency cost before optimizing them away.

This separates source market impact and public-network movement from local CPU pressure, Python
processing, logging, or a blocked event loop. Polygon base fee is relevant to on-chain
approve/split/merge/redeem paths, not automatically to every off-chain CLOB order.

### 4. Attribution data requirements belong in the first schema

The calculation/dashboard may come later, but the first event envelope must make these later
terms observable:

```text
source information alpha
- source execution impact already paid by the whale
- residual post-source market impact facing the copier
- signal/detection delay drift
- local self-inflicted latency drift
- spread, fees, and our incremental size impact
- exit mismatch
= net copy alpha
```

The stable envelope includes at least:

```text
event_id, schema_version, event_type, source
source_timestamp_ms, received_timestamp_ms, monotonic_ns
source_sequence, source_hash, resync_generation
correlation_id, causation_id
code_commit, config_hash, roster_version
environment_snapshot_id, quality_flags
raw_payload, normalized_payload
```

Raw input is recorded before normalization. Prices/sizes/times in the normalized layer use
language-neutral integer units where practical so a future C++ service can consume the same log
without reproducing Python float behavior. Schema evolution is versioned; Python pickles or
language-specific object dumps are forbidden.

Build one attribution "walking skeleton" immediately: one recorded signal must replay into the
same decision and produce the initial impact/latency decomposition. This prevents several days of
collecting a fast but scientifically unusable dataset.

### Source adverse selection and market impact

The source trader's headline alpha is not automatically copyable. A whale may consume the cheap
levels before the public fill becomes visible. The attribution model therefore separates:

```text
source information alpha
- impact paid by the source while sweeping the book
- residual post-source impact inherited by the copier
- signal/publication delay
- local parse/queue/risk delay
- spread and fee drag
- our own incremental impact
- fill and exit mismatch
- marginal capital shadow cost
= realized/counterfactual copy alpha
```

The journal cannot honestly claim to reconstruct the book one microsecond before a whale unless a
coherent pre-source book was actually observed. It stores the last causally available pre-source
generation, its age/uncertainty, the first post-signal generation, and executable depth at bounded
size tiers. Traders that routinely sweep multiple levels can be classified as operationally
uncopyable even if their own PnL remains excellent.

Rejected trades are essential but are computed without production observer damage: when resources
permit, the minimal signal/book/gate marker is retained; full EV, Kelly, and alternative-fill
counterfactuals run offline under the same historical policy/model version.

## SDK facade and order-authority boundary

The bot must not call a changing official SDK shape throughout strategy/risk code. It owns a small
internal order interface such as `prepare_order`, `submit_order`, `cancel_owned_order`,
`get_open_orders`, `get_recent_trades`, and `reconcile_positions`. A pinned official SDK adapter or
direct REST adapter implements that facade. Schema/API changes are confined to the adapter and
contract tests rather than spreading through risk and OMS logic.

Initial SDK evaluation is read-only/shadow where possible: compare normalized market metadata,
book, tick, order-construction bytes, rounding, and error semantics against the existing adapter.
Private keys and live submission do not enter the market-data recorder or replay process. An
official client being beta, archived, or replaced is an operational dependency event that requires
an explicit version bump and golden-vector rerun, never an unattended upgrade.

## Operator console as labeled boundary-condition data

Telegram approval/rejection is a temporary human-in-the-loop control, not permanent execution
alpha. Each action must record request ID, candidate/intent, model/rule/config version, evidence
shown, decision, timestamp, operator identity, and a structured reason code plus optional note.

The audit serves two purposes: preserve accountability for roster/capital changes and identify
boundary conditions the automated scorer does not yet model. It must not silently train on its own
past recommendations; later automation requires out-of-sample validation, class/selection-bias
checks, and an explicit rule change. Emergency risk actions remain human-authorized even if routine
candidate approval becomes automated.

## WebSocket/REST reconciliation boundary

Shadow capture can start before a complete production WebSocket decision path, but it must label
gaps from day one. The later authoritative book state machine uses a resync generation, buffers
WebSocket deltas during REST fetch, applies a compatible snapshot plus a contiguous post-snapshot
delta chain, and verifies sequence/hash before atomic publication. A late REST response from an
older generation is discarded.

If Polymarket does not expose a REST watermark compatible with the WebSocket sequence, the system
must not invent certainty. It uses hash/convergence checks and remains halted for BUYs when state
cannot be proven coherent. Shadow samples spanning an unclosed gap are excluded from alpha
estimation.

## Time-to-market rollout

### Phase A — minimum vertical slice

1. Define the compact event envelope and the minimum copy-alpha fields above.
2. **Implemented, feature-disabled:** one real polling source-signal -> BBO/VWAP -> hypothetical
   decision journal path using the current direct REST adapter and the same already-fetched book.
   The path now includes actual-size entry and projected immediate liquidation, same-request fee
   metadata, wallet/model context, event/category factor IDs, and an exact shadow gate trace.
   Uncalibrated signal-age bounds and residual-alpha LCB remain explicitly unknown.
3. Implement a minimal virtual-clock replay test for that one path before live collection.
4. **Partial:** passive queue/scheduling age, book freshness, dropped-event, writer error, and
   entry-interlock signals exist. Add low-cadence recorder RSS/CPU and CPU-credit observability,
   plus a captured burst benchmark; do not add a high-frequency active watchdog loop.
5. Benchmark `json`/`orjson`/typed decoding on captured payloads; do not change the production
   decoder until semantic-equivalence and burst-performance gates pass.

### Phase B — immediate shadow collection

1. Add public WebSocket input in shadow mode only; it cannot place orders or become the sole TTP
   price authority.
2. Capture BBO/top-three/fixed-size VWAP and signal windows under strict resource bounds.
3. REST-quote each real source signal and record hypothetical slippage without submitting an
   order.
4. Record all four causal VWAP checkpoints for `$3`, `$5`, and `$10` when data quality permits.
5. Run for 3-7 days while measuring data loss and host performance.

### Phase C — evidence gate

For each trader/category/size tier, report:

- source signal to receive latency;
- receive to hypothetical order-ready latency and its local/remote decomposition;
- pre-source, post-source, and decision-time executable VWAP;
- residual source impact, our projected incremental impact, spread and fee drag;
- fraction of signals with positive net copy alpha at `$3`, `$5`, and `$10`;
- p50/p95/p99 event-loop lag, queue age, recorder CPU/RSS, dropped samples, and CPU-credit trend.

If net copy alpha is already non-positive at small size before real execution uncertainty, stop
optimizing high-frequency copy for that trader/category and move research toward slower,
high-conviction signals. Do not build a perfect replay engine around a disproven opportunity.

### Phase D — replay and resilient market-data expansion

Only after Phase B proves the data path useful: expand deterministic replay, implement full
generation/buffer/snapshot reconciliation, then evaluate whether selected markets need deeper
book capture.

## AWS and C++ upgrade path

The schema and causal IDs are deliberately language-neutral so collection/normalization can later
move behind a C++ boundary without rewriting strategy, risk, or historical research.

Upgrade order:

1. Profile and replay a captured burst on the existing `t3.small`.
2. If CPU credits, memory, or event-loop SLOs bind, move the recorder to a separate fixed-
   performance instance or resize away from burstable compute before rewriting languages.
3. Migrate only the measured hot path — WebSocket decode, book maintenance, feature extraction,
   bounded ring, and journal framing — to C++ behind the same event contract.
4. Keep Python orchestration/research until profiling proves it is the bottleneck.

Bare metal and kernel bypass are not current assumptions. Public Internet/API latency and the
exchange's own matching/data path will usually dominate a few microseconds saved in the local
kernel. Consider those techniques only if measurements show host networking is material and an
exchange-proximate deployment opportunity actually exists.

## Proposed process-isolated protocol — design record, not implemented

This section records the 2026-08-06 engineering discussion so a future agent does not mistake a
proposal for deployed protection. None of Processes M/J/S, the binary protocol, or the public WS
path below exists in production yet.

### Process ownership

```text
public WS -> Process M (C++ market data)
                  |-> bounded decision-state stream -> Process T (Python strategy/risk/orders)
                  `-> lock-free SPSC -> journal egress -> Process J (prefer separate host)

Process S (risk supervisor) -> short-lived permission lease -> Process T
```

- **M is deterministic, not strategic.** It decodes the feed, maintains sequence/generation-aware
  books, and publishes objective BBO/top-three/VWAP state. It never decides whether a move is
  economically "material."
- **T owns trading semantics.** Trader scoring, materiality, Kelly/risk sizing, entry/exit policy,
  and authenticated CLOB order submission stay in Python until measurement proves a narrower hot
  path must move.
- **J is lossy before it is blocking.** Research capture may drop with an explicit sequence gap;
  it may never backpressure M or T.
- **S is not a synchronous per-order oracle.** T retains non-bypassable local hard limits and uses
  a cached, expiring lease from S. Loss of S fails closed for new exposure without blindly
  liquidating positions into an uncertain market.

### Cross-platform transport and coalescing

The first M -> T implementation uses Unix-domain `SOCK_STREAM`, not `SOCK_SEQPACKET`, because the
same behavior is required on the macOS development host and Linux production host. Every frame is
length-prefixed and includes at least magic, protocol version, message type, payload length,
source epoch, sequence/generation, local monotonic publish time, flags, payload, and checksum.
Readers and writers must handle partial I/O, reconnect epochs, maximum frame length, unknown
versions, fixed byte order, and broken pipes explicitly.

A socket is FIFO, not an overwriteable mailbox. M therefore uses **event-armed coalesced push**:
the first relevant book change arms a one-shot timer; subsequent deltas update the in-memory latest
state; expiry publishes one fixed-size snapshot and disarms until another change. There is no
permanent 1 ms polling wake-up. The coalescing window is an operational, benchmarked config, not a
strategy threshold.

If T falls behind, M's non-blocking writer may have one partially written frame plus one latest
pending frame. A partial stream frame must finish; only a not-yet-started pending state may be
replaced by a newer state. Each frame reports first/last source sequence and coalesced update count,
so T can halt on stale data and the journal can quantify lost resolution. A future shared-memory
latest-state mailbox is evidence-gated, not assumed necessary.

### Journal isolation

Parser -> journal egress uses a preallocated, fixed-capacity lock-free SPSC ring with one actual
producer and one consumer, acquire/release ordering, and cache-line-separated indices. No mutex,
condition variable, heap allocation, compression, or network write occurs in the parser enqueue.
If multiple producer threads are introduced later, each gets its own SPSC or the design must
explicitly change; silently turning SPSC into MPSC is invalid.

The egress owner handles TCP partial-write state and reconnects. Queue-full or oversize input drops
a complete frame before enqueue and increments counters containing source sequence/time/byte ranges.
The receiver also detects sequence gaps independently because a drop marker can itself be lost.

### Risk lease and unavoidable TOCTOU

Process S emits a lease containing risk epoch, strictly increasing lease sequence, monotonic expiry,
allowed-action mask, exposure ceiling, risk-state hash, and config version. T checks it before
strategy work and again inside the low-level transport adapter after order construction/signing and
connection acquisition, immediately before giving bytes to the OS.

No userspace design can eliminate the final scheduler race after the last check. Dispatch therefore
requires more than `lease_remaining > 0`: remaining lease time must exceed a safety margin derived
from measured p99.9 scheduling/transport delay. Risk-lease expiry, quote freshness, book generation,
and alpha/dispatch deadline are separate gates. Once bytes have entered the kernel they cannot be
reliably unsent merely because a lease expires.

S restart begins halted and must reconcile positions, open orders, recent trades, book generation,
and local reservations before issuing a fresh lease. Host-level failure additionally needs remote
monitoring and bounded-lifetime order semantics; S on the same host cannot solve whole-host death.

### Fixed-point contract

Protocol v1 uses one canonical scale, not an arbitrary per-message exponent:

```text
price_e6, share_size_e6, usd_notional_e6, tick_size_e6
```

Decimal feed strings parse directly to integers without binary float. Price/tick alignment uses
integer remainder; VWAP/notional multiplication uses `unsigned __int128` intermediates in C++ and
defined directional rounding. Token IDs remain 256-bit values (or a generation-scoped catalog key),
not `uint64`. A future venue precision beyond v1 is an explicit protocol upgrade; consumers reject
unsupported precision instead of silently rounding.

C++ and Python must pass the same golden vectors for decimal parsing, binary bytes, malformed
frames, overflow, VWAP, tick alignment, and BUY/SELL rounding before integration. The order adapter
constructs exact integer/rational maker/taker amounts; no float crosses the execution boundary.

### Observer-safe opportunity and capital evidence

Do not run the full Python EV/Kelly pipeline merely to log an opportunity that cannot be acted on.
Use an early rejection boundary:

1. Always preserve a compact raw source signal, BBO/book generation, receive order, gate state, and
   `dropped_due_to_capital`/risk reason when the bounded journal is healthy.
2. If free capital is zero or a hard/recoverable entry gate is closed, stop before CPU-heavy model,
   Kelly, or counterfactual calculations.
3. Reconstruct predicted gross/net EV and foregone EV later with deterministic offline replay and
   the historically correct policy/model/config version.

This deliberately trades immediate model output for exit-path safety and lower observer cost. Raw
capture is still bounded/degradable; if even the minimum marker threatens execution integrity it is
dropped with an auditable gap rather than delaying an exit.

Capital history stores primitives, not a stale `capital_velocity` scalar: capital snapshots,
candidate signals, reserve/increase/release/settle transitions, and actual release time. Capital
velocity and shadow cost are derived offline. A flat APY may remain a funding-carry baseline after
conversion to the actual holding horizon, but the full shadow cost depends on competing opportunity
arrival, fill probability, holding-time distribution, correlation, risk limits, and marginal
liquidity value.

### Ambiguous order state and reconciliation

Reservation state includes at least:

```text
reserved -> submitted -> filled | rejected | cancelled
                     `-> orphaned -> reconciled_filled | reconciled_released
```

An HTTP timeout means **unknown**, not rejected. The corresponding capital remains frozen and
high-risk; releasing it early can double-spend the risk budget. `holding_time_p90` describes a
position hypothesis and must not decide order finality. A separate low-cadence reconciliation loop,
owned by S or an order supervisor, performs idempotent authenticated queries of order ID/client
intent ID, recent trades, open orders, balances, and positions. Only corroborated venue state emits
the reconciled transition. Persistent ambiguity keeps entries halted and alerts the operator while
exits remain available subject to trustworthy position/book state.

### Honest latency names

Python can reliably compare timestamps only within its own monotonic clock domain. Record:

- userspace dispatch preparation start;
- connection-pool/TLS/request-library spans where exposed;
- call into socket/TLS write and return from that call;
- response first byte/complete and total local monotonic RTT;
- exchange match wall time as a separate reconciliation fact with clock-source/uncertainty.

Do not name `socket.send()` return `kernel_handoff` or NIC transmit: it proves only userspace
acceptance into the socket/TLS path. Do not subtract exchange wall time from local monotonic time.
Optional Linux `SO_TIMESTAMPING` or eBPF instrumentation can later estimate kernel/network spans,
but it is not necessary for the first scientifically honest attribution report.

Finally, CLOB order matching is off-chain and settlement follows on-chain. User-selected Polygon
priority fee is not assumed to drive FOK/FAK fill probability. Gas/base-fee observations are tagged
by execution path and used for settlement/RPC/relayer attribution only when causally applicable.

## Execution uncertainty and paper-validation consensus — 2026-08-06

Status: architecture/research decision only. The toxicity analytics, settlement-finality ledger,
depth-survival model, multi-scenario paper report, and micro-live calibration stage below are not
implemented or production-active. Copy Bot remains paper-only.

This section records the conclusions jointly accepted after reviewing off-chain matching priority,
provisional on-chain settlement, and disappearing L2 liquidity. It deliberately separates measured
risk from allegations that the available data cannot establish.

### Off-chain priority and order-toxicity evidence

The CLOB operator validates and matches orders off-chain, so public market data cannot reveal the
operator's complete internal arrival order or guarantee that our hypothetical order would have won
simultaneous liquidity. This is an execution-priority/latency uncertainty, not automatically
on-chain MEV. We have no evidence of operator front-running, a VIP tier, or spoofing, and a FOK
reject followed by an adverse move does not prove any of them.

A signed FOK limit order remains price-bounded: insufficient eligible liquidity should reject the
whole order rather than permit an arbitrary worse execution. The research problem is whether our
orders would fill at all and whether the fills available to us are adversely selected.

The future analytics owner (Q/J or another research process, not the synchronous risk supervisor)
must classify venue and local outcomes before estimating toxicity:

- FOK insufficient-liquidity reject;
- tick/price validation, balance, allowance, rate-limit, order-delay, or server reject;
- local risk/lease/freshness reject; and
- timeout or unknown finality, which is never silently relabeled rejected.

For genuine liquidity-race observations, record side-adjusted post-outcome markouts at 10, 50,
100, 250, 500, and 1,000 ms, requested size, arrival-book generation/depth, quote age, system
pressure, local latency, market type, time-to-resolution, and volatility regime. Compare rejected
orders against comparable signals for which no order was submitted; without this control group,
ordinary signal selection and fast markets can look like reject-induced toxicity.

Process S consumes only a versioned, expiring toxicity state. It does not fit the model. Automatic
handling is graduated and reversible:

```text
NORMAL -> REDUCE_SIZE -> SHADOW_ONLY -> TEMP_QUARANTINE
```

A market requires a minimum sample, uncertainty/confidence bounds, hysteresis, TTL, and controlled
shadow re-entry. A small sample never creates a permanent blacklist. Permanent exclusion requires
review and evidence, not a label such as `spoofer` inferred from price movement alone.

### Matched economic exposure versus settled finality

`MATCHED` is a non-terminal trade state. The accepted model follows the venue lifecycle:

```text
RESERVED -> SUBMITTED -> MATCHED_PROVISIONAL -> MINED_PROVISIONAL -> CONFIRMED_SETTLED
                               |                      |
                               `------> RETRYING <---'
                                           |
                                           `-> CONFIRMED_SETTLED | FAILED_REVERSED
```

The authenticated user WebSocket is the primary low-latency lifecycle feed, authenticated CLOB
REST queries provide idempotent reconciliation, and Polygon RPC receipts/block evidence provide an
independent audit. RPC is corroboration rather than a replacement for the venue lifecycle. The
system must not invent a contradictory confirmation rule without first specifying how it maps to
the venue's `CONFIRMED`, `RETRYING`, and `FAILED` states.

Risk and accounting use two simultaneous views:

1. **Economic exposure:** from `MATCHED`, reserve capital and count the full worst-case position,
   concentration, and loss exposure.
2. **Settlement finality:** only `CONFIRMED` moves the final ledger to settled. `RETRYING` remains
   frozen and high-risk; `FAILED` produces an append-only reversal/reconciliation event.

"Settlement discount" must never reduce exposure and release buying power. It is a valuation
haircut/risk surcharge and confidence flag while the capital remains unavailable. Any exit or hedge
that assumes provisional inventory is sellable must first pass the venue balance/allowance and
order-state gates; ambiguity halts related new exposure and alerts the operator.

Paper replay must model provisional duration, `RETRYING`, and `FAILED` through empirical scenarios
and fault injection. A simulated order does not become a real chain observation merely because the
replay assigns it a settlement path.

### Raw depth, surviving depth, and fill probability

M preserves objective bounded L2, sequence/generation, quality flags, and level-change evidence.
Liquidity-reward eligibility or short quote life is not, by itself, proof that a quote is fake.
Strategy/research code—not M and not the synchronous risk supervisor—estimates usable liquidity.

A fixed rule such as "quotes younger than 500 ms count 10%" may be a stress scenario but is not the
canonical model. The required quantity is nonlinear:

```text
P(cumulative eligible depth surviving until hypothetical arrival >= requested size
  | market, side, level age, distance to touch, size, volatility,
    spread, time to resolution, update/cancel history, and system latency)
```

FOK uses the probability that the entire requested size survives and is eligible at arrival. FAK
uses a fill-size distribution. A resting maker order uses conservative queue-ahead consumption and
must not count touch as a fill. Cancel-versus-trade attribution remains uncertain unless public
book changes are reconciled with the trade feed. Model versions and calibration samples travel
with every derived result.

### Paper PnL is a bounded estimate, not an asserted fill history

Every hypothetical decision freezes its causal inputs and assumptions: decision and simulated
arrival times, latency scenario, book generation, raw depth, quote/level age, fee policy,
depth-survival model version, fill probability, and data-quality/coverage state. Later information
may label the outcome but may not leak back into the decision.

The report produces at least four separate scenarios:

| Scenario | Fill assumption | Reporting use |
| --- | --- | --- |
| Optimistic upper bound | visible decision-time depth survives | diagnostic ceiling only |
| Latency-adjusted | book observable at simulated order arrival | baseline comparison |
| Persistence-qualified | only depth surviving the reaction/arrival window is eligible | primary conservative paper result |
| Stress | worse latency quantile, depth haircut, fees, rejects, outages, and settlement failures | scale/readiness gate |

The optimistic result is never the headline. For a hypothetical taker FOK, there is no maker-style
"we are first in queue" assumption: replay requires sufficient eligible depth at simulated arrival.
Even then, public data cannot identify the operator's exact internal priority, so the result is a
calibrated probability or an upper/lower bound rather than a deterministic fill.

```text
expected_net_pnl
  = P(fill | market, size, latency, regime) * net_pnl_if_filled
    - settlement_failure_cost
    - measured execution/risk costs
```

Rejected/no-submit opportunities, local gate rejects, journal gaps, and missing high-volatility
windows remain in the denominator and coverage report. If the apparent profit is concentrated in
periods without adequate critical-data coverage, no positive strategy claim is allowed.

Calibration and evaluation use different time/market samples with walk-forward testing. Required
evidence includes fill-probability calibration (reliability/Brier or log loss), confidence bounds,
net EV after fees and observable friction, drawdown/concentration, and stress at larger latency,
depth haircuts, 429/API outage, WS resync, system pressure, and settlement retry/failure regimes.

### Scale gate

Paper mode can reject a strategy but cannot by itself prove execution alpha, operator priority, or
exact live fill probability. After conservative paper evidence is positive, a separately approved
micro-live canary stage—with minimum notional, strict order/daily loss caps, and no automatic
scaling—must compare actual accepts/rejects, fills, markouts, and settlement transitions with the
paper model's predicted intervals. Capital and AWS capacity increase only after that agreement is
demonstrated. Until explicit approval of that later stage, no live order or private-key custody is
authorized by this decision record.

Current official lifecycle references used for this decision:

- <https://docs.polymarket.com/concepts/order-lifecycle>
- <https://docs.polymarket.com/trading/orders/overview>
- <https://docs.polymarket.com/market-data/websocket/user-channel>
- <https://docs.polymarket.com/market-makers/liquidity-rewards>

## Phase 0 soak implementation — 2026-08-06

Status at this checkpoint: implemented and locally network-sanity-tested; paper/read-only. The AWS
service is deployed only when the later handoff entry explicitly records its commit and service
state. This section distinguishes implemented evidence from future inference.

### Distinct fills and incremental conviction

Production Copy Bot dedup is unchanged. The research recorder uses an isolated fill surrogate over
the Data API identity plus price and cash notional because the public activity response does not
document a unique fill ID. Only an identical research fill surrogate is idempotent. Separate fills
from the same wallet, market, outcome, and side remain separate observations and update
`wallet_recent_trade_count_1h` and recent notional. Repeated scaling-in is therefore evidence of
conviction rather than an accidental duplicate.

This is still an observation, not an automatic size multiplier. A one-hour feature collected for
less than one hour is explicitly marked window-incomplete, and restart restoration rebuilds it only
from retained journal history.

### SELL lifecycle and conservative tax lots

Each `$3/$5/$10` shadow tier stores individual entry lots, never one blended aggregate cost basis.
The default replay policy is `worst_execution_first`; deterministic FIFO and LIFO alternatives are
available for sensitivity analysis. A source SELL first computes the reduction fraction of the
source inventory observed by this recorder, then applies only that fraction to shadow lots the bot
actually acquired. A SELL with no observed source inventory or no shadow lots produces no realized
PnL. Unfilled bid-side quantity is never treated as sold.

Source token quantity prefers the untouched Data API `size` field. `usdcSize / price` is only a
labeled fallback because the cash field can include fee effects and would distort the partial-SELL
ratio. Every lifecycle event records `source_share_basis`, lot IDs, shares closed, cost basis closed,
executable bid proceeds, and realized PnL. Open lots remain unrealized; source and shadow books are
not mixed.

### Honest signal and book time

The append-only parent `wallet_signal_ingress` is written before any REST enrichment. The decision
book is the public-WS state already known at that monotonic ingress instant. A book received after
ingress, a stale locally received book, or no warmed book cannot be back-dated as T0 and causes the
shadow lifecycle to skip. REST supplies fee/event metadata and its own latency only; its later book
is not presented as the signal-time book.

T+100 ms and T+500 ms tasks are armed immediately from the same local monotonic ingress deadline.
Every observation stores target time, actual capture time, lateness, reconnect epoch, local
generation, server timestamp/hash when supplied, and whether the latest captured generation was
already known by the target deadline. The comparison is against the target, not the delayed callback
time. If Python/GIL/host scheduling wakes the task late, that lateness remains visible rather than
silently shifting the simulated arrival.

The public market WS payload used here does not provide an exchange sequence ID compatible with the
wallet activity stream. `exchange_sequence_id` is therefore explicitly null. Local generation and
reconnect epoch prevent accidental reuse across a reconnect but do not prove blockchain-to-book
causality. Offline replay may use conservative timing intervals or exclude ambiguous samples; it
must not invent a causal join.

Likewise, `first_local_seen - source reported timestamp` is stored only as
`reported_visibility_lag_ms` with status `source_timestamp_semantics_not_documented`. It is useful
for wallet/market cohort comparison but cannot, by itself, prove private RPC use, MEV routing, or the
true off-chain match age and cannot automatically quarantine a wallet.

### Resource and operational boundary

The service polls the current tracked/challenger/retiring roster at a low cadence, bootstraps only
one Data API page per wallet, subscribes to a bounded asset LRU, keeps full L2 only in memory, and
writes signal-sized top-ten JSONL. It owns no key and has `order_capability=false`. Production
bootstrap records zero historical samples; the optional sample/warm-up flags exist only for an
explicit local sanity run.

The Linux service contract adds a 768 MB address-space limit, systemd `MemoryMax=1G`, 50% CPU quota,
low CPU/I/O priority, protected system paths, and one writable data directory. This limits research
damage on the current `t3.small`; it does not prove burst safety. The streaming inspector reports
malformed lines, file size, poll gaps/errors, delayed-book availability/lateness, reconnects,
quality flags, sides, visibility-lag proxy, and realized shadow PnL without loading the journal into
memory.

Local live-public-data sanity (`bootstrap_sample_count=1`, warm-up only) observed one warmed asset,
one source SELL, no poll error, both delayed books available, T+100 capture lateness 0.860 ms, T+500
lateness 1.239 ms, and a signal-time WS book age of 8.574 ms. This validates plumbing only. It is not
strategy evidence, a PnL claim, or permission to trade.

## Non-goals for the first release

- No private keys or live execution in the recorder.
- No continuous all-market full-depth archive.
- No claim of microsecond market-state accuracy from public feeds.
- No automatic trader promotion from shadow data.
- No C++ rewrite before a recorded profile identifies a Python hot-path bottleneck.
