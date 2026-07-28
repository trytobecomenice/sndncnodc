#!/usr/bin/env python3
"""Parameter sensitivity check for a handful of "judgment call" constants in
config.py — read-only, touches nothing live. Two DIFFERENT kinds of output,
deliberately never blended into one number:

  PART 1a — REAL BACKTEST (KELLY_FRACTION_MULTIPLIER): replays real closed
  trades' recorded PnL under alternate multiplier values. This is genuine
  historical validation — real trades, real realized PnL.

  PART 1b — STRUCTURAL CHECKS ONLY (KELLY_SHRINKAGE_PSEUDO_COUNT,
  capital-multiplier saturation/cap): NOT historical backtests. These
  constants either aren't recoverable from what was actually journaled per
  trade (KELLY_SHRINKAGE_PSEUDO_COUNT — see fetch_kelly_sensitive_trades()'s
  docstring) or have zero real trade history where they were ever in effect
  with a non-default value (capital_multiplier went live 2026-07-27; 0 of
  406 closed trades to date reflect a non-default multiplier). These sections
  show how sensitive the FORMULA's output is to the constant, using real
  current wallet data as inputs — they do NOT tell you whether real PnL would
  have been better or worse. Labeled loudly in the output so this distinction
  survives being read out of context.

Run: python3 parameter_sensitivity_backtest.py
"""

import json

import bot
import config
import db


def fetch_kelly_sensitive_trades():
    """Pulls every closed trade with a recorded score_breakdown (Rule 22,
    2026-07-24+) from outcome_review, and splits it into three buckets —
    NOT one blended list, since silently mixing them would corrupt the
    Part 1a backtest:

      - base tier (sizing_tier="base" / kelly_fraction is None): sized at
        the flat config.BASE_TRADE_USD regardless of any Kelly constant —
        genuinely unaffected by KELLY_FRACTION_MULTIPLIER, excluded from
        the sensitivity test on its own merits, not a data gap.
      - stale floor-policy trades (kelly_fraction <= 0): verified live
        against real rows in data/app.db — recorded trade_size_usd for
        these is exactly config.MIN_TRADE_USD, NOT 0.0, because they were
        sized under bot.compute_trade_size_usd()'s PRE-2026-07-28 behavior
        (floor at MIN_TRADE_USD on negative Kelly edge; see that function's
        own docstring). The CURRENT formula returns 0.0/skips these
        entirely. Replaying them under an alternate multiplier using
        today's formula would silently misrepresent what actually
        happened — excluded, not guessed at. 298 of 358 real rows fall
        here as of this writing, confirmed by direct query, not assumed.
      - replayable: kelly_fraction > 0, current-formula-consistent. This is
        the ONLY bucket Part 1a's backtest uses.

    Why KELLY_SHRINKAGE_PSEUDO_COUNT (k=25) can't be replayed the same way:
    score_breakdown_json stores the OUTPUT of the shrinkage step
    (shrunk_win_rate) and the resulting kelly_fraction, but never the raw
    (observed_win_rate, trade_count, market_price) inputs that fed it —
    confirmed by reading bot.py's process_trade() directly (score_breakdown
    dict literal has no such keys). shrunk_win_rate = (n*w + k*price)/(n+k)
    has two unknowns (n, w) given one known output — not invertible without
    the raw inputs. See part1b_structural_shrinkage() for how this
    constant is checked instead.
    """
    conn = db._connect()
    try:
        rows = conn.execute(
            "SELECT contributing_score_factors_json, pnl_usd FROM outcome_review "
            "WHERE contributing_score_factors_json IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    base_tier_pnls = []
    stale_floor_policy_pnls = []
    replayable = []
    for row in rows:
        payload = json.loads(row["contributing_score_factors_json"])
        score_breakdown = payload.get("score_breakdown") or {}
        kelly_fraction = score_breakdown.get("kelly_fraction")
        trade_size_usd = score_breakdown.get("trade_size_usd")
        pnl_usd = row["pnl_usd"]

        if score_breakdown.get("sizing_tier") == "base" or kelly_fraction is None:
            base_tier_pnls.append(pnl_usd)
            continue
        if kelly_fraction <= 0:
            stale_floor_policy_pnls.append(pnl_usd)
            continue
        if trade_size_usd is None or not trade_size_usd or pnl_usd is None:
            continue
        replayable.append({
            "kelly_fraction": kelly_fraction,
            "trade_size_usd": trade_size_usd,
            "pnl_usd": pnl_usd,
        })
    return replayable, base_tier_pnls, stale_floor_policy_pnls


def recompute_trade_size(kelly_fraction_full, multiplier, min_trade_usd, max_trade_usd):
    """Mirrors bot.compute_trade_size_usd()'s tail exactly (verified against
    10 real rows before this script was written — 6/6 positive-kelly rows
    reproduced the recorded trade_size_usd to within floating-point
    precision). capital_multiplier is fixed at 1.0 here on purpose: every
    replayable trade predates rule_set v7 (confirmed: all real
    score_breakdown rows so far carry rule_set_version 3 or 4 only), so none
    of them were ever sized with a non-default capital_multiplier applied.
    """
    half_kelly_fraction = kelly_fraction_full * multiplier
    if half_kelly_fraction <= 0:
        return 0.0
    clamped = min(1.0, half_kelly_fraction)
    return min_trade_usd + (max_trade_usd - min_trade_usd) * clamped


def part1a_real_backtest():
    replayable, base_tier_pnls, stale_floor_policy_pnls = fetch_kelly_sensitive_trades()

    print("=" * 78)
    print("PART 1a -- REAL BACKTEST: KELLY_FRACTION_MULTIPLIER")
    print("=" * 78)
    print(f"N={len(replayable)} actual closed trades, real realized PnL.")
    print(f"(excluded: {len(base_tier_pnls)} base-tier trades unaffected by this constant; "
          f"{len(stale_floor_policy_pnls)} trades sized under a since-fixed floor-at-minimum "
          f"policy that predates the current sizing formula -- see this script's module "
          f"docstring for why replaying those would misrepresent what actually happened)")
    print()

    if not replayable:
        print("No replayable trades available -- nothing to backtest yet.")
        return

    actual_total_pnl = sum(t["pnl_usd"] for t in replayable)
    print(f"Actual realized PnL on these {len(replayable)} trades: ${actual_total_pnl:.2f}")
    print()

    current = config.KELLY_FRACTION_MULTIPLIER
    test_values = sorted({round(current * f, 4) for f in (0.8, 0.9, 1.0, 1.1, 1.2)})

    baseline_counterfactual_pnl = None
    results = []
    for mult in test_values:
        total = 0.0
        for t in replayable:
            new_size = recompute_trade_size(t["kelly_fraction"], mult, config.MIN_TRADE_USD, config.MAX_TRADE_USD)
            scale = (new_size / t["trade_size_usd"]) if t["trade_size_usd"] else 0.0
            total += t["pnl_usd"] * scale
        results.append((mult, total))
        if abs(mult - current) < 1e-9:
            baseline_counterfactual_pnl = total

    print(f"{'multiplier':>12} {'counterfactual PnL':>20} {'% vs current':>15}")
    for mult, total in results:
        marker = "  <- current" if abs(mult - current) < 1e-9 else ""
        pct = ((total / baseline_counterfactual_pnl) - 1.0) * 100 if baseline_counterfactual_pnl else float("nan")
        print(f"{mult:>12.3f} {total:>20.2f} {pct:>14.1f}%{marker}")
    print()


def part1b_structural_shrinkage():
    """STRUCTURAL CHECK ONLY -- see module docstring. Uses real, CURRENT
    wallet_profile (win_rate, trade_count_all_time) pairs -- today's stats,
    not point-in-time historical snapshots (those don't exist, see
    fetch_kelly_sensitive_trades()'s docstring) -- across a representative
    market-price grid, showing how compute_shrunk_win_rate()'s OUTPUT moves
    under alternate KELLY_SHRINKAGE_PSEUDO_COUNT values. Does not touch or
    imply anything about real realized PnL.
    """
    print("=" * 78)
    print("PART 1b -- STRUCTURAL CHECK ONLY: KELLY_SHRINKAGE_PSEUDO_COUNT")
    print("NOT a historical PnL backtest -- see module docstring.")
    print("=" * 78)

    conn = db._connect()
    try:
        wallets = conn.execute(
            "SELECT wallet_address, win_rate, trade_count_all_time FROM wallet_profile "
            "WHERE win_rate IS NOT NULL AND trade_count_all_time IS NOT NULL "
            "ORDER BY trade_count_all_time DESC LIMIT 5"
        ).fetchall()
    finally:
        conn.close()

    if not wallets:
        print("No wallets with real win_rate/trade_count_all_time found.")
        return

    current_k = config.KELLY_SHRINKAGE_PSEUDO_COUNT
    test_ks = sorted({round(current_k * f) for f in (0.8, 1.0, 1.2)})
    price_grid = [0.10, 0.30, 0.50, 0.70, 0.90]

    for wallet in wallets:
        win_rate = wallet["win_rate"]
        trade_count = wallet["trade_count_all_time"]
        print(f"\nwallet={wallet['wallet_address'][:10]}... real win_rate={win_rate:.3f} "
              f"real trade_count={trade_count}")
        header = "  price".ljust(10) + "".join(f"k={k:>6}".rjust(12) for k in test_ks)
        print(header)
        for price in price_grid:
            row = f"  {price:.2f}".ljust(10)
            for k in test_ks:
                shrunk = bot.compute_shrunk_win_rate(win_rate, trade_count, price, pseudo_count=k)
                row += f"{shrunk:>12.4f}"
            print(row)
    print()


def compute_capital_multiplier(sharpe_proxy, saturation, cap):
    """Python port of scoreWallets.ts's computeCapitalMultiplier() (TS is the
    source of truth; this exists only so this Python script can test
    saturation/cap sensitivity without shelling out to Node) --
    multiplier = 1 + clamp(sharpe_proxy/saturation, 0, 1) * (cap - 1).
    """
    saturation_fraction = max(0.0, min(1.0, sharpe_proxy / saturation)) if saturation else 0.0
    return 1 + saturation_fraction * (cap - 1)


def part1b_structural_capital_multiplier():
    """STRUCTURAL CHECK ONLY -- see module docstring. capital_multiplier
    went live 2026-07-27; zero real closed trades to date were ever sized
    with a non-default value (confirmed: every replayable trade above
    predates rule_set v7). Uses real CURRENT sharpeProxy values already
    computed and stored in wallet_profile.score_breakdown_json (written by
    scoreWallets.ts's analyzePnlSeries) -- real wallets, real Sharpe
    estimates, but the formula's sensitivity, not a PnL replay.
    """
    print("=" * 78)
    print("PART 1b -- STRUCTURAL CHECK ONLY: capital-multiplier saturation/cap")
    print("NOT a historical PnL backtest -- see module docstring.")
    print("=" * 78)

    conn = db._connect()
    try:
        rows = conn.execute(
            "SELECT wallet_address, score_breakdown_json FROM wallet_profile "
            "WHERE score_breakdown_json IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    sharpe_values = []
    for row in rows:
        breakdown = json.loads(row["score_breakdown_json"])
        sharpe_proxy = breakdown.get("sharpeProxy")
        if sharpe_proxy is not None:
            sharpe_values.append((row["wallet_address"], sharpe_proxy))

    if not sharpe_values:
        print("No wallets with a real sharpeProxy found.")
        return

    sharpe_values.sort(key=lambda pair: pair[1], reverse=True)
    top_wallets = sharpe_values[:5]

    current_saturation = 0.35
    current_cap = 2.0

    print("\nSensitivity to SATURATION (cap held at current 2.0):")
    saturation_values = sorted({round(current_saturation * f, 4) for f in (0.8, 1.0, 1.2)})
    header = "  wallet".ljust(14) + "real sharpeProxy".rjust(18) + "".join(f"sat={s:>6}".rjust(14) for s in saturation_values)
    print(header)
    for address, sharpe_proxy in top_wallets:
        row = f"  {address[:10]}...".ljust(14) + f"{sharpe_proxy:>18.4f}"
        for saturation in saturation_values:
            row += f"{compute_capital_multiplier(sharpe_proxy, saturation, current_cap):>14.4f}"
        print(row)

    print("\nSensitivity to CAP (saturation held at current 0.35):")
    cap_values = sorted({round(current_cap * f, 4) for f in (0.8, 1.0, 1.2)})
    header = "  wallet".ljust(14) + "real sharpeProxy".rjust(18) + "".join(f"cap={c:>6}".rjust(14) for c in cap_values)
    print(header)
    for address, sharpe_proxy in top_wallets:
        row = f"  {address[:10]}...".ljust(14) + f"{sharpe_proxy:>18.4f}"
        for cap in cap_values:
            row += f"{compute_capital_multiplier(sharpe_proxy, current_saturation, cap):>14.4f}"
        print(row)
    print()


def main():
    part1a_real_backtest()
    part1b_structural_shrinkage()
    part1b_structural_capital_multiplier()


if __name__ == "__main__":
    main()
