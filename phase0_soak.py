#!/usr/bin/env python3
"""Deterministic Phase-0 soak research state.

This module is deliberately pure: no network, DB, key, order, sleep, or wall
clock access.  A standalone recorder feeds it distinct wallet fills plus the
book visible at local detection time.  It preserves incremental conviction and
pairs source reductions with proportional shadow exits at executable bid VWAP.
"""

from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from phase0_attribution import (
    MICROS,
    PHASE0_ATTRIBUTION_VERSION,
    build_phase0_attribution,
    build_sell_execution_observation,
)


SOAK_SCHEMA_VERSION = 1
DEFAULT_TIERS_USD = (3, 5, 10)


def _micros(value, field):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return int((parsed * MICROS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _rounded_ratio(numerator, denominator):
    if denominator <= 0:
        return 0
    return (numerator + denominator // 2) // denominator


def _source_trade_shares(trade, source_price_micros, source_usd_micros):
    """Prefer the source-reported token quantity over cash/price inference.

    Polymarket activity ``usdcSize`` is a cash field and can reflect fees;
    dividing it by price can therefore distort partial-reduction ratios.
    The untouched raw activity row is authoritative when it exposes ``size``.
    """
    raw_size = (trade.get("_raw_record") or {}).get("size")
    if raw_size is not None:
        return _micros(raw_size, "source_share_size"), "source_reported_share_size"
    return (
        _rounded_ratio(source_usd_micros * MICROS, source_price_micros),
        "fallback_cash_notional_div_price",
    )


def signal_key(trade):
    return "|".join(
        (
            str(trade.get("user_address") or "").lower(),
            str(trade.get("market_slug") or ""),
            str(trade.get("outcome") or ""),
        )
    )


class ConvictionTracker:
    """Counts distinct incremental fills; dedup remains exact trade-id only."""

    def __init__(self, window_ms=3_600_000, started_ms=None):
        self.window_ms = int(window_ms)
        self.started_ms = int(started_ms) if started_ms is not None else None
        self._events = defaultdict(deque)

    def observe(self, trade, received_timestamp_ms):
        now_ms = int(received_timestamp_ms)
        if self.started_ms is None:
            self.started_ms = now_ms
        key = (*signal_key(trade).split("|"), str(trade.get("side") or "").upper())
        events = self._events[key]
        cutoff = now_ms - self.window_ms
        while events and events[0][0] < cutoff:
            events.popleft()
        size_micros = _micros(trade.get("size_usd") or 0, "size_usd")
        events.append((now_ms, size_micros))
        return {
            "window_ms": self.window_ms,
            "wallet_recent_trade_count_1h": len(events),
            "wallet_recent_trade_usd_micros_1h": sum(item[1] for item in events),
            "window_complete_since_process_start": now_ms - self.started_ms >= self.window_ms,
            "dedup_identity": "caller_supplied_distinct_fill_event",
        }


class Phase0ShadowLedger:
    """Pairs source BUY/SELL fills with three exact-size shadow ledgers."""

    def __init__(self, tiers_usd=DEFAULT_TIERS_USD,
                 lot_allocation_policy="worst_execution_first"):
        if lot_allocation_policy not in {
            "worst_execution_first", "fifo", "lifo"
        }:
            raise ValueError("unsupported lot allocation policy")
        self.tiers_usd = tuple(int(tier) for tier in tiers_usd)
        self.lot_allocation_policy = lot_allocation_policy
        self.source_shares_micros = {}
        self.shadow_lots = {tier: {} for tier in self.tiers_usd}
        self.realized_pnl_micros = {tier: 0 for tier in self.tiers_usd}

    def restore_key(self, key, ledger_after):
        source = int((ledger_after or {}).get("source_shares_micros") or 0)
        if source > 0:
            self.source_shares_micros[key] = source
        else:
            self.source_shares_micros.pop(key, None)
        for tier in self.tiers_usd:
            raw_lots = ((ledger_after or {}).get("shadow_lots") or {}).get(str(tier))
            if raw_lots:
                self.shadow_lots[tier][key] = [
                    {
                        "lot_id": str(raw["lot_id"]),
                        "shares_micros": int(raw["shares_micros"]),
                        "cost_basis_micros": (
                            int(raw["cost_basis_micros"])
                            if raw.get("cost_basis_micros") is not None else None
                        ),
                    }
                    for raw in raw_lots
                    if int(raw.get("shares_micros") or 0) > 0
                ]
            else:
                self.shadow_lots[tier].pop(key, None)
        totals = (ledger_after or {}).get("realized_pnl_micros") or {}
        for tier in self.tiers_usd:
            if str(tier) in totals:
                self.realized_pnl_micros[tier] = int(totals[str(tier)])

    def _after(self, key):
        lots = {}
        for tier in self.tiers_usd:
            position_lots = self.shadow_lots[tier].get(key) or []
            lots[str(tier)] = [dict(lot) for lot in position_lots]
        return {
            "source_shares_micros": self.source_shares_micros.get(key, 0),
            "shadow_lots": lots,
            "lot_allocation_policy": self.lot_allocation_policy,
            "realized_pnl_micros": {
                str(tier): self.realized_pnl_micros[tier] for tier in self.tiers_usd
            },
        }

    def _ordered_lots(self, lots):
        if self.lot_allocation_policy == "fifo":
            return list(lots)
        if self.lot_allocation_policy == "lifo":
            return list(reversed(lots))

        def conservative_cost_per_share(lot):
            cost = lot.get("cost_basis_micros")
            shares = int(lot.get("shares_micros") or 0)
            if cost is None or shares <= 0:
                return (1, 0)
            return (0, _rounded_ratio(int(cost) * MICROS, shares))

        return sorted(lots, key=conservative_cost_per_share, reverse=True)

    def _close_lots(self, lots, filled_shares):
        remaining = int(filled_shares)
        cost_closed = 0
        cost_known = True
        lot_closures = []
        for lot in self._ordered_lots(lots):
            if remaining <= 0:
                break
            closed = min(remaining, lot["shares_micros"])
            lot_cost = lot.get("cost_basis_micros")
            closed_cost = (
                lot_cost * closed // lot["shares_micros"]
                if lot_cost is not None and lot["shares_micros"] > 0 else None
            )
            if closed_cost is None:
                cost_known = False
            else:
                cost_closed += closed_cost
                lot["cost_basis_micros"] -= closed_cost
            lot["shares_micros"] -= closed
            remaining -= closed
            lot_closures.append({
                "lot_id": lot["lot_id"],
                "shares_closed_micros": closed,
                "cost_basis_closed_micros": closed_cost,
            })
        lots[:] = [lot for lot in lots if lot["shares_micros"] > 0]
        return (cost_closed if cost_known else None), lot_closures

    def apply_signal(self, trade, book):
        key = signal_key(trade)
        side = str(trade.get("side") or "").upper()
        source_price = _micros(trade.get("price"), "source_price")
        source_usd = _micros(trade.get("size_usd") or 0, "source_size_usd")
        source_trade_shares, source_share_basis = _source_trade_shares(
            trade, source_price, source_usd
        )
        prior_source = self.source_shares_micros.get(key, 0)

        if side == "BUY":
            action = "increase_observed_position" if prior_source > 0 else "open_observed_position"
            self.source_shares_micros[key] = prior_source + source_trade_shares
            tiers = {}
            for tier in self.tiers_usd:
                attribution = build_phase0_attribution(trade, book, tier)
                execution = attribution["buy_execution"]
                shares = int(execution["shares_micros"])
                cash = execution["net_cash_outflow_usd_micros"]
                if shares > 0:
                    self.shadow_lots[tier].setdefault(key, []).append({
                        "lot_id": str(
                            trade.get("_soak_event_id") or trade.get("trade_id") or "unknown"
                        ),
                        "shares_micros": shares,
                        "cost_basis_micros": int(cash) if cash is not None else None,
                    })
                tiers[str(tier)] = {
                    "action": "hypothetical_buy",
                    "attribution_version": PHASE0_ATTRIBUTION_VERSION,
                    "execution": execution,
                    "realized_pnl_usd_micros": None,
                }
            return {
                "source_position_action": action,
                "source_share_basis": source_share_basis,
                "source_reduce_fraction_ppm": None,
                "tiers": tiers,
                "ledger_after": self._after(key),
            }

        if side != "SELL":
            return {
                "source_position_action": "unknown",
                "source_share_basis": source_share_basis,
                "status": "unsupported_side",
                "tiers": {},
                "ledger_after": self._after(key),
            }

        if prior_source <= 0:
            fraction_ppm = None
            action = "sell_without_observed_source_inventory"
        else:
            fraction_ppm = min(MICROS, _rounded_ratio(source_trade_shares * MICROS, prior_source))
            action = "close_observed_position" if fraction_ppm >= MICROS else "reduce_observed_position"
            remaining_source = max(0, prior_source - source_trade_shares)
            if remaining_source:
                self.source_shares_micros[key] = remaining_source
            else:
                self.source_shares_micros.pop(key, None)

        tiers = {}
        for tier in self.tiers_usd:
            lots = self.shadow_lots[tier].get(key) or []
            if fraction_ppm is None or not lots:
                tiers[str(tier)] = {
                    "action": "unmatched_source_sell",
                    "copy_participation_status": "no_shadow_lots_for_source_reduction",
                    "execution": None,
                    "realized_pnl_usd_micros": None,
                }
                continue
            current_shares = sum(lot["shares_micros"] for lot in lots)
            requested_shares = current_shares * fraction_ppm // MICROS
            execution = build_sell_execution_observation(
                book, Decimal(requested_shares) / MICROS
            )
            filled_shares = int(execution["filled_shares_micros"])
            cost_closed, lot_closures = self._close_lots(lots, filled_shares)
            net_proceeds = execution["net_proceeds_usd_micros"]
            realized = (
                int(net_proceeds) - cost_closed
                if net_proceeds is not None and cost_closed is not None else None
            )
            if lots:
                self.shadow_lots[tier][key] = lots
            else:
                self.shadow_lots[tier].pop(key, None)
            if realized is not None:
                self.realized_pnl_micros[tier] += realized
            tiers[str(tier)] = {
                "action": "hypothetical_sell",
                "requested_fraction_ppm": fraction_ppm,
                "lot_allocation_policy": self.lot_allocation_policy,
                "lot_closures": lot_closures,
                "execution": execution,
                "cost_basis_closed_usd_micros": cost_closed,
                "realized_pnl_usd_micros": realized,
            }
        return {
            "source_position_action": action,
            "source_share_basis": source_share_basis,
            "source_reduce_fraction_ppm": fraction_ppm,
            "tiers": tiers,
            "ledger_after": self._after(key),
        }
