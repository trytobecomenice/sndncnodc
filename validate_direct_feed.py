#!/usr/bin/env python3
"""Parallel validation (2026-07-21, methodology fixed 2026-07-22) — compares
the new direct Polymarket Data API fetcher (polymarket_data_api.py) against
bullpen's own `tracker feed` for the SAME tracked wallets, side by side,
WITHOUT touching bot.py's live state (seen_trade_ids, positions,
bot_event_log) at all. Read-only, side-effect-free — safe to run any number
of times while the real bot keeps running normally.

METHODOLOGY FIX (2026-07-22): the first version of this script compared
bullpen's tracker feed (limit=150, shared across ALL wallets) against the
direct API (limit=50 PER wallet, so up to 1000 total) — an unfair comparison
that made "direct-API-only" trades mostly just reflect the direct API
reaching deeper into history, not a real accuracy difference. Fixed by
comparing a shared, explicit TIME WINDOW instead of mismatched count limits:
the direct API filters server-side via its `start` param (confirmed live to
work correctly); bullpen has no time-window flag at all (checked `bullpen
tracker feed --help` — only a count-based `--limit`), so its results are
filtered client-side to the same window after fetching generously.

Matches trades between the two sources by transaction_hash — confirmed live
(2026-07-21) that bullpen's own `transaction_hash` field and Polymarket's
`transactionHash` are the exact same value, so this is an exact join key,
not a fuzzy heuristic.

WHY THIS EXISTS: before ever pointing bot.py's live poll loop at the new
fetcher, the goal is real evidence — over multiple runs, ideally over days —
that the two sources agree closely enough to trust a cutover. A single run
proves the plumbing works; it does NOT by itself prove the feeds are
equivalent over time (a source that's usually right but occasionally misses
a trade wouldn't necessarily show up as a mismatch in one snapshot).

Run: python3 validate_direct_feed.py [window_hours]
"""

import sys
import time
from datetime import datetime, timezone

from bullpen_client import run_bullpen_json
from db import get_tracked_traders
from polymarket_data_api import fetch_all_wallets_concurrent

BULLPEN_FEED_LIMIT = 150  # generous — bullpen has no time-window flag, so this must be large
# enough to plausibly reach back through the whole comparison window; filtered client-side after.
DIRECT_PER_WALLET_LIMIT = 100
DEFAULT_WINDOW_HOURS = 24


def parse_bullpen_timestamp(ts_str):
    """bullpen's timestamp field is "YYYY-MM-DD HH:MM:SS UTC" — parse to epoch
    seconds so it's directly comparable against the direct API's epoch ints
    and against the shared window cutoff."""
    if not ts_str:
        return None
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def fetch_bullpen_side(tracked_lower, window_start_epoch):
    start = time.time()
    feed = run_bullpen_json(["tracker", "feed", "--limit", str(BULLPEN_FEED_LIMIT)], retries=3, retry_delay=0.5)
    elapsed = time.time() - start
    trades = feed.get("trades", [])
    trades = [t for t in trades if (t.get("user_address") or "").lower() in tracked_lower]
    # Client-side window filter — bullpen has no server-side time param.
    in_window = []
    oldest_seen = None
    for t in trades:
        ts = parse_bullpen_timestamp(t.get("timestamp"))
        if ts is None:
            continue
        oldest_seen = ts if oldest_seen is None else min(oldest_seen, ts)
        if ts >= window_start_epoch:
            in_window.append(t)
    # Real, honest caveat: if bullpen's own limit=150 didn't reach back far
    # enough to cover the whole window, this comparison is STILL unfair in
    # bullpen's disfavor — surfaced explicitly rather than silently assumed away.
    limit_reached_window = oldest_seen is not None and oldest_seen <= window_start_epoch
    return in_window, elapsed, limit_reached_window


def fetch_direct_side(addresses, window_start_epoch):
    start = time.time()
    result = fetch_all_wallets_concurrent(addresses, limit=DIRECT_PER_WALLET_LIMIT, start=window_start_epoch)
    elapsed = time.time() - start
    return result["trades"], result["errors"], elapsed


def main():
    window_hours = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_WINDOW_HOURS
    window_start_epoch = time.time() - window_hours * 3600

    traders = get_tracked_traders()
    addresses = list(traders.keys())
    tracked_lower = {a.lower() for a in addresses}
    print(f"Validating against {len(addresses)} tracked wallet(s), {window_hours:.1f}h window\n")

    print("Fetching via bullpen tracker feed...")
    bullpen_trades, bullpen_elapsed, limit_reached_window = fetch_bullpen_side(tracked_lower, window_start_epoch)
    print(f"  {len(bullpen_trades)} trade(s) within the window, in {bullpen_elapsed:.2f}s")
    if not limit_reached_window:
        print(f"  *** WARNING: bullpen's limit={BULLPEN_FEED_LIMIT} did NOT reach back to the full "
              f"{window_hours:.1f}h window — this comparison is still unfair to bullpen. "
              f"Re-run with a shorter window or raise BULLPEN_FEED_LIMIT. ***")
    print()

    print("Fetching via direct Polymarket Data API (concurrent, server-side time filter)...")
    direct_trades, direct_errors, direct_elapsed = fetch_direct_side(addresses, window_start_epoch)
    print(f"  {len(direct_trades)} trade(s) within the window, in {direct_elapsed:.2f}s, {len(direct_errors)} wallet error(s)\n")
    for e in direct_errors:
        print(f"    ERROR fetching {e['wallet_address']}: {e['error']}")

    bullpen_by_tx = {t.get("transaction_hash"): t for t in bullpen_trades if t.get("transaction_hash")}
    direct_by_tx = {t.get("transaction_hash"): t for t in direct_trades if t.get("transaction_hash")}

    both = set(bullpen_by_tx) & set(direct_by_tx)
    bullpen_only = set(bullpen_by_tx) - set(direct_by_tx)
    direct_only = set(direct_by_tx) - set(bullpen_by_tx)

    print("\n=== Comparison (same time window, both sources) ===")
    print(f"In both sources:        {len(both)}")
    print(f"Bullpen ONLY:           {len(bullpen_only)}")
    print(f"Direct API ONLY:        {len(direct_only)}")

    # price is compared for informational purposes only — size_usd is EXPECTED
    # to differ on taker trades now that we know why (Polymarket's real taker
    # fee; bullpen reports the pre-fee nominal value, direct API reports the
    # real settled usdcSize) — see docs/copy-trading/RISK_MANAGEMENT.md Rule 10.
    price_mismatches = []
    structural_mismatches = []
    for tx in both:
        b, d = bullpen_by_tx[tx], direct_by_tx[tx]
        for field in ("market_slug", "outcome", "side"):
            if b.get(field) != d.get(field):
                structural_mismatches.append((tx, field, b.get(field), d.get(field)))
        bp, dp = b.get("price"), d.get("price")
        if bp is not None and dp is not None and abs(float(bp) - float(dp)) > 0.01:
            price_mismatches.append((tx, bp, dp))

    if structural_mismatches:
        print(f"\n*** {len(structural_mismatches)} STRUCTURAL MISMATCH(ES) (market_slug/outcome/side) ***")
        for tx, field, bv, dv in structural_mismatches:
            print(f"  {tx[:16]}... field={field}: bullpen={bv!r} direct={dv!r}")
    else:
        print("\nNo structural mismatches (market_slug/outcome/side) on trades present in both sources.")

    if price_mismatches:
        print(f"\n*** {len(price_mismatches)} PRICE MISMATCH(ES) (expected to be rare/zero — unlike size_usd) ***")
        for tx, bp, dp in price_mismatches:
            print(f"  {tx[:16]}...: bullpen price={bp} direct price={dp}")
    else:
        print("No price mismatches — consistent with Rule 10's finding that price agrees, only size_usd differs (fee).")

    if bullpen_only:
        print(f"\nSample of bullpen-only trades (present in bullpen, missing from direct API):")
        for tx in list(bullpen_only)[:5]:
            t = bullpen_by_tx[tx]
            print(f"  {t.get('timestamp')} {t.get('user_address')} {t.get('market_slug')} {t.get('side')}")

    if direct_only:
        print(f"\nSample of direct-API-only trades (present in direct API, missing from bullpen):")
        for tx in list(direct_only)[:5]:
            t = direct_by_tx[tx]
            print(f"  {t.get('timestamp')} {t.get('user_address')} {t.get('market_slug')} {t.get('side')}")

    print(f"\n=== Timing ===")
    print(f"Bullpen (1 call, all wallets):        {bullpen_elapsed:.2f}s")
    print(f"Direct API ({len(addresses)} concurrent calls, cold connections): {direct_elapsed:.2f}s")
    print(f"(Direct API drops to ~0.4-0.5s per cycle once connections are warm — see Rule 14; "
          f"this one-off script always pays the cold-connection cost since it doesn't reuse an executor.)")


if __name__ == "__main__":
    main()
