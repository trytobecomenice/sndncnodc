#!/usr/bin/env python3
"""Offline Phase-0 signal/book autopsy pipeline.

The normalizer is deliberately standard-library only and reads the source
journal twice.  It joins delayed books to signals by the recorder correlation
ID, never by a nearest wall-clock timestamp.  Optional Polars/Plotly analysis
is isolated behind a separate research requirements file.
"""

import argparse
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path


MICROS = 1_000_000
NORMALIZED_SCHEMA_VERSION = "phase0-autopsy-observation-v1"

FIELDNAMES = (
    "schema_version",
    "journal_path",
    "signal_event_id",
    "api_trade_id",
    "wallet",
    "market_slug",
    "outcome",
    "side",
    "source_reported_timestamp_ms",
    "first_local_seen_timestamp_ms",
    "reported_visibility_lag_ms",
    "tier_usd",
    "observation_delay_ms",
    "observation_label",
    "source_price_micros",
    "t0_executable_price_micros",
    "executable_price_micros",
    "deterioration_from_source_micros",
    "deterioration_from_t0_micros",
    "requested_usd_micros",
    "filled_usd_micros",
    "requested_shares_micros",
    "filled_shares_micros",
    "fill_ratio_ppm",
    "insufficient_liquidity",
    "causal_valid",
    "causal_status",
    "capture_lateness_ns",
    "book_received_timestamp_ms",
    "book_received_monotonic_ns",
    "book_local_generation",
    "book_reconnect_epoch",
    "book_hash",
    "quality_flags_json",
)


def _iter_jsonl(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield line_number, json.loads(line)
            except json.JSONDecodeError:
                # A recorder killed during append may leave one partial tail.
                # Its absence remains visible in the returned audit counters.
                yield line_number, None


def _price_micros(value):
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0 or parsed > 1:
        return None
    return int((parsed * MICROS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _execution_fields(execution, side):
    execution = execution or {}
    if side == "BUY":
        return {
            "executable_price_micros": execution.get("average_price_micros"),
            "requested_usd_micros": execution.get("requested_usd_micros"),
            "filled_usd_micros": execution.get("filled_usd_micros"),
            "requested_shares_micros": None,
            "filled_shares_micros": execution.get("shares_micros"),
            "fill_ratio_ppm": execution.get("fill_ratio_ppm"),
            "insufficient_liquidity": execution.get("insufficient_liquidity"),
        }
    return {
        "executable_price_micros": execution.get("average_price_micros"),
        "requested_usd_micros": None,
        "filled_usd_micros": None,
        "requested_shares_micros": execution.get("requested_shares_micros"),
        "filled_shares_micros": execution.get("filled_shares_micros"),
        "fill_ratio_ppm": execution.get("liquidation_ratio_ppm"),
        "insufficient_liquidity": execution.get("insufficient_liquidity"),
    }


def _deterioration(side, later_price, earlier_price):
    if later_price is None or earlier_price is None:
        return None
    if side == "BUY":
        return int(later_price) - int(earlier_price)
    if side == "SELL":
        return int(earlier_price) - int(later_price)
    return None


def _signal_base(record, journal_path):
    signal = record.get("signal") or {}
    side = str(signal.get("side") or "").upper()
    return {
        "journal_path": str(Path(journal_path)),
        "signal_event_id": record.get("signal_event_id") or record.get("correlation_id"),
        "api_trade_id": record.get("api_trade_id"),
        "wallet": str(signal.get("user_address") or "").lower(),
        "market_slug": signal.get("market_slug"),
        "outcome": signal.get("outcome"),
        "side": side,
        "source_reported_timestamp_ms": record.get("source_reported_timestamp_ms"),
        "first_local_seen_timestamp_ms": record.get("first_local_seen_timestamp_ms"),
        "reported_visibility_lag_ms": record.get("reported_visibility_lag_ms"),
        "source_price_micros": _price_micros(signal.get("price")),
        "quality_flags_json": json.dumps(
            sorted(record.get("quality_flags") or ()), separators=(",", ":")
        ),
        "shadow_lifecycle": record.get("shadow_lifecycle") or {},
        "decision_book": record.get("decision_book") or {},
        "decision_book_age_ns": record.get("decision_book_age_ns"),
        "signal_error": record.get("error"),
    }


def _t0_by_tier(base):
    result = {}
    for tier, item in (base["shadow_lifecycle"].get("tiers") or {}).items():
        execution = (item or {}).get("execution")
        fields = _execution_fields(execution, base["side"])
        result[str(tier)] = fields.get("executable_price_micros")
    return result


def _row(base, tier, delay_ms, execution, *, causal_valid, causal_status,
         book=None, capture_lateness_ns=None, t0_price=None):
    fields = _execution_fields(execution, base["side"])
    executable_price = fields["executable_price_micros"]
    book = book or {}
    row = {
        "schema_version": NORMALIZED_SCHEMA_VERSION,
        **{key: base.get(key) for key in (
            "journal_path", "signal_event_id", "api_trade_id", "wallet",
            "market_slug", "outcome", "side", "source_reported_timestamp_ms",
            "first_local_seen_timestamp_ms", "reported_visibility_lag_ms",
            "source_price_micros", "quality_flags_json",
        )},
        "tier_usd": int(tier),
        "observation_delay_ms": int(delay_ms),
        "observation_label": "T0" if int(delay_ms) == 0 else f"T+{int(delay_ms)}ms",
        "t0_executable_price_micros": t0_price,
        **fields,
        "deterioration_from_source_micros": _deterioration(
            base["side"], executable_price, base["source_price_micros"]
        ),
        "deterioration_from_t0_micros": _deterioration(
            base["side"], executable_price, t0_price
        ),
        "causal_valid": bool(causal_valid and executable_price is not None),
        "causal_status": causal_status,
        "capture_lateness_ns": capture_lateness_ns,
        "book_received_timestamp_ms": book.get("received_timestamp_ms"),
        "book_received_monotonic_ns": book.get("received_monotonic_ns"),
        "book_local_generation": book.get("local_generation"),
        "book_reconnect_epoch": book.get("reconnect_epoch"),
        "book_hash": book.get("book_hash"),
    }
    return row


def normalize_journals(journal_paths, output_csv):
    """Normalize journal observations to a narrow, analysis-safe CSV.

    The first pass keeps only signal-sized attribution records.  The second
    pass performs exact-ID pairing.  No delayed record is promoted to causal
    evidence unless the online recorder proved its book was known by the
    target deadline.
    """
    paths = [Path(path) for path in journal_paths]
    signals = {}
    malformed_lines = 0
    duplicate_signals = 0
    for path in paths:
        for _line_number, record in _iter_jsonl(path):
            if record is None:
                malformed_lines += 1
                continue
            if record.get("event_type") != "wallet_signal":
                continue
            base = _signal_base(record, path)
            signal_id = base["signal_event_id"]
            if not signal_id:
                continue
            key = (str(path), str(signal_id))
            if key in signals:
                duplicate_signals += 1
            signals[key] = base

    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    unmatched_delayed = 0
    duplicate_observations = 0
    seen_observations = set()
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()

        for key, base in signals.items():
            t0_prices = _t0_by_tier(base)
            lifecycle = base["shadow_lifecycle"]
            decision_age = base.get("decision_book_age_ns")
            base_valid = (
                base["side"] in {"BUY", "SELL"}
                and base.get("signal_error") is None
                and bool(base.get("decision_book"))
                and isinstance(decision_age, int)
                and decision_age >= 0
            )
            for tier, item in (lifecycle.get("tiers") or {}).items():
                execution = (item or {}).get("execution")
                action = str((item or {}).get("action") or "unknown")
                valid = base_valid and action in {"hypothetical_buy", "hypothetical_sell"}
                status = (
                    "known_at_signal_and_executable"
                    if valid else f"not_causal_or_not_executable:{action}"
                )
                row = _row(
                    base, tier, 0, execution,
                    causal_valid=valid,
                    causal_status=status,
                    book=base["decision_book"],
                    t0_price=t0_prices.get(str(tier)),
                )
                writer.writerow(row)
                rows_written += 1

        for path in paths:
            for _line_number, record in _iter_jsonl(path):
                if record is None or record.get("event_type") != "delayed_book_observation":
                    continue
                signal_id = record.get("correlation_id")
                base = signals.get((str(path), str(signal_id)))
                if base is None:
                    unmatched_delayed += 1
                    continue
                observation_id = record.get("event_id")
                dedup_key = (str(path), str(observation_id))
                if observation_id and dedup_key in seen_observations:
                    duplicate_observations += 1
                    continue
                seen_observations.add(dedup_key)
                target = int(record.get("target_delay_ms") or 0)
                known = record.get("book_known_by_capture_deadline") is True
                t0_prices = _t0_by_tier(base)
                for tier, execution in (record.get("tier_execution_observations") or {}).items():
                    status = str(record.get("target_snapshot_status") or "unknown")
                    row = _row(
                        base, tier, target, execution,
                        causal_valid=known,
                        causal_status=status,
                        book=record.get("book"),
                        capture_lateness_ns=record.get("capture_lateness_ns"),
                        t0_price=t0_prices.get(str(tier)),
                    )
                    writer.writerow(row)
                    rows_written += 1

    return {
        "schema_version": NORMALIZED_SCHEMA_VERSION,
        "journal_count": len(paths),
        "signal_count": len(signals),
        "rows_written": rows_written,
        "malformed_json_lines": malformed_lines,
        "duplicate_signal_records": duplicate_signals,
        "duplicate_delayed_observations": duplicate_observations,
        "unmatched_delayed_observations": unmatched_delayed,
        "output_csv": str(output_csv),
        "join_contract": "exact_journal_path_plus_signal_correlation_id",
    }


def analyze_with_polars(normalized_csv, output_dir, write_plot=False):
    """Create Parquet, coverage/decay summaries, and an optional HTML chart."""
    try:
        import polars as pl
    except ImportError as exc:
        raise RuntimeError(
            "Polars analysis is optional; install requirements-phase0-analysis.txt "
            "or run with --normalize-only"
        ) from exc

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    frame = pl.read_csv(normalized_csv, try_parse_dates=False)
    parquet_path = output_dir / "phase0_autopsy_observations.parquet"
    frame.write_parquet(parquet_path, compression="zstd")

    group_keys = ["side", "tier_usd", "observation_delay_ms", "observation_label"]
    coverage = frame.group_by(group_keys).agg(
        pl.len().alias("observation_count"),
        pl.col("causal_valid").sum().alias("causal_valid_count"),
        pl.col("executable_price_micros").is_not_null().sum().alias("priced_count"),
        pl.col("insufficient_liquidity").sum().alias("insufficient_liquidity_count"),
    )
    valid = frame.filter(
        pl.col("causal_valid") & pl.col("executable_price_micros").is_not_null()
    )
    decay = valid.group_by(group_keys).agg(
        pl.col("deterioration_from_source_micros").median().alias(
            "source_deterioration_p50_micros"
        ),
        pl.col("deterioration_from_source_micros").quantile(0.1).alias(
            "source_deterioration_p10_micros"
        ),
        pl.col("deterioration_from_source_micros").quantile(0.9).alias(
            "source_deterioration_p90_micros"
        ),
        pl.col("deterioration_from_t0_micros").median().alias(
            "t0_deterioration_p50_micros"
        ),
        pl.col("capture_lateness_ns").median().alias("capture_lateness_p50_ns"),
    )
    summary = coverage.join(decay, on=group_keys, how="left").sort(group_keys)
    summary = summary.with_columns(
        (pl.col("causal_valid_count") / pl.col("observation_count")).alias(
            "causal_coverage_ratio"
        )
    )
    summary_path = output_dir / "phase0_autopsy_summary.json"
    summary_path.write_text(
        json.dumps(summary.to_dicts(), indent=2, sort_keys=True), encoding="utf-8"
    )

    plot_path = None
    if write_plot:
        try:
            import plotly.express as px
        except ImportError as exc:
            raise RuntimeError(
                "Plotly is required only for --plot; install "
                "requirements-phase0-analysis.txt"
            ) from exc
        plot_frame = summary.filter(
            pl.col("t0_deterioration_p50_micros").is_not_null()
        ).with_columns(
            (pl.col("t0_deterioration_p50_micros") / MICROS).alias(
                "median_deterioration_from_t0"
            ),
            pl.concat_str(
                [pl.col("side"), pl.lit(" $"), pl.col("tier_usd").cast(pl.String)]
            ).alias("series"),
        )
        figure = px.line(
            plot_frame.to_pandas(),
            x="observation_delay_ms",
            y="median_deterioration_from_t0",
            color="series",
            markers=True,
            title="Phase-0 executable VWAP deterioration from T0",
            labels={
                "observation_delay_ms": "Delay after first local signal visibility (ms)",
                "median_deterioration_from_t0": "Median side-adjusted price deterioration",
            },
        )
        plot_path = output_dir / "phase0_alpha_decay.html"
        figure.write_html(plot_path, include_plotlyjs="cdn")

    return {
        "parquet": str(parquet_path),
        "summary": str(summary_path),
        "plot": str(plot_path) if plot_path else None,
        "interpretation_guard": (
            "Positive deterioration is worse for the copier on both BUY and SELL. "
            "Only causal_valid rows enter decay estimates; these remain recorder-known "
            "book observations, not proof of exchange fill priority."
        ),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journals", nargs="+", help="Phase-0 JSONL journal(s)")
    parser.add_argument("--output-dir", default="data/phase0-autopsy")
    parser.add_argument("--normalize-only", action="store_true")
    parser.add_argument("--plot", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    output_dir = Path(args.output_dir)
    normalized_csv = output_dir / "phase0_autopsy_observations.csv"
    result = {
        "normalization": normalize_journals(args.journals, normalized_csv),
        "analysis": None,
    }
    if not args.normalize_only:
        result["analysis"] = analyze_with_polars(
            normalized_csv, output_dir, write_plot=args.plot
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
