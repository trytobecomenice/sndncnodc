#!/usr/bin/env python3
"""Reports whether the active (tracked AND not circuit-breaker-muted)
copying pool has fallen below config.TARGET_ACTIVE_TRADER_COUNT, and if
so, proposes the next-best untried candidate(s) from wallet_profile,
each annotated with real evidence for a human to decide with.

Deliberately PROPOSES ONLY -- never edits config.py or wallet_profile
itself, and never silently drops a candidate. Every TRACKED_TRADERS
change this session has gone through explicit human review before being
made; this script exists to surface WHEN a review is worth having and
gather the evidence for it, not to replace the review itself.

Adding a proposed candidate still means editing config.TRACKED_TRADERS by
hand and restarting bot.py, same as every other wallet-membership change
so far (see docs/copy-trading/RISK_MANAGEMENT.md Rule 37).

Reads bot.py's real, live tracked-wallet list from
bot_risk_state["tracked_traders"] (published at every bot.py startup,
2026-07-27 -- the same source the Next.js dashboard uses) rather than a
second manually-synced copy: this script drifting from what's actually
tracked would defeat its own purpose.

Evidence-gathering, not auto-filtering (2026-07-27, second revision):
this script's first version hard-rejected any `is_likely_bot=true`
candidate. That's the wrong axis -- an algorithmic trader with a genuine
directional edge is fine to copy; the real danger is a wallet whose
profit comes from liquidity rewards, market-making rebates, or
micro-arbitrage that copy-lag and taker fees make structurally
unreplicable. `is_likely_bot` conflates "automated" with "unreplicable
edge" and can be wrong in both directions. So this version pulls real
`bullpen polymarket activity` for every candidate and reports concrete,
checkable signals (price extremity, side bias, repeated same-market/
same-price quotes) alongside the raw bot flag -- verified live on the
first 4 candidates: both gloriafoster and Asperatus showed the exact
liquidity-farming signature (repeated SELLs at the same near-zero price
in the same market, sizes varying wildly minutes apart) that motivated
this rewrite. Nothing here auto-rejects; every candidate up to the gap
is reported with its evidence attached, worst signals first flagged
plainly, final call left to the human reviewing the output.

Usage:
    python3 propose_pool_refill.py
"""

from collections import Counter

from db import (
    get_risk_value, get_pool_refill_candidates, get_ever_tracked_wallets, get_muted_wallets,
)
from bullpen_client import run_bullpen_json
import config


def check_is_likely_bot(wallet_address):
    """Returns True/False from a live `bullpen polymarket wallet-stats`
    call, or None if the call fails or the field isn't present. One input
    signal among several now, not a gate -- see the module docstring for
    why this alone isn't disqualifying.
    """
    try:
        # run_bullpen_json() already appends --output json itself --
        # passing it again here causes "can only be provided once" (a
        # real bug caught on this script's first live run).
        response = run_bullpen_json(["polymarket", "wallet-stats", wallet_address], retries=2)
    except Exception:
        return None
    behavior = response.get("behavior_stats") or {}
    if behavior.get("status") != "ok":
        return None
    data = behavior.get("data") or {}
    is_bot = data.get("is_likely_bot")
    return is_bot if isinstance(is_bot, bool) else None


def fetch_recent_trades(wallet_address, limit=None):
    """Recent real TRADE activity for `wallet_address` via `bullpen
    polymarket activity`, or None if the call fails. A quick-look sample
    (config.POOL_REFILL_ACTIVITY_SAMPLE_SIZE), not a full history --
    enough to see a repeated-quote pattern if one exists.
    """
    limit = limit or config.POOL_REFILL_ACTIVITY_SAMPLE_SIZE
    try:
        response = run_bullpen_json(
            ["polymarket", "activity", "--address", wallet_address, "--limit", str(limit)],
            retries=2,
        )
    except Exception:
        return None
    activities = response.get("activities") or []
    return [a for a in activities if a.get("type") == "TRADE"]


def summarize_liquidity_farming_signal(trades):
    """Plain, human-readable diagnostic stats from a real trade sample --
    deliberately NOT a hidden score or an accept/reject verdict: the point
    is to surface EVIDENCE a human can actually check, not replace one
    opaque threshold (is_likely_bot) with another. Returns None if there's
    no sample to summarize (the activity call failed or the wallet has no
    trade history at all).

    - extreme_price_pct: % of trades at price < 0.05 or > 0.95 -- betting
      near-certain/near-impossible outcomes at scale is consistent with
      farming Polymarket's liquidity-rewards program (tight quotes near
      the edges of the range), not genuine directional conviction.
    - sell_pct/buy_pct: side balance in this sample.
    - top_repeated_quote / top_repeated_quote_count: the single most-
      repeated (market, price) pair in the sample -- hitting the exact
      same quote many times in one market is a liquidity-provision
      signature, not a one-off bet. This is the strongest single signal:
      confirmed live, both gloriafoster and Asperatus had their single
      most common quote repeated in double digits within the sample.
    - unique_markets: how spread out the sample is across markets.
    """
    if not trades:
        return None
    n = len(trades)
    priced = [t for t in trades if isinstance(t.get("price"), (int, float))]
    extreme = sum(1 for t in priced if t["price"] < 0.05 or t["price"] > 0.95)
    sell_count = sum(1 for t in trades if t.get("side") == "SELL")
    quote_counts = Counter((t.get("slug"), round(t["price"], 3)) for t in priced)
    top_quote, top_quote_count = quote_counts.most_common(1)[0] if quote_counts else (None, 0)
    return {
        "sample_size": n,
        "extreme_price_pct": round(100 * extreme / len(priced), 1) if priced else None,
        "sell_pct": round(100 * sell_count / n, 1),
        "buy_pct": round(100 * (n - sell_count) / n, 1),
        "top_repeated_quote": top_quote,
        "top_repeated_quote_count": top_quote_count,
        "unique_markets": len({t.get("slug") for t in trades}),
    }


def main():
    tracked_traders = get_risk_value("tracked_traders")
    if not tracked_traders:
        print("bot_risk_state['tracked_traders'] is empty or unset -- start bot.py at least "
              "once first (it publishes this at every startup).")
        return

    tracked_lower = {addr.lower(): nick for addr, nick in tracked_traders.items()}
    muted = get_muted_wallets()
    active = [addr for addr in tracked_lower if addr not in muted]

    print(f"Tracked: {len(tracked_lower)}  |  Muted: {len(muted & set(tracked_lower))}  |  "
          f"Actively copying: {len(active)}  |  Target: {config.TARGET_ACTIVE_TRADER_COUNT}")

    gap = config.TARGET_ACTIVE_TRADER_COUNT - len(active)
    if gap <= 0:
        print(f"\nActive pool ({len(active)}) already meets or exceeds the target "
              f"({config.TARGET_ACTIVE_TRADER_COUNT}) -- no refill needed.")
        return

    print(f"\nActive pool is {gap} below target. Gathering evidence per candidate "
          f"(a wallet-stats + activity call each, this takes a moment)...\n")

    ever_tracked = get_ever_tracked_wallets()
    # Exclude currently-tracked (any casing) AND every wallet ever tracked
    # before, dropped or not -- a previously-kicked wallet must never be
    # re-proposed on the strength of the same track record that got it
    # kicked (see get_pool_refill_candidates()'s own docstring).
    exclude = set(tracked_lower) | ever_tracked

    candidates = get_pool_refill_candidates(
        exclude, config.POOL_REFILL_MIN_COMPOSITE_SCORE, limit=gap,
    )

    if not candidates:
        print(f"No untried candidates found scoring >= {config.POOL_REFILL_MIN_COMPOSITE_SCORE} "
              f"composite_score. Run `pnpm scan:leaderboard && pnpm scan:wallets` "
              f"(packages/copy-trading) to discover more, then re-run this script.")
        return

    for c in candidates:
        win_rate = f"{c['win_rate']*100:.1f}%" if c.get("win_rate") is not None else "—"
        trades_all_time = c.get("trade_count_all_time") or "—"
        category = c.get("category") or "uncategorized"
        print(f"=== {c.get('nickname') or '(no nickname)'}  ({c['wallet_address']}) ===")
        print(f"  composite_score={c['composite_score']:.3f}  win_rate={win_rate}  "
              f"trades_all_time={trades_all_time}  category={category}")

        is_bot = check_is_likely_bot(c["wallet_address"])
        is_bot_str = {True: "true", False: "false", None: "UNVERIFIED (call failed)"}[is_bot]
        print(f"  is_likely_bot (bullpen): {is_bot_str}  [one signal, not a verdict -- see below]")

        recent_trades = fetch_recent_trades(c["wallet_address"])
        signal = summarize_liquidity_farming_signal(recent_trades)
        if signal is None:
            print("  activity sample: UNAVAILABLE (call failed or no trade history) -- "
                  "check manually with `bullpen polymarket activity --address <addr>`")
        else:
            warning = ""
            if signal["extreme_price_pct"] and signal["extreme_price_pct"] >= 50:
                warning = "  <-- majority of sampled trades at extreme prices"
            if signal["top_repeated_quote_count"] >= 5:
                warning += "  <-- same (market, price) hit repeatedly, possible liquidity farming"
            print(f"  activity sample (last {signal['sample_size']} trades): "
                  f"extreme_price%={signal['extreme_price_pct']}  "
                  f"sell%={signal['sell_pct']}  buy%={signal['buy_pct']}  "
                  f"unique_markets={signal['unique_markets']}{warning}")
            if signal["top_repeated_quote"][0]:
                print(f"    most-repeated quote: {signal['top_repeated_quote_count']}x at "
                      f"{signal['top_repeated_quote']}")
        print()

    print(f"{len(candidates)} candidate(s) shown, {gap} slot(s) open. No auto-filtering --  "
          f"review the evidence above per candidate (and pull real trade history for anything "
          f"borderline) before deciding whether to add to config.TRACKED_TRADERS and restart "
          f"bot.py, same as every other wallet decision this session.")


if __name__ == "__main__":
    main()
