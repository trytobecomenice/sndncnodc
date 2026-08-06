#!/usr/bin/env python3
"""Read-only, resource-bounded Phase-0 AWS soak recorder.

The process owns no trading credentials and has no order code.  It polls the
public wallet activity API for distinct fills, takes one public REST book at
first visibility, and uses the unauthenticated market WebSocket for online
T+100/T+500 ms observations.  It writes signal-sized JSONL only; raw WS
firehose frames are not journalled on the t3.small.
"""

import argparse
import asyncio
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time

import aiohttp

import config
from db import get_monitored_noncopying_traders, get_tracked_traders
from phase0_attribution import build_phase0_attribution, build_sell_execution_observation
from phase0_soak import ConvictionTracker, DEFAULT_TIERS_USD, Phase0ShadowLedger, SOAK_SCHEMA_VERSION
import polymarket_data_api
import polymarket_simulator


MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DELAY_TARGETS_MS = (100, 500)


def _wall_ms():
    return time.time_ns() // 1_000_000


def _soak_event_id(trade):
    """A recorder-only fill surrogate without changing production bot dedup.

    Data API activity has no documented unique trade ID.  The production bot
    safely retains its deployed boundary key; the research recorder adds
    price and notional so two same-tx/asset/side/timestamp rows are not merged
    when their economic fills differ.
    """
    identity = {
        "api_trade_id": trade.get("trade_id"),
        "price": trade.get("price"),
        "size_usd": trade.get("size_usd"),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"phase0:{digest}"


def _compact_book(book, depth=10):
    return {
        "bids": list((book.get("bids") or [])[:depth]),
        "asks": list((book.get("asks") or [])[:depth]),
        "book_timestamp_ms": book.get("book_timestamp_ms"),
        "book_hash": book.get("book_hash"),
        "received_timestamp_ms": book.get("received_timestamp_ms"),
        "received_monotonic_ns": book.get("received_monotonic_ns"),
        "local_generation": book.get("local_generation"),
        "reconnect_epoch": book.get("reconnect_epoch"),
        "fee_rate": book.get("fee_rate"),
        "event_slug": book.get("event_slug"),
    }


class JsonlSoakJournal:
    def __init__(self, path, fsync_every=25):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a", encoding="utf-8", buffering=1)
        self.fsync_every = max(1, int(fsync_every))
        self.count = 0

    def append(self, record):
        self.handle.write(
            json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        )
        self.count += 1
        if self.count % self.fsync_every == 0:
            self.handle.flush()
            os.fsync(self.handle.fileno())

    def close(self):
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()


class MarketBookStream:
    """Bounded public-WS L2 cache with reconnect epochs and local generations."""

    def __init__(self, journal, max_assets=256):
        self.journal = journal
        self.max_assets = int(max_assets)
        self.assets = OrderedDict()
        self.books = {}
        self.ws = None
        self.connected = False
        self.reconnect_epoch = 0
        self.last_pong_monotonic_ns = 0
        self.stop_event = asyncio.Event()
        self.subscription_changed = asyncio.Event()

    async def ensure_subscription(self, asset_id):
        asset_id = str(asset_id)
        evicted = None
        is_new = asset_id not in self.assets
        if asset_id in self.assets:
            self.assets.move_to_end(asset_id)
        else:
            self.assets[asset_id] = time.monotonic_ns()
            if len(self.assets) > self.max_assets:
                evicted, _ = self.assets.popitem(last=False)
                self.books.pop(evicted, None)
            self.subscription_changed.set()
        if is_new and self.connected and self.ws is not None:
            if evicted:
                await self.ws.send_json({"assets_ids": [evicted], "operation": "unsubscribe"})
            await self.ws.send_json({"assets_ids": [asset_id], "operation": "subscribe"})

    def snapshot(self, asset_id):
        asset_id = str(asset_id)
        state = self.books.get(asset_id)
        if (
            not self.connected
            or state is None
            or state.get("reconnect_epoch") != self.reconnect_epoch
        ):
            return None
        return {
            "bids": sorted(state["bids"].items(), key=lambda item: item[0], reverse=True),
            "asks": sorted(state["asks"].items(), key=lambda item: item[0]),
            "book_timestamp_ms": state.get("server_timestamp_ms"),
            "book_hash": state.get("book_hash"),
            "received_timestamp_ms": state["received_timestamp_ms"],
            "received_monotonic_ns": state["received_monotonic_ns"],
            "local_generation": state["local_generation"],
            "reconnect_epoch": state["reconnect_epoch"],
        }

    def _state(self, asset_id):
        return self.books.setdefault(str(asset_id), {
            "bids": {},
            "asks": {},
            "local_generation": 0,
            "reconnect_epoch": self.reconnect_epoch,
            "received_timestamp_ms": 0,
            "received_monotonic_ns": 0,
            "server_timestamp_ms": None,
            "book_hash": None,
        })

    @staticmethod
    def _number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _apply(self, payload, received_wall_ms, received_monotonic_ns):
        event_type = payload.get("event_type")
        if event_type == "book":
            asset_id = payload.get("asset_id")
            if not asset_id:
                return
            state = self._state(asset_id)
            state["bids"] = {
                price: size for level in payload.get("bids") or ()
                if (price := self._number(level.get("price"))) is not None
                and (size := self._number(level.get("size"))) not in (None, 0)
            }
            state["asks"] = {
                price: size for level in payload.get("asks") or ()
                if (price := self._number(level.get("price"))) is not None
                and (size := self._number(level.get("size"))) not in (None, 0)
            }
            state["book_hash"] = payload.get("hash")
            self._stamp(state, payload, received_wall_ms, received_monotonic_ns)
            return
        if event_type != "price_change":
            return
        for change in payload.get("price_changes") or ():
            asset_id = change.get("asset_id")
            price = self._number(change.get("price"))
            size = self._number(change.get("size"))
            side = str(change.get("side") or "").upper()
            if not asset_id or price is None or size is None or side not in {"BUY", "SELL"}:
                continue
            state = self._state(asset_id)
            levels = state["bids" if side == "BUY" else "asks"]
            if size == 0:
                levels.pop(price, None)
            else:
                levels[price] = size
            state["book_hash"] = change.get("hash") or state.get("book_hash")
            self._stamp(state, payload, received_wall_ms, received_monotonic_ns)

    def _stamp(self, state, payload, received_wall_ms, received_monotonic_ns):
        state["local_generation"] += 1
        state["reconnect_epoch"] = self.reconnect_epoch
        state["received_timestamp_ms"] = received_wall_ms
        state["received_monotonic_ns"] = received_monotonic_ns
        try:
            state["server_timestamp_ms"] = int(payload.get("timestamp"))
        except (TypeError, ValueError):
            state["server_timestamp_ms"] = None

    async def _heartbeat(self, ws):
        while not ws.closed and not self.stop_event.is_set():
            await asyncio.sleep(10)
            await ws.send_str("PING")

    async def run(self):
        backoff = 1
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while not self.stop_event.is_set():
                if not self.assets:
                    self.subscription_changed.clear()
                    try:
                        await asyncio.wait_for(self.subscription_changed.wait(), timeout=1)
                    except asyncio.TimeoutError:
                        continue
                try:
                    async with session.ws_connect(MARKET_WS_URL, autoping=False) as ws:
                        self.ws = ws
                        self.connected = True
                        self.reconnect_epoch += 1
                        self.last_pong_monotonic_ns = time.monotonic_ns()
                        await ws.send_json({
                            "assets_ids": list(self.assets),
                            "type": "market",
                            "custom_feature_enabled": True,
                        })
                        self.journal.append({
                            "schema_version": SOAK_SCHEMA_VERSION,
                            "event_type": "ws_connected",
                            "timestamp_ms": _wall_ms(),
                            "reconnect_epoch": self.reconnect_epoch,
                            "asset_count": len(self.assets),
                        })
                        heartbeat = asyncio.create_task(self._heartbeat(ws))
                        backoff = 1
                        try:
                            while not self.stop_event.is_set() and not ws.closed:
                                try:
                                    message = await ws.receive(timeout=5)
                                except asyncio.TimeoutError:
                                    if time.monotonic_ns() - self.last_pong_monotonic_ns > 25_000_000_000:
                                        raise RuntimeError("market WS heartbeat stale")
                                    continue
                                if message.type == aiohttp.WSMsgType.TEXT:
                                    if message.data == "PONG":
                                        self.last_pong_monotonic_ns = time.monotonic_ns()
                                        continue
                                    received_wall_ms = _wall_ms()
                                    received_monotonic_ns = time.monotonic_ns()
                                    payloads = json.loads(message.data)
                                    if not isinstance(payloads, list):
                                        payloads = [payloads]
                                    for payload in payloads:
                                        if isinstance(payload, dict):
                                            self._apply(payload, received_wall_ms, received_monotonic_ns)
                                elif message.type in {
                                    aiohttp.WSMsgType.CLOSE,
                                    aiohttp.WSMsgType.CLOSED,
                                    aiohttp.WSMsgType.ERROR,
                                }:
                                    break
                        finally:
                            heartbeat.cancel()
                            await asyncio.gather(heartbeat, return_exceptions=True)
                except Exception as exc:
                    self.journal.append({
                        "schema_version": SOAK_SCHEMA_VERSION,
                        "event_type": "ws_disconnected",
                        "timestamp_ms": _wall_ms(),
                        "reconnect_epoch": self.reconnect_epoch,
                        "error": str(exc),
                        "retry_in_seconds": backoff,
                    })
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(30, backoff * 2)
                finally:
                    self.connected = False
                    self.ws = None

    def stop(self):
        self.stop_event.set()


class SoakRecorder:
    def __init__(self, journal, poll_seconds=30, max_workers=4, max_assets=256,
                 bootstrap_sample_count=0, bootstrap_sample_warmup_seconds=0):
        self.journal = journal
        self.poll_seconds = float(poll_seconds)
        self.executor = ThreadPoolExecutor(max_workers=int(max_workers))
        self.stream = MarketBookStream(journal, max_assets=max_assets)
        self.ledger = Phase0ShadowLedger()
        self.conviction = ConvictionTracker()
        self.seen_ids = set()
        self.api_seen_ids = set()
        self.seen_order = deque(maxlen=50_000)
        self.pending = set()
        self.stop_event = asyncio.Event()
        self.previous_poll_completed_ms = None
        self.bootstrap_required = True
        self.bootstrap_sample_count = max(0, int(bootstrap_sample_count))
        # Sanity-test aid only. Production keeps bootstrap_sample_count=0, so
        # startup history is never converted into a fake newly-seen signal.
        self.bootstrap_sample_warmup_seconds = max(
            0.0, float(bootstrap_sample_warmup_seconds)
        )
        self._restore()

    def _restore(self):
        if not self.journal.path.exists():
            return
        try:
            with self.journal.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if record.get("event_type") == "dedup_bootstrap":
                        identity = record.get("dedup_identity")
                        event_ids = record.get("event_ids") or ()
                        if identity == "exact_trade_id":
                            # Compatibility with the pre-fingerprint local
                            # sanity format: these are API boundary IDs, not
                            # the newer research event fingerprints.
                            self.api_seen_ids.update(str(item) for item in event_ids)
                        else:
                            for event_id in event_ids:
                                self._remember(event_id)
                            self.api_seen_ids.update(record.get("api_trade_ids") or ())
                        continue
                    if record.get("event_type") != "wallet_signal":
                        continue
                    event_id = record.get("signal_event_id") or record.get("event_id")
                    if event_id:
                        self._remember(event_id)
                    api_trade_id = record.get("api_trade_id")
                    if api_trade_id:
                        self.api_seen_ids.add(str(api_trade_id))
                    signal = dict(record.get("signal") or {})
                    received_ms = record.get("first_local_seen_timestamp_ms")
                    if received_ms is not None:
                        self.conviction.observe(signal, received_ms)
                    key = record.get("position_key")
                    lifecycle = record.get("shadow_lifecycle") or {}
                    if key and lifecycle.get("ledger_after"):
                        self.ledger.restore_key(key, lifecycle["ledger_after"])
            self.bootstrap_required = not bool(self.api_seen_ids)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.journal.append({
                "schema_version": SOAK_SCHEMA_VERSION,
                "event_type": "restore_failed",
                "timestamp_ms": _wall_ms(),
                "error": str(exc),
            })

    def _remember(self, trade_id):
        trade_id = str(trade_id)
        if trade_id in self.seen_ids:
            return
        if len(self.seen_order) == self.seen_order.maxlen:
            self.seen_ids.discard(self.seen_order[0])
        self.seen_order.append(trade_id)
        self.seen_ids.add(trade_id)

    async def _fetch_feed(self, wallets):
        return await asyncio.to_thread(
            lambda: polymarket_data_api.fetch_all_wallets_concurrent(
                wallets,
                limit=config.DIRECT_API_PER_WALLET_LIMIT,
                max_workers=self.executor._max_workers,
                executor=self.executor,
                known_trade_ids=self.api_seen_ids,
                capture_timing=True,
                max_pages_per_wallet=(1 if self.bootstrap_required else None),
            ),
        )

    async def _resolve_signal_book(self, trade):
        market_info, book = await asyncio.to_thread(
            polymarket_simulator.fetch_order_book_for_outcome,
            trade["market_slug"],
            trade["outcome"],
            ignore_staleness=True,
            capture_parse_timing=True,
        )
        book["fee_rate"] = market_info.get("fee_rate")
        book["event_slug"] = market_info.get("event_slug")
        token_id = polymarket_simulator.token_id_for_outcome(market_info, trade["outcome"])
        return market_info, book, str(token_id)

    async def _capture_delay(self, event_id, trade, asset_id, delay_context,
                             target_delay_ms, signal_monotonic_ns):
        target_ns = signal_monotonic_ns + int(target_delay_ms) * 1_000_000
        await asyncio.sleep(max(0, (target_ns - time.monotonic_ns()) / 1_000_000_000))
        actual_ns = time.monotonic_ns()
        book = self.stream.snapshot(asset_id)
        fee_rate = delay_context.get("fee_rate")
        lifecycle = delay_context.get("lifecycle") or {}
        if book is not None:
            book["fee_rate"] = fee_rate
        tier_observations = {}
        if book is not None:
            for tier in DEFAULT_TIERS_USD:
                tier_key = str(tier)
                if str(trade.get("side") or "").upper() == "BUY":
                    tier_observations[tier_key] = build_phase0_attribution(
                        trade, book, tier
                    )["buy_execution"]
                else:
                    immediate = (lifecycle.get("tiers") or {}).get(tier_key) or {}
                    execution = immediate.get("execution") or {}
                    requested = execution.get("requested_shares_micros")
                    tier_observations[tier_key] = (
                        build_sell_execution_observation(
                            book, requested / 1_000_000
                        ) if requested is not None else None
                    )
        received_ns = book.get("received_monotonic_ns") if book else None
        self.journal.append({
            "schema_version": SOAK_SCHEMA_VERSION,
            "event_type": "delayed_book_observation",
            "event_id": f"{event_id}:t+{target_delay_ms}ms",
            "correlation_id": event_id,
            "target_delay_ms": target_delay_ms,
            "target_monotonic_ns": target_ns,
            "capture_monotonic_ns": actual_ns,
            "capture_lateness_ns": max(0, actual_ns - target_ns),
            "book_known_by_capture_deadline": bool(
                book is not None and received_ns is not None and received_ns <= target_ns
            ),
            "causal_alignment": "online_latest_state_at_capture",
            "target_snapshot_status": (
                "latest_generation_was_already_known_by_target"
                if book is not None and received_ns is not None and received_ns <= target_ns
                else "not_proven_known_by_target"
            ),
            "exchange_sequence_id": None,
            "exchange_sequence_status": "not_provided_by_public_market_ws",
            "book": _compact_book(book) if book else None,
            "tier_execution_observations": tier_observations,
        })

    async def _process_signal(self, trade, poll_started_ms, poll_completed_ms):
        event_id = _soak_event_id(trade)
        trade["_soak_event_id"] = event_id
        first_seen_wall_ms = _wall_ms()
        first_seen_monotonic_ns = time.monotonic_ns()
        conviction = self.conviction.observe(trade, first_seen_wall_ms)
        raw_signal = trade.get("_raw_record") or {}
        source_ms = trade.get("_source_timestamp_ms")
        reported_visibility_lag_ms = (
            first_seen_wall_ms - int(source_ms) if source_ms is not None else None
        )
        signal_fields = {
            key: trade.get(key) for key in (
                "trade_id", "transaction_hash", "user_address", "market_slug",
                "market_title", "outcome", "side", "price", "size_usd", "timestamp",
            )
        }
        # Persist ingress before any REST lookup. T+100/T+500 observations can
        # legitimately complete before enrichment when REST is slow; this
        # parent event keeps append order causal and replayable.
        self.journal.append({
            "schema_version": SOAK_SCHEMA_VERSION,
            "event_type": "wallet_signal_ingress",
            "event_id": event_id,
            "api_trade_id": trade.get("trade_id"),
            "first_local_seen_timestamp_ms": first_seen_wall_ms,
            "first_local_seen_monotonic_ns": first_seen_monotonic_ns,
            "source_reported_timestamp_ms": source_ms,
            "reported_visibility_lag_ms": reported_visibility_lag_ms,
            "visibility_lag_status": "source_timestamp_semantics_not_documented",
            "poll_visibility_window": {
                "previous_poll_completed_ms": self.previous_poll_completed_ms,
                "current_poll_started_ms": poll_started_ms,
                "current_poll_completed_ms": poll_completed_ms,
            },
            "signal": signal_fields,
            "raw_signal_payload": raw_signal,
            "raw_signal_payload_format": trade.get("_raw_payload_format"),
            "conviction": conviction,
        })
        early_asset_id = raw_signal.get("asset")
        delay_context = {"fee_rate": None, "lifecycle": None}
        decision_book = None
        if early_asset_id:
            early_asset_id = str(early_asset_id)
            # Snapshot before yielding. A later WS update must never be
            # back-dated and presented as the book known at signal ingress.
            decision_book = self.stream.snapshot(early_asset_id)
            await self.stream.ensure_subscription(early_asset_id)
            for target_ms in DELAY_TARGETS_MS:
                task = asyncio.create_task(self._capture_delay(
                    event_id,
                    trade,
                    early_asset_id,
                    delay_context,
                    target_ms,
                    first_seen_monotonic_ns,
                ))
                self.pending.add(task)
                task.add_done_callback(self.pending.discard)
        try:
            market_info, rest_book, asset_id = await self._resolve_signal_book(trade)
            await self.stream.ensure_subscription(asset_id)
            quality_flags = []
            if decision_book is not None:
                decision_book["fee_rate"] = rest_book.get("fee_rate")
                decision_book["event_slug"] = rest_book.get("event_slug")
            decision_received_ns = (
                decision_book.get("received_monotonic_ns") if decision_book else None
            )
            decision_book_age_ns = (
                first_seen_monotonic_ns - int(decision_received_ns)
                if decision_received_ns is not None else None
            )
            if decision_book is None:
                quality_flags.append("ws_book_unknown_at_signal")
                lifecycle = {
                    "status": "skipped_no_ws_book_known_at_signal",
                    "tiers": {},
                    "ledger_after": None,
                }
            elif decision_book_age_ns is None or decision_book_age_ns < 0:
                quality_flags.append("ws_book_arrived_after_signal")
                lifecycle = {
                    "status": "skipped_post_signal_ws_book",
                    "tiers": {},
                    "ledger_after": None,
                }
            elif decision_book_age_ns > polymarket_simulator.MAX_BOOK_AGE_SECONDS * 1_000_000_000:
                quality_flags.append("ws_book_stale_at_signal")
                lifecycle = {
                    "status": "skipped_stale_ws_book_at_signal",
                    "tiers": {},
                    "ledger_after": None,
                }
            else:
                lifecycle = self.ledger.apply_signal(trade, decision_book)
            delay_context["fee_rate"] = rest_book.get("fee_rate")
            delay_context["lifecycle"] = lifecycle
        except Exception as exc:
            market_info, rest_book, asset_id, lifecycle = (
                None, None, early_asset_id, None
            )
            decision_book_age_ns = (
                first_seen_monotonic_ns - int(decision_book["received_monotonic_ns"])
                if decision_book and decision_book.get("received_monotonic_ns") is not None
                else None
            )
            quality_flags = ["signal_book_unavailable"]
            error = str(exc)
        else:
            error = None
        record = {
            "schema_version": SOAK_SCHEMA_VERSION,
            "event_type": "wallet_signal",
            "event_id": f"{event_id}:attribution",
            "correlation_id": event_id,
            "signal_event_id": event_id,
            "api_trade_id": trade.get("trade_id"),
            "position_key": "|".join((
                str(trade.get("user_address") or "").lower(),
                str(trade.get("market_slug") or ""),
                str(trade.get("outcome") or ""),
            )),
            "first_local_seen_timestamp_ms": first_seen_wall_ms,
            "first_local_seen_monotonic_ns": first_seen_monotonic_ns,
            "source_reported_timestamp_ms": source_ms,
            "reported_visibility_lag_ms": reported_visibility_lag_ms,
            "visibility_lag_status": "source_timestamp_semantics_not_documented",
            "poll_visibility_window": {
                "previous_poll_completed_ms": self.previous_poll_completed_ms,
                "current_poll_started_ms": poll_started_ms,
                "current_poll_completed_ms": poll_completed_ms,
            },
            "signal": signal_fields,
            "raw_signal_payload": trade.get("_raw_record"),
            "raw_signal_payload_format": trade.get("_raw_payload_format"),
            "conviction": conviction,
            "asset_id": asset_id,
            "market_info": market_info,
            "decision_book": _compact_book(decision_book) if decision_book else None,
            "decision_book_source": "public_ws_state_known_at_signal",
            "decision_book_age_ns": decision_book_age_ns,
            "rest_enrichment_latency_ns": (
                int(rest_book["received_monotonic_ns"]) - first_seen_monotonic_ns
                if rest_book and rest_book.get("received_monotonic_ns") is not None else None
            ),
            "shadow_lifecycle": lifecycle,
            "quality_flags": quality_flags,
            "error": error,
        }
        self.journal.append(record)
        if asset_id and not early_asset_id:
            for target_ms in DELAY_TARGETS_MS:
                task = asyncio.create_task(self._capture_delay(
                    event_id,
                    trade,
                    asset_id,
                    delay_context,
                    target_ms,
                    first_seen_monotonic_ns,
                ))
                self.pending.add(task)
                task.add_done_callback(self.pending.discard)

    async def run(self, duration_seconds=0):
        stream_task = asyncio.create_task(self.stream.run())
        started_ns = time.monotonic_ns()
        deadline_ns = (
            started_ns + int(float(duration_seconds) * 1_000_000_000)
            if duration_seconds else None
        )
        self.journal.append({
            "schema_version": SOAK_SCHEMA_VERSION,
            "event_type": "recorder_started",
            "timestamp_ms": _wall_ms(),
            "pid": os.getpid(),
            "paper_only": True,
            "order_capability": False,
        })
        try:
            while not self.stop_event.is_set():
                if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
                    break
                poll_started_ms = _wall_ms()
                tracked = get_tracked_traders()
                monitored = get_monitored_noncopying_traders()
                wallets = list(dict.fromkeys([*tracked, *monitored]))
                try:
                    feed = await self._fetch_feed(wallets)
                except Exception as exc:
                    feed = {"trades": [], "errors": [{"error": str(exc)}]}
                poll_completed_ms = _wall_ms()
                trades = feed.get("trades") or []
                new_trades = [
                    trade for trade in trades
                    if _soak_event_id(trade) not in self.seen_ids
                ]
                new_trades.sort(key=lambda trade: trade.get("_source_timestamp_ms") or 0)
                if self.bootstrap_required:
                    samples = (
                        new_trades[-self.bootstrap_sample_count:]
                        if self.bootstrap_sample_count else []
                    )
                    sample_ids = {_soak_event_id(trade) for trade in samples}
                    bootstrap_ids = []
                    bootstrap_api_ids = []
                    for trade in new_trades:
                        if trade.get("trade_id"):
                            event_id = _soak_event_id(trade)
                            if event_id not in sample_ids:
                                self._remember(event_id)
                            self.api_seen_ids.add(str(trade["trade_id"]))
                            bootstrap_ids.append(event_id)
                            bootstrap_api_ids.append(str(trade["trade_id"]))
                    warm_assets = []
                    for trade in reversed(new_trades):
                        raw = trade.get("_raw_record") or {}
                        asset_id = raw.get("asset")
                        if asset_id and str(asset_id) not in warm_assets:
                            warm_assets.append(str(asset_id))
                        if len(warm_assets) >= self.stream.max_assets:
                            break
                    for asset_id in warm_assets:
                        await self.stream.ensure_subscription(asset_id)
                    warmed_sample_books = 0
                    if samples and self.bootstrap_sample_warmup_seconds > 0:
                        sample_assets = {
                            str((trade.get("_raw_record") or {}).get("asset"))
                            for trade in samples
                            if (trade.get("_raw_record") or {}).get("asset")
                        }
                        warmup_deadline = (
                            time.monotonic()
                            + self.bootstrap_sample_warmup_seconds
                        )
                        while time.monotonic() < warmup_deadline:
                            warmed_sample_books = sum(
                                self.stream.snapshot(asset_id) is not None
                                for asset_id in sample_assets
                            )
                            if warmed_sample_books == len(sample_assets):
                                break
                            await asyncio.sleep(0.01)
                    self.journal.append({
                        "schema_version": SOAK_SCHEMA_VERSION,
                        "event_type": "dedup_bootstrap",
                        "timestamp_ms": _wall_ms(),
                        "event_ids": bootstrap_ids,
                        "api_trade_ids": bootstrap_api_ids,
                        "dedup_identity": (
                            "research_fill_surrogate:api_trade_id+price+size_usd"
                        ),
                        "signals_recorded": bool(samples),
                        "bootstrap_sample_count": len(samples),
                        "warmed_asset_count": len(warm_assets),
                        "warmed_sample_book_count": warmed_sample_books,
                        "bootstrap_sample_warmup_seconds": (
                            self.bootstrap_sample_warmup_seconds
                        ),
                    })
                    self.bootstrap_required = False
                    new_trades = samples
                processed_new_trades = 0
                for trade in new_trades:
                    if self.stop_event.is_set() or (
                        deadline_ns is not None and time.monotonic_ns() >= deadline_ns
                    ):
                        break
                    api_trade_id = trade.get("trade_id")
                    event_id = _soak_event_id(trade)
                    if not api_trade_id or event_id in self.seen_ids:
                        continue
                    self._remember(event_id)
                    self.api_seen_ids.add(str(api_trade_id))
                    await self._process_signal(trade, poll_started_ms, poll_completed_ms)
                    processed_new_trades += 1
                self.journal.append({
                    "schema_version": SOAK_SCHEMA_VERSION,
                    "event_type": "poll_cycle",
                    "poll_started_ms": poll_started_ms,
                    "poll_completed_ms": poll_completed_ms,
                    "duration_ms": poll_completed_ms - poll_started_ms,
                    "wallet_count": len(wallets),
                    "fetched_trade_count": len(trades),
                    "new_trade_count": len(new_trades),
                    "processed_new_trade_count": processed_new_trades,
                    "errors": feed.get("errors") or [],
                })
                self.previous_poll_completed_ms = poll_completed_ms
                sleep_seconds = self.poll_seconds
                if deadline_ns is not None:
                    sleep_seconds = min(
                        sleep_seconds,
                        max(0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000),
                    )
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=sleep_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.stream.stop()
            await asyncio.gather(stream_task, return_exceptions=True)
            if self.pending:
                await asyncio.gather(*self.pending, return_exceptions=True)
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.journal.append({
                "schema_version": SOAK_SCHEMA_VERSION,
                "event_type": "recorder_stopped",
                "timestamp_ms": _wall_ms(),
            })

    def stop(self):
        self.stop_event.set()


def apply_memory_limit(memory_limit_mb):
    if not memory_limit_mb:
        return False
    # RLIMIT_AS is reliable for this deployment target (Linux) but Darwin can
    # reject a limit below the process' already-reserved virtual address
    # space. AWS still has both this limit and systemd MemoryMax; local macOS
    # sanity runs intentionally rely on their parent/process limits instead.
    if sys.platform != "linux":
        return False
    import resource
    limit = int(memory_limit_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    return True


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", default="data/phase0_soak_v1.jsonl")
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--duration-seconds", type=float, default=0.0)
    parser.add_argument("--memory-limit-mb", type=int, default=768)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--max-assets", type=int, default=256)
    parser.add_argument(
        "--bootstrap-sample-count",
        type=int,
        default=0,
        help="sanity-test only; production soak must keep the default zero",
    )
    parser.add_argument(
        "--bootstrap-sample-warmup-seconds",
        type=float,
        default=0.0,
        help="sanity-test only; wait for sampled WS books before replaying startup history",
    )
    return parser.parse_args(argv)


async def async_main(args):
    journal = JsonlSoakJournal(args.journal)
    recorder = SoakRecorder(
        journal,
        poll_seconds=args.poll_seconds,
        max_workers=args.max_workers,
        max_assets=args.max_assets,
        bootstrap_sample_count=args.bootstrap_sample_count,
        bootstrap_sample_warmup_seconds=args.bootstrap_sample_warmup_seconds,
    )
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, recorder.stop)
    try:
        await recorder.run(duration_seconds=args.duration_seconds)
    finally:
        journal.close()


def main(argv=None):
    args = parse_args(argv)
    apply_memory_limit(args.memory_limit_mb)
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
