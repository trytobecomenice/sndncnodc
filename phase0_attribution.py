#!/usr/bin/env python3
"""Pure Phase-0 evidence features for a wallet-conditioned shadow signal.

This module has no network, database, key, clock, or order dependency.  It
turns the exact source signal and already-fetched public order book into
fixed-point observations that can be replayed later.  Unknown model inputs
remain explicitly unknown; this module never invents a wallet target price
or authorizes a trade.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


PHASE0_ATTRIBUTION_VERSION = "phase0-attribution-v1"
MICROS = 1_000_000


def _micros(value, field_name, *, maximum=None):
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not parsed.is_finite() or parsed < 0 or (maximum is not None and parsed > maximum):
        raise ValueError(f"{field_name} is outside its supported range")
    return int((parsed * MICROS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _rounded_ratio(numerator, denominator):
    if denominator <= 0:
        return 0
    return (numerator + denominator // 2) // denominator


def _fee_micros(shares_micros, price_micros, fee_rate_ppm):
    # shares * rate * price * (1-price), returned as USD micros.
    numerator = (
        shares_micros
        * fee_rate_ppm
        * price_micros
        * (MICROS - price_micros)
    )
    return _rounded_ratio(numerator, MICROS ** 3)


def _levels(raw_levels, *, bids):
    parsed = []
    for raw_price, raw_size in raw_levels or ():
        try:
            price = _micros(raw_price, "book price", maximum=Decimal("1"))
            size = _micros(raw_size, "book size")
        except ValueError:
            continue
        if price in (None, 0) or size in (None, 0):
            continue
        parsed.append((price, size))
    return tuple(sorted(parsed, key=lambda item: item[0], reverse=bids))


@dataclass(frozen=True)
class BuyExecutionObservation:
    requested_usd_micros: int
    filled_usd_micros: int
    shares_micros: int
    average_price_micros: int | None
    taker_fee_usd_micros: int | None
    net_cash_outflow_usd_micros: int | None
    fill_ratio_ppm: int
    insufficient_liquidity: bool


@dataclass(frozen=True)
class ExitLiquidationObservation:
    requested_shares_micros: int
    filled_shares_micros: int
    average_price_micros: int | None
    gross_proceeds_usd_micros: int
    taker_fee_usd_micros: int | None
    net_proceeds_usd_micros: int | None
    liquidation_ratio_ppm: int
    insufficient_liquidity: bool


def _walk_buy(asks, requested_usd_micros, fee_rate_ppm):
    remaining = requested_usd_micros
    filled = 0
    shares = 0
    fee = 0
    for price, available_shares in asks:
        if remaining <= 0:
            break
        level_cost = price * available_shares // MICROS
        if level_cost <= remaining:
            taken_shares = available_shares
            taken_cost = level_cost
        else:
            taken_shares = remaining * MICROS // price
            taken_cost = taken_shares * price // MICROS
        if taken_shares <= 0:
            break
        shares += taken_shares
        filled += taken_cost
        remaining -= taken_cost
        if fee_rate_ppm is not None:
            fee += _fee_micros(taken_shares, price, fee_rate_ppm)
    average = _rounded_ratio(filled * MICROS, shares) if shares else None
    fill_ratio = min(MICROS, _rounded_ratio(filled * MICROS, requested_usd_micros))
    fee_value = fee if fee_rate_ppm is not None else None
    return BuyExecutionObservation(
        requested_usd_micros=requested_usd_micros,
        filled_usd_micros=filled,
        shares_micros=shares,
        average_price_micros=average,
        taker_fee_usd_micros=fee_value,
        net_cash_outflow_usd_micros=(filled + fee if fee_value is not None else None),
        fill_ratio_ppm=fill_ratio,
        insufficient_liquidity=(requested_usd_micros - filled) > 1,
    )


def _walk_exit(bids, requested_shares_micros, fee_rate_ppm):
    remaining = requested_shares_micros
    filled_shares = 0
    gross_proceeds = 0
    fee = 0
    for price, available_shares in bids:
        if remaining <= 0:
            break
        taken_shares = min(remaining, available_shares)
        proceeds = price * taken_shares // MICROS
        filled_shares += taken_shares
        gross_proceeds += proceeds
        remaining -= taken_shares
        if fee_rate_ppm is not None:
            fee += _fee_micros(taken_shares, price, fee_rate_ppm)
    average = _rounded_ratio(gross_proceeds * MICROS, filled_shares) if filled_shares else None
    fill_ratio = (
        min(MICROS, _rounded_ratio(filled_shares * MICROS, requested_shares_micros))
        if requested_shares_micros else 0
    )
    fee_value = fee if fee_rate_ppm is not None else None
    return ExitLiquidationObservation(
        requested_shares_micros=requested_shares_micros,
        filled_shares_micros=filled_shares,
        average_price_micros=average,
        gross_proceeds_usd_micros=gross_proceeds,
        taker_fee_usd_micros=fee_value,
        net_proceeds_usd_micros=(gross_proceeds - fee if fee_value is not None else None),
        liquidation_ratio_ppm=fill_ratio,
        insufficient_liquidity=remaining > 0,
    )


def build_sell_execution_observation(book, requested_shares, fee_rate=None):
    """Return an executable bid-side FAK observation for an exact share size.

    This is intentionally public for the deterministic Phase-0 shadow ledger.
    It performs no I/O and never assumes unfilled shares were sold.
    """
    requested_shares_micros = _micros(requested_shares, "requested_shares")
    raw_fee_rate = book.get("fee_rate") if fee_rate is None else fee_rate
    try:
        fee_rate_ppm = _micros(raw_fee_rate, "fee_rate", maximum=Decimal("1"))
    except ValueError:
        fee_rate_ppm = None
    bids = _levels(book.get("bids"), bids=True)
    return asdict(_walk_exit(bids, requested_shares_micros or 0, fee_rate_ppm))


def build_phase0_attribution(trade, book, copy_size_usd, strategy_context=None):
    """Return JSON-safe, replayable Phase-0 observations.

    `strategy_context` contains values the existing decision path already
    computed.  No extra API/model call is allowed here.
    """
    strategy_context = dict(strategy_context or {})
    flags = set()
    bids = _levels(book.get("bids"), bids=True)
    asks = _levels(book.get("asks"), bids=False)
    try:
        requested = _micros(copy_size_usd, "copy_size_usd")
    except ValueError:
        requested = 0
        flags.add("invalid_copy_size")

    raw_fee_rate = book.get("fee_rate")
    try:
        fee_rate_ppm = _micros(raw_fee_rate, "fee_rate", maximum=Decimal("1"))
    except ValueError:
        fee_rate_ppm = None
        flags.add("invalid_fee_rate")
    if raw_fee_rate is None:
        fee_rate_ppm = None
        flags.add("fee_rate_unknown")

    buy = _walk_buy(asks, requested, fee_rate_ppm) if requested else BuyExecutionObservation(
        0, 0, 0, None, None, None, 0, True
    )
    exit_observation = _walk_exit(bids, buy.shares_micros, fee_rate_ppm)

    try:
        source_price_micros = _micros(
            trade.get("price"), "source_price", maximum=Decimal("1")
        )
    except ValueError:
        source_price_micros = None
        flags.add("invalid_source_price")
    chase_micros = (
        buy.average_price_micros - source_price_micros
        if buy.average_price_micros is not None and source_price_micros is not None
        else None
    )

    source_timestamp_ms = trade.get("_source_timestamp_ms")
    received_timestamp_ms = trade.get("_received_timestamp_ms")
    reported_age_ms = None
    if source_timestamp_ms is not None and received_timestamp_ms is not None:
        try:
            reported_age_ms = int(received_timestamp_ms) - int(source_timestamp_ms)
        except (TypeError, ValueError):
            flags.add("invalid_signal_timestamp")
        else:
            if reported_age_ms < 0:
                flags.add("source_clock_ahead")
    else:
        flags.add("signal_age_unknown")

    if buy.net_cash_outflow_usd_micros is not None and exit_observation.net_proceeds_usd_micros is not None:
        immediate_round_trip_pnl_micros = (
            exit_observation.net_proceeds_usd_micros - buy.net_cash_outflow_usd_micros
        )
    else:
        immediate_round_trip_pnl_micros = None

    spread_micros = asks[0][0] - bids[0][0] if bids and asks else None
    visible_bid_notional = sum(price * size // MICROS for price, size in bids)
    visible_ask_notional = sum(price * size // MICROS for price, size in asks)

    wallet_model = dict(strategy_context.get("wallet_model") or {})
    point_probability = wallet_model.get("shrunk_win_rate")
    try:
        point_probability_micros = _micros(
            point_probability, "wallet point probability", maximum=Decimal("1")
        )
    except ValueError:
        point_probability_micros = None
        flags.add("invalid_wallet_point_probability")

    point_net_edge_micros = None
    if (
        point_probability_micros is not None
        and buy.average_price_micros is not None
        and buy.taker_fee_usd_micros is not None
        and buy.shares_micros > 0
    ):
        fee_per_share_micros = buy.taker_fee_usd_micros * MICROS // buy.shares_micros
        point_net_edge_micros = (
            point_probability_micros - buy.average_price_micros - fee_per_share_micros
        )

    factor_ids = []
    event_slug = strategy_context.get("event_slug") or book.get("event_slug")
    category = strategy_context.get("category")
    if event_slug:
        factor_ids.append(f"event:{event_slug}")
    if category:
        factor_ids.append(f"category:{category}")

    if not bids or not asks:
        flags.add("missing_two_sided_book")
    if buy.insufficient_liquidity:
        flags.add("insufficient_entry_liquidity")
    if exit_observation.insufficient_liquidity:
        flags.add("insufficient_exit_liquidity")

    return {
        "version": PHASE0_ATTRIBUTION_VERSION,
        "source_position_action": trade.get("_source_position_action", "unknown"),
        "source_intent": "unknown",
        "source_intent_status": "economic_intent_model_unavailable",
        "signal_timing": {
            "reported_source_to_receive_ms": reported_age_ms,
            "age_lower_bound_ms": None,
            "age_upper_bound_ms": None,
            "bounds_status": "poll_visibility_window_or_clock_uncertainty_unavailable",
        },
        "source_price_micros": source_price_micros,
        "buy_execution": asdict(buy),
        "side_adjusted_chase_micros": chase_micros,
        "fee_rate_ppm": fee_rate_ppm,
        "projected_exit_liquidation": asdict(exit_observation),
        "market_quality": {
            "best_bid_price_micros": bids[0][0] if bids else None,
            "best_ask_price_micros": asks[0][0] if asks else None,
            "spread_micros": spread_micros,
            "visible_bid_notional_usd_micros": visible_bid_notional,
            "visible_ask_notional_usd_micros": visible_ask_notional,
            "immediate_round_trip_pnl_usd_micros": immediate_round_trip_pnl_micros,
        },
        "wallet_model_observation": {
            "model_version": wallet_model.get("model_version"),
            "sizing_tier": wallet_model.get("sizing_tier"),
            "sample_count": wallet_model.get("sample_count"),
            "point_probability_micros": point_probability_micros,
        },
        "residual_alpha": {
            "point_estimate_micros": point_net_edge_micros,
            "lower_bound_micros": None,
            "status": "uncalibrated_lower_bound_model_unavailable",
        },
        "risk_context": {
            "factor_ids": sorted(set(factor_ids)),
            "scenario_ids": [],
            "scenario_status": "scenario_model_unavailable",
        },
        "quality_flags": sorted(flags),
    }
