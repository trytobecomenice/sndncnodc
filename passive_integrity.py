#!/usr/bin/env python3
"""Signal-triggered integrity measurements with no polling loop.

Every value is derived from timestamps already attached to a signal or an
order-book response. Calling code invokes this once when a BUY decision is
about to be committed; this module starts no thread, timer, asyncio task, or
network request.
"""

from dataclasses import dataclass

from entry_interlock import IntegritySample


NANOSECONDS_PER_MILLISECOND = 1_000_000


def _elapsed_ms(later_ns, earlier_ns):
    if later_ns is None or earlier_ns is None:
        return None
    elapsed = (int(later_ns) - int(earlier_ns)) / NANOSECONDS_PER_MILLISECOND
    return max(0.0, elapsed)


@dataclass(frozen=True)
class PassiveIntegrityMeasurement:
    observed_monotonic_ms: int
    scheduler_lag_ms: float | None
    decision_queue_age_ms: float | None
    decision_path_age_ms: float | None
    book_server_age_at_receive_ms: float | None
    book_local_residence_ms: float | None
    effective_book_age_ms: float | None
    clock_uncertainty_ms: float
    quality_flags: tuple

    def to_interlock_sample(self, sequence_coherent, minimum_audit_available):
        # On the current synchronous poller, time waiting between the fetch
        # worker's enqueue stamp and process_trade() is the observable
        # scheduling delay. A future asyncio sidecar can supply its callback
        # scheduling stamp to the same field without adding a timer loop.
        return IntegritySample(
            observed_monotonic_ms=self.observed_monotonic_ms,
            event_loop_lag_ms=self.scheduler_lag_ms,
            market_data_age_ms=self.effective_book_age_ms,
            decision_queue_age_ms=self.decision_queue_age_ms,
            sequence_coherent=sequence_coherent,
            minimum_audit_available=minimum_audit_available,
        )


def measure_passive_integrity(signal, book, decision_monotonic_ns, decision_timestamp_ms):
    """Measure queue/path/book age exactly once at decision time.

    Server epoch time and local monotonic time are never subtracted from
    each other. Server age is estimated at local receipt using wall time;
    monotonic time then measures how long that already-received snapshot
    sat locally. Their sum is the effective age used by the interlock.
    """
    flags = set()
    enqueued_ns = signal.get("_enqueued_monotonic_ns")
    signal_received_ns = enqueued_ns
    queue_age_ms = _elapsed_ms(decision_monotonic_ns, enqueued_ns)
    if queue_age_ms is None:
        flags.add("missing_signal_monotonic_timestamp")

    book_timestamp_ms = book.get("book_timestamp_ms")
    book_received_timestamp_ms = book.get("received_timestamp_ms")
    book_received_monotonic_ns = book.get("received_monotonic_ns")

    server_age_ms = None
    clock_uncertainty_ms = 0.0
    if book_timestamp_ms is None or book_received_timestamp_ms is None:
        flags.add("missing_book_wall_timestamp")
    else:
        raw_server_age = float(book_received_timestamp_ms) - float(book_timestamp_ms)
        if raw_server_age < 0:
            flags.add("book_server_clock_ahead")
            clock_uncertainty_ms = abs(raw_server_age)
            server_age_ms = 0.0
        else:
            server_age_ms = raw_server_age

    local_residence_ms = _elapsed_ms(decision_monotonic_ns, book_received_monotonic_ns)
    if local_residence_ms is None:
        flags.add("missing_book_monotonic_timestamp")

    effective_book_age_ms = None
    if server_age_ms is not None and local_residence_ms is not None:
        effective_book_age_ms = server_age_ms + local_residence_ms
    if book_received_timestamp_ms is not None and local_residence_ms is not None:
        wall_residence_ms = float(decision_timestamp_ms) - float(book_received_timestamp_ms)
        if wall_residence_ms < 0:
            flags.add("local_wall_clock_regressed")
            clock_uncertainty_ms = max(clock_uncertainty_ms, abs(wall_residence_ms))
        else:
            clock_uncertainty_ms = max(
                clock_uncertainty_ms,
                abs(wall_residence_ms - local_residence_ms),
            )

    return PassiveIntegrityMeasurement(
        observed_monotonic_ms=int(decision_monotonic_ns // NANOSECONDS_PER_MILLISECOND),
        scheduler_lag_ms=queue_age_ms,
        decision_queue_age_ms=queue_age_ms,
        decision_path_age_ms=_elapsed_ms(decision_monotonic_ns, signal_received_ns),
        book_server_age_at_receive_ms=server_age_ms,
        book_local_residence_ms=local_residence_ms,
        effective_book_age_ms=effective_book_age_ms,
        clock_uncertainty_ms=clock_uncertainty_ms,
        quality_flags=tuple(sorted(flags)),
    )
