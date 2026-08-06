#!/usr/bin/env python3
"""Streamlit Phase-0 tactical research dashboard (read-only)."""

import json
from pathlib import Path

from research.autopsy_features import (
    activity_aware_clustering_capability,
    annotate_open_lot_topology_risk,
    cluster_signals,
    cohort_summary,
    cutoff_mtm_capability,
    extract_depth_observations,
    open_lots_by_wallet_tier,
    read_normalized_rows,
    realized_pnl_by_wallet_tier,
    session_label,
)


def main():
    import polars as pl
    import plotly.express as px
    import streamlit as st

    st.set_page_config(page_title="Phase 0 Quant Autopsy", layout="wide")
    st.title("Phase 0 Quant Autopsy")
    st.caption("Offline observation dashboard — no DB writes, roster actions, or trading controls")

    normalized_path = st.sidebar.text_input(
        "Normalized CSV", "data/phase0-autopsy/phase0_autopsy_observations.csv"
    )
    journal_text = st.sidebar.text_input(
        "Source journal(s), comma-separated", "data/phase0-soak.jsonl"
    )
    cluster_window_ms = st.sidebar.number_input(
        "Maximum adjacent signal gap (ms)", min_value=0, value=300_000, step=1_000
    )
    cluster_diameter_ms = st.sidebar.number_input(
        "Maximum cluster diameter (ms)", min_value=0, value=300_000, step=1_000
    )
    zombie_age_hours = st.sidebar.number_input(
        "Open-lot aging warning (hours)", min_value=0.0, value=48.0, step=1.0
    )
    current_graph_path = st.sidebar.text_input("Current topology snapshot (optional)", "")
    previous_graph_path = st.sidebar.text_input("Previous topology snapshot (optional)", "")
    session_timezone = st.sidebar.text_input("Session timezone", "America/New_York")
    session_start = st.sidebar.text_input("US-hours start", "09:30")
    session_end = st.sidebar.text_input("US-hours end", "16:00")

    if not Path(normalized_path).exists():
        st.info("Run phase0_autopsy.py first, then point this dashboard at its normalized CSV.")
        return
    journal_paths = [Path(item.strip()) for item in journal_text.split(",") if item.strip()]
    existing_journals = [path for path in journal_paths if path.exists()]

    rows = list(read_normalized_rows(normalized_path))
    assignments, clusters = cluster_signals(
        rows, int(cluster_window_ms), int(cluster_diameter_ms)
    )
    realized = realized_pnl_by_wallet_tier(existing_journals) if existing_journals else {}
    open_lots, open_lot_details = (
        open_lots_by_wallet_tier(existing_journals) if existing_journals else ({}, [])
    )
    current_graph = None
    previous_graph = None
    if current_graph_path and Path(current_graph_path).exists():
        current_graph = json.loads(Path(current_graph_path).read_text(encoding="utf-8"))
    if previous_graph_path and Path(previous_graph_path).exists():
        previous_graph = json.loads(Path(previous_graph_path).read_text(encoding="utf-8"))
    if current_graph is not None:
        open_lot_details = annotate_open_lot_topology_risk(
            open_lot_details, current_graph, previous_graph
        )
    cohort = cohort_summary(rows, assignments, realized, open_lots)
    for item in cohort:
        oldest = item.get("oldest_open_lot_age_hours")
        item["open_lot_age_status"] = (
            "AGING_WARNING"
            if oldest is not None and oldest >= float(zombie_age_hours)
            else "WITHIN_DISPLAY_THRESHOLD"
        )

    for row in rows:
        row["signal_cluster_id"] = assignments.get(
            str(row.get("signal_event_id")), str(row.get("signal_event_id"))
        )
        row["session"] = session_label(
            row.get("first_local_seen_timestamp_ms"), session_timezone,
            session_start, session_end,
        )
        row["post_t0_decay"] = (
            row["deterioration_from_t0_micros"] / 1_000_000
            if row.get("deterioration_from_t0_micros") is not None else None
        )
    # Real journals can have fields that stay null for the first 100+ rows and
    # become integers later (for example delayed SELL evidence). Polars' small
    # default inference window then locks the column to Null and crashes. This
    # is offline research, so scan all rows for a stable schema.
    frame = (
        pl.DataFrame(rows, infer_schema_length=None, strict=False)
        if rows else pl.DataFrame()
    )

    overview_tab, decay_tab, micro_tab = st.tabs([
        "Cohort Overview", "Alpha Decay", "Microstructure"
    ])
    with overview_tab:
        st.subheader("Wallet cohort")
        st.dataframe(cohort, width="stretch")
        st.caption(
            "Independent cluster count prevents simultaneous same-market/same-side signals "
            "from inflating sample size. PnL is summed from matched SELL lifecycle events only."
        )
        st.subheader("Signal cascade candidates")
        st.dataframe(clusters, width="stretch")
        st.info(activity_aware_clustering_capability()["reason"])
        aging_count = sum(
            item["open_lot_age_status"] == "AGING_WARNING" for item in cohort
        )
        if aging_count:
            st.error(
                f"{aging_count} wallet/size cohort(s) contain open lots older than "
                f"the configured {float(zombie_age_hours):g}h display threshold."
            )
        topology_risk_count = sum(
            item.get("topology_risk_status") == "TOPOLOGY_RISK"
            for item in open_lot_details
        )
        if topology_risk_count:
            st.error(
                f"[TOPOLOGY_RISK] {topology_risk_count} open lot(s) crossed an observed "
                "event-membership change. Valuation is frozen: no arbitrary haircut was "
                "fabricated without an executable mark and calibrated policy."
            )
        st.subheader("Open shadow lots: committed capital and age only")
        st.dataframe(open_lot_details, width="stretch")
        st.caption(
            "Open committed capital is remaining recorded cost basis, not present value. "
            "Unknown cost/age stays explicit."
        )
        st.warning(cutoff_mtm_capability()["reason"])

    with decay_tab:
        if frame.is_empty():
            st.info("No normalized observations.")
        else:
            wallets = sorted(value for value in frame["wallet"].unique().to_list() if value)
            selected_wallets = st.multiselect("Wallet", wallets, default=wallets)
            tiers = sorted(frame["tier_usd"].drop_nulls().unique().to_list())
            selected_tiers = st.multiselect("Copy size", tiers, default=tiers)
            sessions = sorted(frame["session"].unique().to_list())
            selected_sessions = st.multiselect("Session", sessions, default=sessions)
            filtered = frame.filter(
                pl.col("wallet").is_in(selected_wallets)
                & pl.col("tier_usd").is_in(selected_tiers)
                & pl.col("session").is_in(selected_sessions)
                & pl.col("causal_valid")
                & pl.col("post_t0_decay").is_not_null()
            )
            curve = filtered.group_by([
                "tier_usd", "observation_delay_ms", "session"
            ]).agg(
                pl.col("post_t0_decay").median().alias("median_post_t0_decay"),
                pl.col("signal_cluster_id").n_unique().alias("independent_cluster_count"),
            ).sort("observation_delay_ms")
            figure = px.line(
                curve.to_pandas(), x="observation_delay_ms", y="median_post_t0_decay",
                color="session", line_dash="tier_usd", markers=True,
                hover_data=["independent_cluster_count"],
                title="Side-adjusted executable price decay from T0",
            )
            st.plotly_chart(figure, width="stretch")
            st.dataframe(curve, width="stretch")

    with micro_tab:
        if not existing_journals:
            st.info("Provide the source JSONL journal to inspect visible depth checkpoints.")
        else:
            depth = extract_depth_observations(existing_journals)
            depth_frame = (
                pl.DataFrame(depth, infer_schema_length=None, strict=False)
                if depth else pl.DataFrame()
            )
            if depth_frame.is_empty():
                st.info("No book checkpoints available.")
            else:
                depth_markets = sorted(
                    value for value in depth_frame["market_slug"].unique().to_list()
                    if value
                )
                selected_depth_markets = st.multiselect(
                    "Microstructure markets",
                    depth_markets,
                    default=depth_markets[: min(6, len(depth_markets))],
                    help="Limit facets so a large market universe stays readable.",
                )
                if not selected_depth_markets:
                    st.info("Select at least one market to draw depth checkpoints.")
                    return
                depth_frame = depth_frame.filter(
                    pl.col("market_slug").is_in(selected_depth_markets)
                )
                depth_long = depth_frame.unpivot(
                    index=["signal_event_id", "wallet", "market_slug", "observation_delay_ms"],
                    on=["visible_bid_notional_usd", "visible_ask_notional_usd"],
                    variable_name="book_side", value_name="visible_notional_usd",
                )
                figure = px.bar(
                    depth_long.to_pandas(), x="observation_delay_ms",
                    y="visible_notional_usd", color="book_side", barmode="group",
                    facet_row="market_slug", title="Visible recorded book notional by checkpoint",
                )
                st.plotly_chart(figure, width="stretch")
                st.caption("Visible depth is not guaranteed executable or persistent liquidity.")


if __name__ == "__main__":
    main()
