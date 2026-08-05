#!/usr/bin/env python3
"""Recoverable BUY interlock for execution-integrity degradation.

This is deliberately separate from the persistent drawdown/capital kill
switch in risk_manager.py. The interlock trips immediately when the bot
cannot prove that a new entry would be based on timely, coherent data, but
recovers automatically only after a sustained healthy window. It never
gates SELL/reduce-only/closeout paths.

The state transition is a pure function: callers own sampling, persistence,
metrics, and alerts. That makes recorded samples replayable without wall
clock or database access.
"""

from dataclasses import dataclass
from enum import Enum
import math


class InterlockStatus(str, Enum):
    HEALTHY = "healthy"
    INTERLOCKED = "interlocked"


@dataclass(frozen=True)
class IntegrityThresholds:
    max_event_loop_lag_ms: float
    max_market_data_age_ms: float
    max_decision_queue_age_ms: float
    recovery_window_ms: int
    min_recovery_samples: int = 3

    def __post_init__(self):
        numeric_limits = (
            self.max_event_loop_lag_ms,
            self.max_market_data_age_ms,
            self.max_decision_queue_age_ms,
            self.recovery_window_ms,
        )
        if any(not math.isfinite(float(value)) or value < 0 for value in numeric_limits):
            raise ValueError("integrity thresholds must be finite and non-negative")
        if self.min_recovery_samples < 1:
            raise ValueError("min_recovery_samples must be at least 1")


@dataclass(frozen=True)
class IntegritySample:
    observed_monotonic_ms: int
    event_loop_lag_ms: float
    market_data_age_ms: float
    decision_queue_age_ms: float
    sequence_coherent: bool
    minimum_audit_available: bool


@dataclass(frozen=True)
class EntryInterlockState:
    status: InterlockStatus = InterlockStatus.HEALTHY
    changed_at_monotonic_ms: int = 0
    last_observed_monotonic_ms: int = 0
    reasons: tuple = ()
    recovery_started_monotonic_ms: int | None = None
    healthy_recovery_samples: int = 0

    @property
    def active(self):
        return self.status == InterlockStatus.INTERLOCKED

    def risk_value(self):
        """JSON-safe value for db.bot_risk_state, or None when healthy."""
        if not self.active:
            return None
        return {
            "active": True,
            "status": self.status.value,
            "triggered_at_monotonic_ms": self.changed_at_monotonic_ms,
            "last_observed_monotonic_ms": self.last_observed_monotonic_ms,
            "reasons": list(self.reasons),
            "recovery_started_monotonic_ms": self.recovery_started_monotonic_ms,
            "healthy_recovery_samples": self.healthy_recovery_samples,
        }


def entry_interlock_state_from_risk_value(value, observed_monotonic_ms):
    """Restore persisted active/healthy state conservatively after restart.

    Monotonic timestamps cannot cross a process restart, so an active value
    restarts its recovery window from the current monotonic observation. A
    malformed present value remains interlocked instead of becoming an
    accidental fail-open.
    """
    observed_monotonic_ms = int(observed_monotonic_ms)
    if value is None:
        return EntryInterlockState(
            status=InterlockStatus.HEALTHY,
            changed_at_monotonic_ms=observed_monotonic_ms,
            last_observed_monotonic_ms=observed_monotonic_ms,
        )
    if isinstance(value, dict):
        explicitly_healthy = value.get("active") is False and value.get("status") == "healthy"
        if explicitly_healthy:
            return EntryInterlockState(
                status=InterlockStatus.HEALTHY,
                changed_at_monotonic_ms=observed_monotonic_ms,
                last_observed_monotonic_ms=observed_monotonic_ms,
            )
        raw_reasons = value.get("reasons")
        if isinstance(raw_reasons, (list, tuple)) and raw_reasons:
            reasons = tuple(str(reason) for reason in raw_reasons)
        else:
            reasons = ("persisted_entry_interlock_state_malformed",)
    else:
        reasons = ("persisted_entry_interlock_state_malformed",)
    return EntryInterlockState(
        status=InterlockStatus.INTERLOCKED,
        changed_at_monotonic_ms=observed_monotonic_ms,
        last_observed_monotonic_ms=observed_monotonic_ms,
        reasons=reasons,
    )


@dataclass(frozen=True)
class InterlockTransition:
    previous_status: InterlockStatus
    state: EntryInterlockState

    @property
    def changed(self):
        return self.previous_status != self.state.status


def _invalid_or_exceeds(name, value, limit):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return f"{name}_invalid"
    if not math.isfinite(numeric) or numeric < 0:
        return f"{name}_invalid"
    if numeric > limit:
        return f"{name}_exceeded"
    return None


def integrity_breaches(sample, thresholds, previous_state=None):
    """Return stable machine-readable reasons that make a BUY unsafe."""
    reasons = []
    if sample.observed_monotonic_ms < 0:
        reasons.append("monotonic_clock_invalid")
    if previous_state and sample.observed_monotonic_ms < previous_state.last_observed_monotonic_ms:
        reasons.append("monotonic_clock_regressed")

    for name, value, limit in (
        ("event_loop_lag", sample.event_loop_lag_ms, thresholds.max_event_loop_lag_ms),
        ("market_data_age", sample.market_data_age_ms, thresholds.max_market_data_age_ms),
        ("decision_queue_age", sample.decision_queue_age_ms, thresholds.max_decision_queue_age_ms),
    ):
        reason = _invalid_or_exceeds(name, value, limit)
        if reason:
            reasons.append(reason)

    if sample.sequence_coherent is not True:
        reasons.append("sequence_not_coherent")
    if sample.minimum_audit_available is not True:
        reasons.append("minimum_audit_unavailable")
    return tuple(reasons)


def evaluate_entry_interlock(state, sample, thresholds):
    """Apply one integrity sample and return a deterministic transition.

    A single breach trips immediately. Recovery needs both a minimum count
    of consecutive healthy samples and a minimum healthy duration, so a
    brief good tick during a burst cannot reopen entries.
    """
    breaches = integrity_breaches(sample, thresholds, previous_state=state)
    previous_status = state.status

    if breaches:
        changed_at = (
            sample.observed_monotonic_ms
            if not state.active
            else state.changed_at_monotonic_ms
        )
        next_state = EntryInterlockState(
            status=InterlockStatus.INTERLOCKED,
            changed_at_monotonic_ms=changed_at,
            last_observed_monotonic_ms=sample.observed_monotonic_ms,
            reasons=breaches,
        )
        return InterlockTransition(previous_status, next_state)

    if not state.active:
        next_state = EntryInterlockState(
            status=InterlockStatus.HEALTHY,
            changed_at_monotonic_ms=state.changed_at_monotonic_ms,
            last_observed_monotonic_ms=sample.observed_monotonic_ms,
        )
        return InterlockTransition(previous_status, next_state)

    recovery_started = (
        state.recovery_started_monotonic_ms
        if state.recovery_started_monotonic_ms is not None
        else sample.observed_monotonic_ms
    )
    healthy_samples = state.healthy_recovery_samples + 1
    recovered = (
        healthy_samples >= thresholds.min_recovery_samples
        and sample.observed_monotonic_ms - recovery_started >= thresholds.recovery_window_ms
    )
    if recovered:
        next_state = EntryInterlockState(
            status=InterlockStatus.HEALTHY,
            changed_at_monotonic_ms=sample.observed_monotonic_ms,
            last_observed_monotonic_ms=sample.observed_monotonic_ms,
        )
    else:
        next_state = EntryInterlockState(
            status=InterlockStatus.INTERLOCKED,
            changed_at_monotonic_ms=state.changed_at_monotonic_ms,
            last_observed_monotonic_ms=sample.observed_monotonic_ms,
            reasons=state.reasons,
            recovery_started_monotonic_ms=recovery_started,
            healthy_recovery_samples=healthy_samples,
        )
    return InterlockTransition(previous_status, next_state)
