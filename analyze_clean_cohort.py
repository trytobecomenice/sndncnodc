#!/usr/bin/env python3
"""Deterministic trade- and wallet-cluster bootstrap for clean Paper rows."""

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import random
import sqlite3
import statistics

import config


def percentile(values, probability):
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def estimators(rows):
    returns = [row["pnl"] / row["cost"] for row in rows]
    return {
        "equal_weight_mean_return": statistics.mean(returns),
        "cost_weighted_roi": sum(row["pnl"] for row in rows) / sum(row["cost"] for row in rows),
    }


def bootstrap(rows, draws=10_000, seed=20260807, cluster_wallet=False):
    rng = random.Random(seed)
    samples = {"equal_weight_mean_return": [], "cost_weighted_roi": []}
    if cluster_wallet:
        groups = defaultdict(list)
        for row in rows:
            groups[row["wallet"]].append(row)
        units = sorted(groups)
        for _ in range(draws):
            sample = []
            for wallet in rng.choices(units, k=len(units)):
                sample.extend(groups[wallet])
            values = estimators(sample)
            for key in samples:
                samples[key].append(values[key])
    else:
        for _ in range(draws):
            values = estimators(rng.choices(rows, k=len(rows)))
            for key in samples:
                samples[key].append(values[key])
    return {key: {"lower_95": percentile(values, 0.025),
                  "upper_95": percentile(values, 0.975)}
            for key, values in samples.items()}


def summarize(rows, draws, seed):
    wins = [row for row in rows if row["pnl"] > 0]
    losses = [row for row in rows if row["pnl"] <= 0]
    win_return = statistics.mean(row["pnl"] / row["cost"] for row in wins)
    loss_return = -statistics.mean(row["pnl"] / row["cost"] for row in losses)
    observed = estimators(rows)
    return {
        "trade_count": len(rows),
        "wallet_count_effective_n": len({row["wallet"] for row in rows}),
        "market_count": len({row["market"] for row in rows}),
        "win_rate": len(wins) / len(rows),
        "average_win_return": win_return,
        "average_loss_return_absolute": loss_return,
        "breakeven_win_rate_equal_weight": loss_return / (win_return + loss_return),
        **observed,
        "trade_level_bootstrap_95": bootstrap(rows, draws, seed, False),
        "wallet_cluster_bootstrap_95": bootstrap(rows, draws, seed + 1, True),
    }


def load_resolution_audit(path):
    if path is None:
        return set(), set(), None
    report = json.loads(Path(path).read_text())
    phantom_ids = {row["id"] for row in report.get("rows", [])
                   if row.get("verdict") == "phantom"}
    unknown_ids = {row["id"] for row in report.get("rows", [])
                   if row.get("verdict") == "unknown"}
    return phantom_ids, unknown_ids, {
        "audit_version": report.get("audit_version"),
        "factual_phantom_count_excluded": len(phantom_ids),
        "unknown_count_excluded_from_fact_clean_estimators": len(unknown_ids),
    }


def analyze(db_path, draws=10_000, seed=20260807, resolution_audit=None):
    phantom_ids, unknown_ids, audit_summary = load_resolution_audit(resolution_audit)
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        db_rows = conn.execute(
            "SELECT pt.id,lower(pt.wallet_address) wallet,pt.market_slug market,"
            "pt.realized_pnl_usd pnl,pt.cost_basis_usd cost,"
            "COALESCE(wp.circuit_breaker_muted,1) muted "
            "FROM paper_trade pt LEFT JOIN wallet_profile wp "
            "ON lower(wp.wallet_address)=lower(pt.wallet_address) "
            "WHERE pt.strategy='bot_filtered' AND pt.status='closed' "
            "AND pt.is_demo_data=0 AND COALESCE(pt.is_phantom,0)=0 "
            "AND pt.realized_pnl_usd IS NOT NULL AND pt.cost_basis_usd>0"
        ).fetchall()
    finally:
        conn.close()
    rows = [dict(row) for row in db_rows
            if row["id"] not in phantom_ids and row["id"] not in unknown_ids]
    eligible = [row for row in rows if not row["muted"]]
    report = {
        "draws": draws, "seed": seed,
        "all_clean_rows": summarize(rows, draws, seed),
        "currently_eligible_wallet_rows": summarize(eligible, draws, seed + 10),
    }
    if audit_summary is not None:
        report["resolution_audit_filter"] = audit_summary
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.SQLITE_PATH)
    parser.add_argument("--draws", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260807)
    parser.add_argument(
        "--resolution-audit", type=Path,
        help="Exclude factual phantom rows; keep UNKNOWN outside fact-clean estimators.",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = analyze(args.db, args.draws, args.seed, args.resolution_audit)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
