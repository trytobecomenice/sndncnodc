#!/usr/bin/env python3
"""Deterministic displayed-depth FAK/FOK counterfactual engine.

This walks recorded L2 levels with fixed-point integer arithmetic.  It models
only the book visible in the journal: no queue priority, cancels, hidden size,
operator ordering, network delay, or fill probability.  Its result is an
upper-bound counterfactual, never evidence that a live order would fill.
"""

import argparse
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path


MICROS = 1_000_000
ENGINE_VERSION = "virtual-displayed-clob-v2"
DEFAULT_FEE_PRECISION_MICROS = 10  # current docs: 0.00001 USDC
ROUNDING_POLICY = "floor_each_fill_to_1e-6_then_floor_fee_to_declared_precision"


def _micros(value, field, *, maximum=None):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not parsed.is_finite() or parsed < 0 or (maximum is not None and parsed > maximum):
        raise ValueError(f"{field} outside supported range")
    return int((parsed * MICROS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _ratio(numerator, denominator):
    if denominator <= 0:
        return None
    return (numerator + denominator // 2) // denominator


def _levels(raw_levels, *, bids):
    parsed = []
    for raw in raw_levels or ():
        if isinstance(raw, dict):
            raw_price, raw_size = raw.get("price"), raw.get("size")
        else:
            try:
                raw_price, raw_size = raw
            except (TypeError, ValueError):
                continue
        try:
            price = _micros(raw_price, "price", maximum=Decimal("1"))
            size = _micros(raw_size, "size")
        except ValueError:
            continue
        if price <= 0 or size <= 0:
            continue
        parsed.append((price, size))
    return sorted(parsed, key=lambda item: item[0], reverse=bids)


@dataclass(frozen=True)
class MatchResult:
    engine_version: str
    model_scope: str
    side: str
    order_type: str
    status: str
    requested_cash_micros: int | None
    requested_shares_micros: int | None
    cash_budget_mode: str | None
    filled_cash_micros: int
    filled_shares_micros: int
    taker_fee_cash_micros: int | None
    total_cash_debit_micros: int | None
    net_cash_proceeds_micros: int | None
    fee_rate_ppm: int | None
    fee_precision_micros: int
    fee_model_status: str
    rounding_policy: str
    residual_budget_dust_micros: int | None
    residual_share_dust_micros: int | None
    unfilled_cash_micros: int | None
    unfilled_shares_micros: int | None
    fill_ratio_ppm: int
    best_price_micros: int | None
    worst_fill_price_micros: int | None
    vwap_price_micros: int | None
    side_adjusted_slippage_micros: int | None
    side_adjusted_slippage_bps: int | None
    levels_consumed: int
    insufficient_displayed_depth: bool
    preview_fill_before_fok_kill: dict | None


def _validate_order_type(order_type):
    order_type = str(order_type).upper()
    if order_type not in {"FAK", "FOK"}:
        raise ValueError("order_type must be FAK or FOK")
    return order_type


def _fee_rate_ppm(fee_rate):
    return None if fee_rate is None else _micros(fee_rate, "fee_rate", maximum=Decimal("1"))


def _fee_cash_micros(shares_micros, price_micros, fee_rate_ppm,
                     fee_precision_micros=DEFAULT_FEE_PRECISION_MICROS):
    """Documented fee formula, evaluated per simulated fill and truncated.

    All operands are fixed-point 1e-6.  This mirrors Solidity-style integer
    division, then floors to the documented 0.00001-USDC fee precision.  It is
    a declared research assumption until golden vectors from the deployed
    venue/SDK version are available.
    """
    if fee_rate_ppm is None:
        return None
    precision = int(fee_precision_micros)
    if precision <= 0:
        raise ValueError("fee_precision_micros must be positive")
    raw = (
        int(shares_micros) * int(price_micros)
        * (MICROS - int(price_micros)) * int(fee_rate_ppm)
    ) // (MICROS ** 3)
    return raw // precision * precision


def _level_cost(shares_micros, price_micros, fee_rate_ppm,
                fee_precision_micros, include_fee):
    notional = int(shares_micros) * int(price_micros) // MICROS
    fee = _fee_cash_micros(
        shares_micros, price_micros, fee_rate_ppm, fee_precision_micros
    )
    return notional, fee, notional + ((fee or 0) if include_fee else 0)


def _max_affordable_shares(available_shares, price, remaining,
                           fee_rate_ppm, fee_precision_micros, include_fee):
    """Return the largest 1e-6-share amount whose declared cost fits."""
    low, high = 0, int(available_shares)
    while low < high:
        candidate = (low + high + 1) // 2
        _notional, _fee, cost = _level_cost(
            candidate, price, fee_rate_ppm, fee_precision_micros, include_fee
        )
        if cost <= remaining:
            low = candidate
        else:
            high = candidate - 1
    return low


def simulate_cash_buy(book, cash_usd, order_type="FAK", *, fee_rate=None,
                      cash_budget_mode="ORDER_NOTIONAL",
                      fee_precision_micros=DEFAULT_FEE_PRECISION_MICROS):
    """Walk asks for a research cash-budget BUY.

    ORDER_NOTIONAL follows the documented order boundary: the requested cash
    sizes displayed notional and fee is attributed at match. ALL_IN_CAPITAL is
    a conservative capital-allocation scenario that embeds each marginal fee
    inside the budget. Neither mode claims exact venue acceptance semantics.
    """
    order_type = _validate_order_type(order_type)
    cash_budget_mode = str(cash_budget_mode).upper()
    if cash_budget_mode not in {"ORDER_NOTIONAL", "ALL_IN_CAPITAL"}:
        raise ValueError("cash_budget_mode must be ORDER_NOTIONAL or ALL_IN_CAPITAL")
    rate_ppm = _fee_rate_ppm(fee_rate)
    include_fee = cash_budget_mode == "ALL_IN_CAPITAL"
    if include_fee and rate_ppm is None:
        raise ValueError("ALL_IN_CAPITAL requires an explicit fee_rate")
    requested = _micros(cash_usd, "cash_usd")
    asks = _levels((book or {}).get("asks"), bids=False)
    remaining = requested
    spent = 0
    shares = 0
    fees = 0 if rate_ppm is not None else None
    levels_consumed = 0
    worst = None
    for price, available_shares in asks:
        if remaining <= 1:
            break
        level_cash, level_fee, level_budget_cost = _level_cost(
            available_shares, price, rate_ppm, fee_precision_micros, include_fee
        )
        if level_budget_cost <= remaining:
            taken_shares = available_shares
        else:
            taken_shares = _max_affordable_shares(
                available_shares, price, remaining, rate_ppm,
                fee_precision_micros, include_fee,
            )
        taken_cash, taken_fee, taken_budget_cost = _level_cost(
            taken_shares, price, rate_ppm, fee_precision_micros, include_fee
        )
        if taken_shares <= 0 or taken_cash <= 0 or taken_budget_cost <= 0:
            continue
        remaining -= taken_budget_cost
        spent += taken_cash
        shares += taken_shares
        if fees is not None:
            fees += taken_fee or 0
        worst = price
        levels_consumed += 1
    insufficient = remaining > 1
    preview = {
        "filled_cash_micros": spent,
        "filled_shares_micros": shares,
        "unfilled_cash_micros": remaining,
        "taker_fee_cash_micros": fees,
        "cash_budget_mode": cash_budget_mode,
        "levels_consumed": levels_consumed,
    }
    if order_type == "FOK" and insufficient:
        return asdict(_empty_or_killed(
            side="BUY", order_type=order_type, status="REJECTED_FOK_INSUFFICIENT_DEPTH",
            requested_cash=requested, requested_shares=None,
            best=asks[0][0] if asks else None, insufficient=True, preview=preview,
            cash_budget_mode=cash_budget_mode, fee_rate_ppm=rate_ppm,
            fee_precision_micros=fee_precision_micros,
        ))
    return asdict(_filled_result(
        side="BUY", order_type=order_type, requested_cash=requested,
        requested_shares=None, filled_cash=spent, filled_shares=shares,
        unfilled_cash=remaining, unfilled_shares=None,
        best=asks[0][0] if asks else None, worst=worst,
        levels_consumed=levels_consumed, insufficient=insufficient,
        cash_budget_mode=cash_budget_mode, taker_fee=fees,
        fee_rate_ppm=rate_ppm, fee_precision_micros=fee_precision_micros,
    ))


def simulate_share_sell(book, shares, order_type="FAK", *, fee_rate=None,
                        fee_precision_micros=DEFAULT_FEE_PRECISION_MICROS):
    """Walk bids for an exact-share SELL."""
    order_type = _validate_order_type(order_type)
    requested = _micros(shares, "shares")
    rate_ppm = _fee_rate_ppm(fee_rate)
    bids = _levels((book or {}).get("bids"), bids=True)
    remaining = requested
    proceeds = 0
    filled_shares = 0
    fees = 0 if rate_ppm is not None else None
    levels_consumed = 0
    worst = None
    for price, available_shares in bids:
        if remaining <= 0:
            break
        taken_shares = min(remaining, available_shares)
        taken_cash = price * taken_shares // MICROS
        remaining -= taken_shares
        filled_shares += taken_shares
        proceeds += taken_cash
        taken_fee = _fee_cash_micros(
            taken_shares, price, rate_ppm, fee_precision_micros
        )
        if fees is not None:
            fees += taken_fee or 0
        worst = price
        levels_consumed += 1
    insufficient = remaining > 0
    preview = {
        "filled_cash_micros": proceeds,
        "filled_shares_micros": filled_shares,
        "unfilled_shares_micros": remaining,
        "levels_consumed": levels_consumed,
    }
    if order_type == "FOK" and insufficient:
        return asdict(_empty_or_killed(
            side="SELL", order_type=order_type, status="REJECTED_FOK_INSUFFICIENT_DEPTH",
            requested_cash=None, requested_shares=requested,
            best=bids[0][0] if bids else None, insufficient=True, preview=preview,
            cash_budget_mode=None, fee_rate_ppm=rate_ppm,
            fee_precision_micros=fee_precision_micros,
        ))
    return asdict(_filled_result(
        side="SELL", order_type=order_type, requested_cash=None,
        requested_shares=requested, filled_cash=proceeds, filled_shares=filled_shares,
        unfilled_cash=None, unfilled_shares=remaining,
        best=bids[0][0] if bids else None, worst=worst,
        levels_consumed=levels_consumed, insufficient=insufficient,
        cash_budget_mode=None, taker_fee=fees,
        fee_rate_ppm=rate_ppm, fee_precision_micros=fee_precision_micros,
    ))


def _filled_result(*, side, order_type, requested_cash, requested_shares,
                   filled_cash, filled_shares, unfilled_cash, unfilled_shares,
                   best, worst, levels_consumed, insufficient,
                   cash_budget_mode, taker_fee, fee_rate_ppm,
                   fee_precision_micros):
    vwap = _ratio(filled_cash * MICROS, filled_shares)
    slippage = (
        vwap - best if side == "BUY" and vwap is not None and best is not None
        else best - vwap if side == "SELL" and vwap is not None and best is not None
        else None
    )
    return MatchResult(
        engine_version=ENGINE_VERSION,
        model_scope="displayed_depth_upper_bound_no_queue_cancel_or_fill_probability",
        side=side, order_type=order_type,
        status=("PARTIAL_FILL" if insufficient and filled_shares else
                "NO_FILL" if not filled_shares else "FULL_FILL"),
        requested_cash_micros=requested_cash,
        requested_shares_micros=requested_shares,
        cash_budget_mode=cash_budget_mode,
        filled_cash_micros=filled_cash,
        filled_shares_micros=filled_shares,
        taker_fee_cash_micros=taker_fee,
        total_cash_debit_micros=(
            filled_cash + taker_fee
            if side == "BUY" and taker_fee is not None else None
        ),
        net_cash_proceeds_micros=(
            max(0, filled_cash - taker_fee)
            if side == "SELL" and taker_fee is not None else None
        ),
        fee_rate_ppm=fee_rate_ppm,
        fee_precision_micros=int(fee_precision_micros),
        fee_model_status=(
            "DOCUMENTED_FORMULA_PER_FILL_REQUIRES_VENUE_GOLDEN_VECTOR"
            if fee_rate_ppm is not None else "UNKNOWN_FEE_RATE_NOT_APPLIED"
        ),
        rounding_policy=ROUNDING_POLICY,
        residual_budget_dust_micros=unfilled_cash,
        residual_share_dust_micros=unfilled_shares,
        unfilled_cash_micros=unfilled_cash,
        unfilled_shares_micros=unfilled_shares,
        fill_ratio_ppm=(
            _ratio(filled_cash * MICROS, requested_cash) or 0
            if requested_cash is not None else
            _ratio(filled_shares * MICROS, requested_shares) or 0
        ),
        best_price_micros=best, worst_fill_price_micros=worst,
        vwap_price_micros=vwap,
        side_adjusted_slippage_micros=slippage,
        side_adjusted_slippage_bps=(
            _ratio(slippage * 10_000, best)
            if slippage is not None and best else None
        ),
        levels_consumed=levels_consumed,
        insufficient_displayed_depth=insufficient,
        preview_fill_before_fok_kill=None,
    )


def _empty_or_killed(*, side, order_type, status, requested_cash,
                     requested_shares, best, insufficient, preview,
                     cash_budget_mode, fee_rate_ppm, fee_precision_micros):
    return MatchResult(
        engine_version=ENGINE_VERSION,
        model_scope="displayed_depth_upper_bound_no_queue_cancel_or_fill_probability",
        side=side, order_type=order_type, status=status,
        requested_cash_micros=requested_cash,
        requested_shares_micros=requested_shares,
        cash_budget_mode=cash_budget_mode,
        filled_cash_micros=0, filled_shares_micros=0,
        taker_fee_cash_micros=(0 if fee_rate_ppm is not None else None),
        total_cash_debit_micros=(0 if side == "BUY" and fee_rate_ppm is not None else None),
        net_cash_proceeds_micros=(0 if side == "SELL" and fee_rate_ppm is not None else None),
        fee_rate_ppm=fee_rate_ppm,
        fee_precision_micros=int(fee_precision_micros),
        fee_model_status=(
            "DOCUMENTED_FORMULA_PER_FILL_REQUIRES_VENUE_GOLDEN_VECTOR"
            if fee_rate_ppm is not None else "UNKNOWN_FEE_RATE_NOT_APPLIED"
        ),
        rounding_policy=ROUNDING_POLICY,
        residual_budget_dust_micros=requested_cash,
        residual_share_dust_micros=requested_shares,
        unfilled_cash_micros=requested_cash,
        unfilled_shares_micros=requested_shares,
        fill_ratio_ppm=0, best_price_micros=best,
        worst_fill_price_micros=None, vwap_price_micros=None,
        side_adjusted_slippage_micros=None,
        side_adjusted_slippage_bps=None, levels_consumed=0,
        insufficient_displayed_depth=insufficient,
        preview_fill_before_fok_kill=preview,
    )


def simulate_journals(journal_paths, tiers_usd=(3, 5, 10), order_type="FAK", *,
                      fee_rate=None, cash_budget_mode="ORDER_NOTIONAL"):
    """Yield counterfactuals for every causally-known recorded checkpoint."""
    bases = {}
    paths = [Path(path) for path in journal_paths]
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event_type") != "wallet_signal":
                    continue
                signal_id = str(record.get("signal_event_id") or record.get("correlation_id") or "")
                signal = record.get("signal") or {}
                sell_sizes = {}
                for tier, item in ((record.get("shadow_lifecycle") or {}).get("tiers") or {}).items():
                    execution = (item or {}).get("execution") or {}
                    sell_sizes[str(tier)] = execution.get("requested_shares_micros")
                bases[(str(path), signal_id)] = {
                    "signal_event_id": signal_id,
                    "wallet": str(signal.get("user_address") or "").lower(),
                    "market_slug": signal.get("market_slug"),
                    "side": str(signal.get("side") or "").upper(),
                    "sell_sizes": sell_sizes,
                }

    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event_type") == "wallet_signal":
                    signal_id = str(record.get("signal_event_id") or record.get("correlation_id") or "")
                    delay_ms = 0
                    book = record.get("decision_book") or {}
                    causal_valid = (
                        bool(book) and isinstance(record.get("decision_book_age_ns"), int)
                        and record.get("decision_book_age_ns") >= 0
                    )
                elif record.get("event_type") == "delayed_book_observation":
                    signal_id = str(record.get("correlation_id") or "")
                    delay_ms = int(record.get("target_delay_ms") or 0)
                    book = record.get("book") or {}
                    causal_valid = record.get("book_known_by_capture_deadline") is True
                else:
                    continue
                base = bases.get((str(path), signal_id))
                if base is None:
                    continue
                for tier in tiers_usd:
                    result = None
                    status = "CAUSAL_BOOK_UNAVAILABLE"
                    if causal_valid and base["side"] == "BUY":
                        observed_fee_rate = (
                            fee_rate if fee_rate is not None else book.get("fee_rate")
                        )
                        result = simulate_cash_buy(
                            book, tier, order_type, fee_rate=observed_fee_rate,
                            cash_budget_mode=cash_budget_mode,
                        )
                        status = result["status"]
                    elif causal_valid and base["side"] == "SELL":
                        requested = base["sell_sizes"].get(str(tier))
                        if requested is not None:
                            result = simulate_share_sell(
                                book, Decimal(int(requested)) / MICROS, order_type,
                                fee_rate=(
                                    fee_rate if fee_rate is not None
                                    else book.get("fee_rate")
                                ),
                            )
                            status = result["status"]
                        else:
                            status = "SELL_SIZE_UNAVAILABLE"
                    yield {
                        **base, "tier_usd": int(tier),
                        "observation_delay_ms": delay_ms,
                        "causal_valid": causal_valid,
                        "simulation_status": status,
                        "match_result": result,
                    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journals", nargs="+")
    parser.add_argument("--tiers", default="3,5,10")
    parser.add_argument("--order-type", choices=("FAK", "FOK"), default="FAK")
    parser.add_argument(
        "--fee-rate", type=Decimal,
        help="Explicit decimal rate, e.g. 0.05. Omit to use each book's recorded rate.",
    )
    parser.add_argument(
        "--cash-budget-mode", choices=("ORDER_NOTIONAL", "ALL_IN_CAPITAL"),
        default="ORDER_NOTIONAL",
    )
    parser.add_argument("--output", default="research/output/virtual-matches.jsonl")
    args = parser.parse_args(argv)
    tiers = tuple(int(item.strip()) for item in args.tiers.split(",") if item.strip())
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output.open("w", encoding="utf-8") as handle:
        for row in simulate_journals(
            args.journals, tiers, args.order_type,
            fee_rate=args.fee_rate, cash_budget_mode=args.cash_budget_mode,
        ):
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
    print(json.dumps({
        "output": str(output), "row_count": count,
        "engine_version": ENGINE_VERSION,
        "model_scope": "displayed_depth_upper_bound_no_queue_cancel_or_fill_probability",
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
