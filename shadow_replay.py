#!/usr/bin/env python3
"""Lean, deterministic shadow journal/replay walking skeleton.

No function in this module can place an order. It converts an already-read
public order book into compact integer features, records exact raw signal
text beside normalized fields, and replays those records through one small
shadow BUY policy using a virtual monotonic clock.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import queue
import threading
import time


SCHEMA_VERSION = 1
POLICY_VERSION = "shadow-buy-v1"
PRICE_SCALE = 1_000_000
SHARE_SCALE = 1_000_000
USD_SCALE = 1_000_000
VWAP_TIERS_USD = (3, 5, 10)
UNSAFE_BOOK_FLAGS = frozenset({"missing_book", "sequence_gap", "stale_book"})
CHECKPOINT_NAMES = (
    "source_pre_trade",
    "signal_visible",
    "decision_commit",
    "execution",
)


def _scaled_int(value, scale, field_name):
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return int((decimal_value * scale).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class BookLevel:
    price_micros: int
    size_micros: int


@dataclass(frozen=True)
class VwapFeature:
    requested_usd_micros: int
    filled_usd_micros: int
    shares_micros: int
    price_micros: int | None
    insufficient_liquidity: bool


@dataclass(frozen=True)
class BookCheckpoint:
    checkpoint: str
    received_timestamp_ms: int
    monotonic_ns: int
    book_timestamp_ms: int | None
    source_sequence: str | None
    source_hash: str | None
    resync_generation: int
    best_bid_price_micros: int | None
    best_ask_price_micros: int | None
    top_bids: tuple
    top_asks: tuple
    buy_vwap: tuple
    quality_flags: tuple

    def __post_init__(self):
        if self.checkpoint not in CHECKPOINT_NAMES:
            raise ValueError(f"unsupported checkpoint: {self.checkpoint!r}")

    def vwap_for_usd(self, usd):
        requested = int(usd * USD_SCALE)
        for feature in self.buy_vwap:
            if feature.requested_usd_micros == requested:
                return feature
        return None


def _normalize_levels(levels, reverse):
    normalized = []
    for raw_price, raw_size in levels:
        price = _scaled_int(raw_price, PRICE_SCALE, "price")
        size = _scaled_int(raw_size, SHARE_SCALE, "size")
        if price <= 0 or price > PRICE_SCALE or size <= 0:
            continue
        normalized.append(BookLevel(price, size))
    return tuple(sorted(normalized, key=lambda level: level.price_micros, reverse=reverse))


def _buy_vwap(asks, usd):
    requested = int(usd * USD_SCALE)
    remaining = requested
    shares = 0
    filled = 0
    for level in asks:
        if remaining <= 0:
            break
        level_cost = level.price_micros * level.size_micros // PRICE_SCALE
        if level_cost <= remaining:
            shares_taken = level.size_micros
            cost_taken = level_cost
        else:
            shares_taken = remaining * SHARE_SCALE // level.price_micros
            cost_taken = shares_taken * level.price_micros // PRICE_SCALE
        if shares_taken <= 0:
            break
        shares += shares_taken
        filled += cost_taken
        remaining -= cost_taken
    price = filled * PRICE_SCALE // shares if shares else None
    return VwapFeature(
        requested_usd_micros=requested,
        filled_usd_micros=filled,
        shares_micros=shares,
        price_micros=price,
        insufficient_liquidity=(requested - filled) > 1,
    )


def build_book_checkpoint(checkpoint, book, received_timestamp_ms, monotonic_ns,
                          book_timestamp_ms=None, source_sequence=None, source_hash=None,
                          resync_generation=0, quality_flags=(), vwap_tiers_usd=VWAP_TIERS_USD):
    """Build fixed-width BBO/top-three/VWAP features from a public book."""
    bids = _normalize_levels(book.get("bids") or (), reverse=True)
    asks = _normalize_levels(book.get("asks") or (), reverse=False)
    flags = set(quality_flags)
    if not bids or not asks:
        flags.add("missing_book")
    return BookCheckpoint(
        checkpoint=checkpoint,
        received_timestamp_ms=int(received_timestamp_ms),
        monotonic_ns=int(monotonic_ns),
        book_timestamp_ms=(int(book_timestamp_ms) if book_timestamp_ms is not None else None),
        source_sequence=(str(source_sequence) if source_sequence is not None else None),
        source_hash=source_hash,
        resync_generation=int(resync_generation),
        best_bid_price_micros=(bids[0].price_micros if bids else None),
        best_ask_price_micros=(asks[0].price_micros if asks else None),
        top_bids=bids[:3],
        top_asks=asks[:3],
        buy_vwap=tuple(_buy_vwap(asks, usd) for usd in sorted(set(vwap_tiers_usd))),
        quality_flags=tuple(sorted(flags)),
    )


def build_rest_book_checkpoint(checkpoint, book, received_timestamp_ms, monotonic_ns,
                               resync_generation=0, quality_flags=(),
                               vwap_tiers_usd=VWAP_TIERS_USD):
    """Adapt polymarket_simulator.fetch_order_book() output without I/O."""
    flags = set(quality_flags)
    if book.get("book_timestamp_ms") is None:
        flags.add("missing_book_timestamp")
    if book.get("book_hash") is None:
        flags.add("missing_book_hash")
    return build_book_checkpoint(
        checkpoint=checkpoint,
        book=book,
        received_timestamp_ms=received_timestamp_ms,
        monotonic_ns=monotonic_ns,
        book_timestamp_ms=book.get("book_timestamp_ms"),
        source_hash=book.get("book_hash"),
        resync_generation=resync_generation,
        quality_flags=flags,
        vwap_tiers_usd=vwap_tiers_usd,
    )


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    event_type: str
    source: str
    received_timestamp_ms: int
    monotonic_ns: int
    raw_payload: str
    normalized_payload: dict
    source_timestamp_ms: int | None = None
    source_sequence: str | None = None
    source_hash: str | None = None
    resync_generation: int = 0
    correlation_id: str | None = None
    causation_id: str | None = None
    code_commit: str | None = None
    config_hash: str | None = None
    roster_version: str | None = None
    environment_snapshot_id: str | None = None
    quality_flags: tuple = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if not self.event_id or not self.event_type or not self.source:
            raise ValueError("event_id, event_type, and source are required")
        if self.received_timestamp_ms < 0 or self.monotonic_ns < 0:
            raise ValueError("received_timestamp_ms and monotonic_ns must be non-negative")
        if not isinstance(self.raw_payload, str):
            raise ValueError("raw_payload must be exact UTF-8 text")
        # Normalize tuples and other JSON-compatible containers at creation,
        # not only after a disk round-trip. An in-memory decision and its
        # replayed equivalent must see identical types and values.
        normalized_payload = json.loads(_canonical_json(self.normalized_payload))
        object.__setattr__(self, "normalized_payload", normalized_payload)
        object.__setattr__(self, "quality_flags", tuple(sorted(set(self.quality_flags))))
        _canonical_json(self.to_record())

    def to_record(self):
        record = asdict(self)
        record["quality_flags"] = list(self.quality_flags)
        return record

    def to_json_line(self):
        return _canonical_json(self.to_record()) + "\n"

    @classmethod
    def from_record(cls, record):
        data = dict(record)
        data["quality_flags"] = tuple(data.get("quality_flags") or ())
        return cls(**data)


def build_source_signal_envelope(event_id, source, raw_payload, signal, checkpoints,
                                 received_timestamp_ms, monotonic_ns, **envelope_fields):
    """Create the v1 signal record consumed by the deterministic policy.

    A checkpoint can be absent only when it was not causally observable;
    decision_commit remains mandatory at policy evaluation time and fails
    closed there if missing.
    """
    checkpoint_records = {}
    for checkpoint in checkpoints:
        if not isinstance(checkpoint, BookCheckpoint):
            raise ValueError("checkpoints must contain BookCheckpoint values")
        if checkpoint.checkpoint in checkpoint_records:
            raise ValueError(f"duplicate checkpoint: {checkpoint.checkpoint}")
        checkpoint_records[checkpoint.checkpoint] = asdict(checkpoint)
    normalized_payload = dict(signal)
    normalized_payload["checkpoints"] = checkpoint_records
    return EventEnvelope(
        event_id=event_id,
        event_type="source_trade_signal",
        source=source,
        raw_payload=raw_payload,
        normalized_payload=normalized_payload,
        received_timestamp_ms=received_timestamp_ms,
        monotonic_ns=monotonic_ns,
        **envelope_fields,
    )


class JsonlEventJournal:
    """Append/read versioned JSONL. Intended to sit behind a bounded writer."""

    def __init__(self, path):
        self.path = Path(path)

    def append(self, envelope):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(envelope.to_json_line())

    def read_all(self):
        if not self.path.exists():
            return []
        events = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    events.append(EventEnvelope.from_record(record))
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise ValueError(f"invalid journal record at line {line_number}: {exc}") from exc
        return events


@dataclass(frozen=True)
class JournalWriterHealth:
    queue_size: int
    queue_capacity: int
    accepted_events: int
    dropped_events: int
    write_errors: int
    running: bool

    @property
    def minimum_audit_available(self):
        return self.running and self.dropped_events == 0 and self.write_errors == 0


class BoundedJournalWriter:
    """Non-blocking producer queue with one isolated JSONL writer thread.

    submit() never waits for disk. A full queue returns False and increments
    an observable loss counter; callers must feed that failed minimum-audit
    state into the entry interlock rather than silently losing records.

    Deferred materialization removes Decimal/VWAP/canonical-JSON work from
    the synchronous BUY call stack, but this remains a Python thread and
    therefore still shares the process GIL. It is not evidence that a future
    per-WebSocket-message recorder is burst-safe; that path needs a separate
    process/host and captured-traffic benchmark before activation.
    """

    _STOP = object()

    def __init__(self, journal, queue_capacity=1_024):
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be at least 1")
        self.journal = journal
        self._queue = queue.Queue(maxsize=queue_capacity)
        self._lock = threading.Lock()
        self._accepted_events = 0
        self._dropped_events = 0
        self._write_errors = 0
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="shadow-journal-writer",
            daemon=True,
        )
        self._thread.start()

    def submit(self, envelope):
        if self._closed:
            raise RuntimeError("journal writer is closed")
        try:
            self._queue.put_nowait(envelope)
        except queue.Full:
            with self._lock:
                self._dropped_events += 1
            return False
        with self._lock:
            self._accepted_events += 1
        return True

    def _run(self):
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                try:
                    materialize = getattr(item, "materialize_event", None)
                    envelope = materialize() if callable(materialize) else item
                    self.journal.append(envelope)
                except Exception:
                    # The health counter is the safety signal. The writer
                    # stays alive so a transient I/O error does not discard
                    # every later record without trying.
                    with self._lock:
                        self._write_errors += 1
            finally:
                self._queue.task_done()

    def health(self):
        with self._lock:
            return JournalWriterHealth(
                queue_size=self._queue.qsize(),
                queue_capacity=self._queue.maxsize,
                accepted_events=self._accepted_events,
                dropped_events=self._dropped_events,
                write_errors=self._write_errors,
                running=self._thread.is_alive(),
            )

    def close(self, timeout=None):
        if self._closed:
            return
        self._closed = True
        # Shutdown may wait; the trading hot path never calls close().
        started = time.monotonic()
        try:
            self._queue.put(self._STOP, timeout=timeout)
        except queue.Full as exc:
            raise TimeoutError("journal writer queue did not drain before timeout") from exc
        remaining = None if timeout is None else max(0.0, timeout - (time.monotonic() - started))
        self._thread.join(remaining)
        if self._thread.is_alive():
            raise TimeoutError("journal writer did not drain before timeout")


class VirtualClock:
    def __init__(self):
        self.monotonic_ns = 0

    def advance_to(self, monotonic_ns):
        monotonic_ns = int(monotonic_ns)
        if monotonic_ns < self.monotonic_ns:
            raise ValueError("journal monotonic time regressed")
        self.monotonic_ns = monotonic_ns


@dataclass(frozen=True)
class ShadowDecision:
    source_event_id: str
    policy_version: str
    action: str
    reason: str
    copy_size_usd_micros: int
    executable_price_micros: int | None
    decision_monotonic_ns: int

    def digest(self):
        return hashlib.sha256(_canonical_json(asdict(self)).encode("utf-8")).hexdigest()


def _checkpoint_from_record(record):
    data = dict(record)
    data["top_bids"] = tuple(BookLevel(**level) for level in data.get("top_bids") or ())
    data["top_asks"] = tuple(BookLevel(**level) for level in data.get("top_asks") or ())
    data["buy_vwap"] = tuple(VwapFeature(**item) for item in data.get("buy_vwap") or ())
    data["quality_flags"] = tuple(data.get("quality_flags") or ())
    return BookCheckpoint(**data)


def decide_shadow_buy(envelope, entry_interlock_active=None):
    """Deterministic first policy: quote a supported-size BUY, never execute."""
    payload = envelope.normalized_payload
    copy_size_usd = payload.get("copy_size_usd")
    if payload.get("side") != "BUY":
        return ShadowDecision(envelope.event_id, POLICY_VERSION, "skip", "not_a_buy", 0, None,
                              envelope.monotonic_ns)
    try:
        copy_size_micros = _scaled_int(copy_size_usd, USD_SCALE, "copy_size_usd")
    except ValueError:
        return ShadowDecision(envelope.event_id, POLICY_VERSION, "skip", "invalid_copy_size", 0,
                              None, envelope.monotonic_ns)
    if entry_interlock_active is None:
        entry_interlock_active = payload.get("entry_interlock_active") is True
    if entry_interlock_active:
        return ShadowDecision(envelope.event_id, POLICY_VERSION, "skip", "entry_interlock_active",
                              copy_size_micros, None, envelope.monotonic_ns)

    checkpoint_record = (payload.get("checkpoints") or {}).get("decision_commit")
    if checkpoint_record is None:
        # Transitional reader for the earliest v1 fixture shape. New writers
        # always use the named checkpoints map above.
        checkpoint_record = payload.get("decision_commit_checkpoint")
    if not isinstance(checkpoint_record, dict):
        return ShadowDecision(envelope.event_id, POLICY_VERSION, "skip", "missing_decision_book",
                              copy_size_micros, None, envelope.monotonic_ns)
    try:
        checkpoint = _checkpoint_from_record(checkpoint_record)
    except (TypeError, ValueError):
        return ShadowDecision(envelope.event_id, POLICY_VERSION, "skip", "invalid_decision_book",
                              copy_size_micros, None, envelope.monotonic_ns)
    if UNSAFE_BOOK_FLAGS.intersection(checkpoint.quality_flags):
        return ShadowDecision(envelope.event_id, POLICY_VERSION, "skip", "untrusted_decision_book",
                              copy_size_micros, None, checkpoint.monotonic_ns)

    feature = next(
        (item for item in checkpoint.buy_vwap if item.requested_usd_micros == copy_size_micros),
        None,
    )
    if feature is None:
        return ShadowDecision(envelope.event_id, POLICY_VERSION, "skip", "unsupported_size_tier",
                              copy_size_micros, None, checkpoint.monotonic_ns)
    if feature.insufficient_liquidity or feature.price_micros is None:
        return ShadowDecision(envelope.event_id, POLICY_VERSION, "skip", "insufficient_liquidity",
                              copy_size_micros, feature.price_micros, checkpoint.monotonic_ns)
    return ShadowDecision(envelope.event_id, POLICY_VERSION, "shadow_buy", "quoted_not_submitted",
                          copy_size_micros, feature.price_micros, checkpoint.monotonic_ns)


def replay_shadow_journal(events, entry_interlock_active=None):
    """Replay in recorded receive order; never sort by source timestamps."""
    clock = VirtualClock()
    decisions = []
    for envelope in events:
        clock.advance_to(envelope.monotonic_ns)
        if envelope.event_type == "source_trade_signal":
            decision = decide_shadow_buy(envelope, entry_interlock_active)
            clock.advance_to(decision.decision_monotonic_ns)
            decisions.append(decision)
    return decisions


def decision_digest(decisions):
    payload = [asdict(decision) for decision in decisions]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
