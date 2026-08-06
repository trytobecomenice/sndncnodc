#!/usr/bin/env python3
"""Build shadow records from data the trading path already fetched.

This module performs no network, database, order, timer, or polling action.
The trading producer creates only a lightweight capture capsule. The writer
later materializes its fixed-width v1 record away from the synchronous BUY
path.
"""

from dataclasses import asdict, dataclass
import json

from phase0_attribution import build_phase0_attribution
from shadow_replay import VWAP_TIERS_USD, build_rest_book_checkpoint, build_source_signal_envelope


PUBLIC_SIGNAL_FIELDS = (
    "trade_id", "transaction_hash", "user_address", "market_slug", "market_title",
    "outcome", "side", "price", "size_usd", "timestamp", "detected_by",
)


def _fallback_raw_payload(trade):
    public_signal = {key: trade.get(key) for key in PUBLIC_SIGNAL_FIELDS if key in trade}
    return json.dumps(public_signal, sort_keys=True, separators=(",", ":"), allow_nan=False)


def build_passive_shadow_event(trade, book, copy_size_usd, measurement,
                               entry_interlock_active, decision_timestamp_ms,
                               decision_monotonic_ns, strategy_context=None):
    """Return one replayable source_trade_signal EventEnvelope."""
    book = book or {}
    quality_flags = set(measurement.quality_flags)
    raw_payload = trade.get("_raw_payload")
    raw_payload_format = trade.get("_raw_payload_format")
    if raw_payload is None and trade.get("_raw_record") is not None:
        try:
            raw_payload = json.dumps(
                trade["_raw_record"],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            raw_payload_format = "canonicalized_api_record"
        except (TypeError, ValueError):
            quality_flags.add("raw_record_serialization_failed")
    if raw_payload is None:
        raw_payload = _fallback_raw_payload(trade)
        raw_payload_format = "reconstructed_normalized_signal"
        quality_flags.add("raw_payload_reconstructed")

    checkpoint = build_rest_book_checkpoint(
        "decision_commit",
        book,
        received_timestamp_ms=decision_timestamp_ms,
        monotonic_ns=decision_monotonic_ns,
        quality_flags=quality_flags,
        vwap_tiers_usd=tuple(VWAP_TIERS_USD) + (copy_size_usd,),
    )
    signal_received_timestamp_ms = trade.get("_received_timestamp_ms")
    signal_received_monotonic_ns = (
        trade.get("_ingress_monotonic_ns")
        or trade.get("_enqueued_monotonic_ns")
    )
    if signal_received_timestamp_ms is None:
        signal_received_timestamp_ms = decision_timestamp_ms
        quality_flags.add("missing_signal_wall_timestamp")
    if signal_received_monotonic_ns is None:
        signal_received_monotonic_ns = decision_monotonic_ns
        quality_flags.add("missing_signal_monotonic_timestamp")

    signal = {
        "wallet_address": trade.get("user_address"),
        "market_slug": trade.get("market_slug"),
        "outcome": trade.get("outcome"),
        "side": (trade.get("side") or "").upper(),
        "source_price": trade.get("price"),
        "source_size_usd": trade.get("size_usd"),
        "copy_size_usd": copy_size_usd,
        "detected_by": trade.get("detected_by", "polling"),
        "raw_payload_format": raw_payload_format,
        "entry_interlock_active": bool(entry_interlock_active),
        "passive_integrity": asdict(measurement),
        "phase0_attribution": build_phase0_attribution(
            trade, book, copy_size_usd, strategy_context=strategy_context
        ),
    }
    return build_source_signal_envelope(
        event_id=str(trade.get("trade_id") or f"shadow-{signal_received_monotonic_ns}"),
        source="polymarket_data_api",
        raw_payload=raw_payload,
        signal=signal,
        checkpoints=[checkpoint],
        received_timestamp_ms=signal_received_timestamp_ms,
        monotonic_ns=signal_received_monotonic_ns,
        source_timestamp_ms=trade.get("_source_timestamp_ms"),
        source_hash=book.get("book_hash"),
        correlation_id=str(trade.get("trade_id") or ""),
        quality_flags=tuple(sorted(quality_flags)),
    )


@dataclass(frozen=True)
class PassiveShadowCapture:
    """Lightweight producer capsule materialized only by the writer thread.

    `trade` and `book` are fresh per-signal mappings which the caller must
    not mutate after submission. Keeping references avoids copying a deep
    order book or running Decimal/JSON work before the BUY gate.
    """

    trade: dict
    book: dict
    copy_size_usd: float
    measurement: object
    entry_interlock_active: bool
    decision_timestamp_ms: int
    decision_monotonic_ns: int
    strategy_context: dict | None = None

    def materialize_event(self):
        return build_passive_shadow_event(
            self.trade,
            self.book,
            self.copy_size_usd,
            self.measurement,
            self.entry_interlock_active,
            self.decision_timestamp_ms,
            self.decision_monotonic_ns,
            self.strategy_context,
        )


def build_passive_shadow_capture(trade, book, copy_size_usd, measurement,
                                 entry_interlock_active, decision_timestamp_ms,
                                 decision_monotonic_ns, strategy_context=None):
    """Create the O(1) queue item; do not normalize or serialize here."""
    return PassiveShadowCapture(
        trade=trade,
        book=book,
        copy_size_usd=copy_size_usd,
        measurement=measurement,
        entry_interlock_active=bool(entry_interlock_active),
        decision_timestamp_ms=int(decision_timestamp_ms),
        decision_monotonic_ns=int(decision_monotonic_ns),
        strategy_context=strategy_context,
    )
