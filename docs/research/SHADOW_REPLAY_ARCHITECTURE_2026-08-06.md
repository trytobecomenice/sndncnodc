# Lean Shadow Recorder and Replay Architecture — 2026-08-06

Status: architecture decision / research plan only. No recorder, WebSocket execution path, AWS
resize, or C++ service is production-active from this document.

## Objective

Measure whether source-wallet signals retain positive **net copy alpha after observable friction**
before investing months in a full-depth replay platform. The first implementation must collect
enough causal data for future attribution while remaining bounded on the current AWS host and
never delaying the existing Copy Bot.

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
2. Implement one source-signal -> BBO/VWAP -> hypothetical decision journal path using the current
   direct REST adapter.
3. Implement a minimal virtual-clock replay test for that one path before live collection.
4. Add event-loop lag, queue age, recorder RSS/CPU, dropped-event, entry-interlock, and CPU-credit
   observability.
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

## Non-goals for the first release

- No private keys or live execution in the recorder.
- No continuous all-market full-depth archive.
- No claim of microsecond market-state accuracy from public feeds.
- No automatic trader promotion from shadow data.
- No C++ rewrite before a recorded profile identifies a Python hot-path bottleneck.
