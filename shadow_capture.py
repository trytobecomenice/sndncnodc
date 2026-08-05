#!/usr/bin/env python3
"""Build shadow records from data the trading path already fetched.

This module performs no network, database, order, timer, or polling action.
The caller hands it the source signal and the direct-REST book already read
for depth sizing; it only creates the fixed-width v1 journal record.
"""

from dataclasses import asdict
import json

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
                               decision_monotonic_ns):
    """Return one replayable source_trade_signal EventEnvelope."""
    book = book or {}
    quality_flags = set(measurement.quality_flags)
    raw_payload = trade.get("_raw_payload")
    raw_payload_format = trade.get("_raw_payload_format")
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
    signal_received_monotonic_ns = trade.get("_enqueued_monotonic_ns")
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
