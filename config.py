"""
Configuration for the Polymarket copytrading bot.
"""

# Traders to mirror (address -> nickname, used only for readable logs).
# Selected 2026-07-14: top 10 by 7-day realized PnL, filtered to
# volume_7d >= $1,000, copyability_tier != "not_recommended",
# risk_tier != "degen", bots/farmers hidden. All 10 are still only
# "low_copyability" tier — see the trader list Claude reported when this
# was generated for the full rationale/caveats before trusting them with
# real funds.
TRACKED_TRADERS = {
    "0xf0318C32136c2dB7feC88B84869aEE6A1106C80c": "strict-1",
    "0x83720820a8aa6c3f20AD71850E7A1A17d16c5223": "strict-2",
    "0xf5FabdCdc6EB6D9765a228824F16ccA9C91f62Df": "strict-3",
    "0xE16D3F2A5807999b358aFfD9445C3a09E45E5e30": "strict-4",
    "0x5B4ec9c06B284eE52C41A761974d836992880232": "strict-5",
    "0xE9A6ED2e4d4ee8CE47CD47cac834746Dc4Cf627b": "strict-6",
    "0x65018f9FC473F6E920B8929a375d39C26a461220": "strict-7",
    "0x5966Db1fE50763C9e3C014d756369BAd07E1F804": "strict-8",
    "0x6211f97A76Ed5C4b1D658f637041AC5F293Db89E": "strict-9",
    "0x56acAb44cfCa2E88bb9B3406890Aea7bFA0CD77e": "strict-10",

    # Added 2026-07-15: non-crypto diversification traders, sourced from
    # `bullpen polymarket discover traders` (overall PnL leaderboard, since
    # its --category filter does not actually filter — verified empirically)
    # then screened with `wallet-stats --section categories` for a
    # Politics/Geopolitics/World-dominant mix with near-zero crypto exposure,
    # and cross-checked against the public Polymarket profit leaderboard via
    # `bullpen polymarket profit --address <addr>`.
    #
    # IMPORTANT CAVEATS (read before risking real funds):
    # - Bullpen's wallet-stats flagged all three as is_likely_bot=true. Your
    #   existing 10 were explicitly filtered to exclude bots/farmers — these
    #   were NOT filtered that way, because doing so eliminated every
    #   candidate with a real non-crypto-dominant track record on today's
    #   leaderboard. Verify you're comfortable with that trade-off.
    # - No Weather- or Science-primary whale-tier trader could be verified.
    #   Weather markets on Polymarket right now (temperature/wildfire) are
    #   too new/thin to have an established profitable specialist, and no
    #   Science category exists at meaningful volume. All 3 below are
    #   Politics/Geopolitics/World traders instead — genuinely non-crypto,
    #   but not the Weather/Science split originally asked for.
    "0x71edffD0D70A1da823Ff07a3C6FC81457294D338": "geo-pako",      # "pako" — Politics 65%/Geopolitics 11%, $381,544 all-time
                                                                      # leaderboard profit (confirmed), low_copyability, risk_tier low
    "0xBAA2BCb5439E985CE4ccF815B4700027D1b92c73": "geo-denizz",    # "denizz" — Politics 32%/Geopolitics 56%/World 11%, $2,441,947
                                                                      # all-time leaderboard profit (confirmed), low_copyability, risk_tier low
    "0x7f9e2d1DF78614564a70BeCc7fA14AA9a6623A0e": "geo-anon-3",    # unnamed — Politics 43%/Geopolitics 41%/World 7%, ~$1.01M summed
                                                                      # category PnL (leaderboard lookup itself errored — could not
                                                                      # double-confirm via that specific endpoint), moderately_copyable,
                                                                      # risk_tier low

    # Added 2026-07-15 (funnel expansion): 7 more, no bot/category filter
    # per instruction ("just high profitability and high volume in month").
    # Kept the ORIGINAL quality bar otherwise: risk_tier != degen,
    # copyability_tier != not_recommended, profit_factor > 1 (all via
    # `wallet-stats --section all`). Sourced from two pools: the top-50
    # all-time PnL leaderboard (`discover traders`, exhausted at 50 rows —
    # that's a hard cap, not a limit I chose), and top holders of the
    # highest-volume single market I could get a fast response from
    # (fed-rate-hike-in-2026 — event-top-holders timed out repeatedly on
    # bigger events; `holders` on a single market worked and returns
    # display names, which I resolved to addresses via `polymarket search
    # --type user` on an exact name match).
    #
    # CAVEATS:
    # - "geo-anon-4" (0xA7b7505A...) shows profit_factor=99.0 from
    #   wallet-stats. That's almost certainly a capped/sentinel value in
    #   Bullpen's calc, not a literal 99x ratio — I saw the same exact 99.0
    #   on another wallet during screening. Real signal underneath (320
    #   trades, $1.18M/mo volume, is_whale=true), but treat the ratio itself
    #   as "very good, uncertain exact magnitude," not gospel.
    # - "fed-warren-buffett" (0x4478d7bd...) is a Polymarket display name
    #   coincidence, not the actual Warren Buffett or anyone impersonating
    #   him in a way that implies real-world identity — just their chosen
    #   pseudonym on the platform.
    # - "fed-559b" (0x559B8506...) has profit_factor=1.04 — barely above
    #   breakeven, the weakest of the 7 on that metric. Included because its
    #   volume_30d ($322K) and win_rate still clear the bar, but it's the
    #   first one I'd cut if you want to trim back.
    "0x1465B79bfF7992Bc703e1AaFB3683b1089647072": "expand-1",  # profit_factor 1.83, win 69.6%, vol_30d $1.42M, Crypto-primary, whale
    "0x060f34e5AA82Cc11C2a54c9EDF3E6A0632925A9d": "expand-2",  # profit_factor 1.37, win 67.5%, vol_30d $1.86M, Sports-primary, copyability_tier="bot"
    "0x620d7e06CE27d16532C061EbA9b46C7e1833C67f": "expand-3",  # profit_factor 1.07, win 50.0%, vol_30d $1.52M, Sports-primary, whale
    "0xA7b7505AbE2FDCC497C00074534f7fbd7e07962E": "geo-anon-4",  # profit_factor 99.0 (capped, see caveat above), win 50.0%, vol_30d $1.18M, Sports-primary, whale
    "0x4478d7bd8a295691ac84f60a5ec2b47e122102a4": "fed-warren-buffett",  # profit_factor 1.35, win 60.2%, vol_30d $1.10M, Politics-primary, whale, 3753 trades
    "0xcaab19659b995951a44cc992447cb2ad5be324dd": "fed-qmg-core",  # profit_factor 2.10, win 73.2%, vol_30d $903K, Mixed category, whale, 2323 trades
    "0x559B850620CD9Ee1136D48484c1F374fC5d44959": "fed-559b",  # profit_factor 1.04 (marginal, see caveat above), win 35.6%, vol_30d $322K, Sports-primary
}

# How much (USD) WE spend on each copied BUY, regardless of the source
# trader's own trade size. Selling is proportional: if the trader sells N%
# of their (observed) position, we sell N% of ours.
FIXED_TRADE_USD = 5.0

# Max acceptable slippage on LIVE orders, as a fraction of the source trade's
# price. Governs TWO complementary mechanisms that share this one number:
#   1. Order-level fill limit: a live buy will not fill above
#      price*(1+SLIPPAGE_TOLERANCE); a live sell will not fill below
#      price*(1-SLIPPAGE_TOLERANCE) (--max-price/--min-price passed to the
#      order itself).
#   2. Pre-trade "Disciplined Taker" price ceiling, added 2026-07-19
#      (check_slippage_ceiling in bot.py, BUY-only): if a fresh preview
#      shows the market has ALREADY moved past this band since the source
#      trade, the copy is aborted before an order is ever submitted, rather
#      than relying solely on (1) to reject/rest an order into an
#      already-moved market.
# Both exist to protect against copying into a price spike that happened
# between the source trade and our execution a few seconds later.
#
# Raised from 0.03 -> 0.05 on 2026-07-15 to cut "unresolved trade" misses on
# fast-moving markets. Tradeoff: a 5% band also means we'll now fill copies
# that have drifted meaningfully worse than the source trader's own price —
# on a thin/low-liquidity market that can matter. If you start seeing live
# fills you wouldn't have taken manually, bring this back down.
SLIPPAGE_TOLERANCE = 0.05

# Risk 1 (spread/liquidity) guard, added 2026-07-15. Before every LIVE
# buy/sell, bot.py calls `bullpen polymarket preview` for a fresh read of
# the current book and computes RELATIVE spread (preview's spread field is
# an absolute price-tick value, e.g. 0.01 -- dividing by price is what makes
# it comparable across outcomes trading near $0.05 vs near $0.95). If
# relative spread exceeds this fraction, or preview itself reports a
# liquidity warning, the copy is skipped and logged as skip_wide_spread
# instead of firing into a thin book. Chosen to match the wider
# SLIPPAGE_TOLERANCE band above rather than a tighter one.
SPREAD_TOLERANCE = 0.05

# Risk 3 (duplicate exposure) guard, added 2026-07-16. Positions are keyed
# per-trader (trader|market_slug|outcome, see position_key in bot.py), so
# without this guard two DIFFERENT tracked traders buying the same
# market_slug+outcome would each open their own separate position -> we'd
# accidentally hold 2x (or Nx) exposure to the same outcome.
#
# "Conviction scaling" policy:
# - Cross-trader: if any OTHER tracked trader already holds an active
#   (shares > 0) position in this exact market_slug+outcome, block the copy
#   entirely -- exposure to a given outcome comes from only one trader at a
#   time.
# - Same-trader: the ORIGINAL trader who opened a position may still add to
#   it (this is the existing average-up behavior), but only up to
#   MAX_BUYS_PER_TRADER_OUTCOME total buys on that exact outcome. A further
#   buy signal beyond the cap is blocked rather than silently averaged in
#   forever.
MAX_BUYS_PER_TRADER_OUTCOME = 2

# Circuit breaker (kill switch), added 2026-07-16. Tracks realized pnl_usd on
# OUR OWN closed copy-trades (paper_sell/live_sell events) per tracked
# trader -- this is the only actual performance data the bot has, since we
# don't see a trader's entry price history from before we started tracking
# them. The moment either rule trips, that trader is muted: all further BUY
# signals from them are blocked (existing positions can still be sold to
# exit), regardless of any other setting. Mutes persist in state.json across
# restarts and must be cleared manually (delete the trader's entry from
# state.json's "muted_traders").
#
# - MUTE_CONSECUTIVE_LOSS_STREAK: mute after this many losing closes in a row.
# - MUTE_WIN_RATE_THRESHOLD / MIN_TRADES_FOR_WIN_RATE_MUTE: mute if win rate
#   over the last 10 closed trades falls below this threshold, but only once
#   at least MIN_TRADES_FOR_WIN_RATE_MUTE closes exist -- with a smaller
#   sample this rule would over-react to normal variance (e.g. 1 loss out of
#   2 trades reading as a 0% win rate).
MUTE_CONSECUTIVE_LOSS_STREAK = 3
MUTE_WIN_RATE_THRESHOLD = 0.30
MIN_TRADES_FOR_WIN_RATE_MUTE = 10

# Trailing Take-Profit (TTP), added 2026-07-16. Runs once per poll cycle
# against every active position (see check_trailing_take_profit in bot.py),
# independent of what the source trader is doing -- this is what lets the
# bot exit before a trader who's slower to sell gives back gains.
#
# - Tracks each position's highest profit percentage seen since entry
#   (peak_profit_pct, persisted on the position in state.json so it survives
#   restarts).
# - The trail only "arms" once peak_profit_pct first reaches
#   TRAILING_TP_ACTIVATION_PCT (+50%) -- positions below that are left alone
#   entirely, no matter how far they pull back from a smaller peak.
# - Once armed, a pullback of TRAILING_TP_DRAWDOWN_PCT from the peak, in
#   PERCENTAGE POINTS (not relative -- e.g. peak +70% -> current +60% is a
#   10-point drawdown, matching the spec example), triggers an immediate
#   full-position market sell regardless of source trader activity.
TRAILING_TP_ACTIVATION_PCT = 0.50
TRAILING_TP_DRAWDOWN_PCT = 0.10

# How often the TTP sweep actually runs. Added 2026-07-16 after measuring a
# full sweep of 79 open positions at >120s (one `bullpen polymarket price`
# subprocess per position): running it every 30s poll cycle would delay
# trade copies by minutes and hammer the API. The sweep is time-gated inside
# the poll loop; worst case a pullback is noticed this many seconds late.
TRAILING_TP_CHECK_INTERVAL_SECONDS = 300

# How often the resolved-market sweep runs (see run_closeout_sweep in
# bot.py). Each sweep checks every open position's market for resolution
# (`bullpen polymarket market`), books final 0/1 outcome prices into the
# ledger for resolved ones, and — in LIVE mode only — runs
# `bullpen polymarket closeout` to actually redeem winners on-chain.
# Without this, positions in resolved markets sit in state forever (source
# traders redeem rather than sell, and redemptions never appear as SELL
# trades in the feed).
CLOSEOUT_INTERVAL_SECONDS = 3600

# Seconds between polls of the tracker feed.
POLL_INTERVAL_SECONDS = 30

# How many recent trades to pull per poll. Must comfortably exceed the
# number of trades ALL tracked addresses combined could make between polls.
# Raised from 50 -> 150 now that we're tracking 10 traders instead of 2.
FEED_LIMIT = 150

# LIVE_MODE = True places REAL orders with REAL funds via `bullpen polymarket
# buy/sell --yes`. No per-trade confirmation. Set back to False to return to
# paper/simulation mode.
# Set to False 2026-07-16 for the paper-trading validation phase of the new
# TTP + hardening features; flip back only after paper results check out.
LIVE_MODE = False

# Number of attempts for READ-ONLY bullpen calls (currently just `tracker
# feed`) before giving up on a poll cycle. Deliberately NOT applied to
# buy/sell: retrying a trade command that may have already executed risks a
# double fill, so trade execution stays single-shot and just logs a
# failed_trade on error (see require_filled/failed_trade handling in bot.py).
FEED_FETCH_RETRIES = 3
FEED_FETCH_RETRY_DELAY_SECONDS = 0.5

# Optional private Polygon RPC endpoint (e.g. an Alchemy/QuickNode app URL)
# to cut latency and get `eth_getLogs` support the public fallback RPC
# lacks. Leave as None to use bullpen's built-in public Polygon RPC.
# Get this from https://dashboard.alchemy.com -> your app -> HTTPS URL.
# Bullpen reads this via the BULLPEN_POLYGON_RPC_URL env var (verified via
# `bullpen --help` and the CLI's own error-message text) — bot.py sets that
# env var from this value before making any bullpen subprocess call, which
# covers ALL bullpen commands (feed polling AND live buy/sell), not just a
# subset. There is no in-repo web3/RPC client of our own to point at it;
# bullpen owns 100% of the on-chain interaction for this bot.
PRIVATE_POLYGON_RPC_URL = None  # e.g. "https://polygon-mainnet.g.alchemy.com/v2/<your-api-key>"

# --- Portfolio-level risk controls (risk_manager.py) -------------------------
# These gate NEW BUYs only — sells, trailing-TP exits, and closeouts are
# never blocked (a risk layer that traps you in positions adds risk instead
# of removing it). Applied in BOTH paper and live mode so paper runs stay
# representative. Chosen 2026-07-18 ("Moderate" bundle + owner's bankroll
# numbers, see below); any limit can be disabled by setting it to None.

# Hard ceiling on total USD deployed (sum of open positions' cost basis).
# A BUY that would push total exposure ABOVE this is skipped.
MAX_TOTAL_EXPOSURE_USD = 250.0

# Cap on USD deployed within a single Polymarket EVENT (markets are grouped
# into events; e.g. every "What price will Bitcoin hit in July?" strike is
# one event). Resolved via `bullpen polymarket market <slug>` ->
# events[0].slug, cached in bot_market_event. NOTE this only catches
# same-event concentration — economically-correlated bets split across
# different events (e.g. "BTC reach 72.5k" vs "BTC dip to 50k") are NOT
# caught; cross-event correlation stays unmodeled for now.
MAX_EVENT_EXPOSURE_USD = 30.0

# Kill switch: portfolio equity is defined as PAPER_BANKROLL_USD + realized
# PnL + unrealized PnL (unrealized comes from the trailing-TP sweep's price
# fetches, so equity refreshes every TRAILING_TP_CHECK_INTERVAL_SECONDS ~5
# min, not per-trade). TWO independent triggers latch the same halt:
#
#   1. EQUITY_FLOOR_USD — the catastrophic stop: halt if equity ever drops
#      below this absolute level. Owner's framing: "trading with $125, stop
#      if I'm down to $100."
#   2. MAX_DRAWDOWN_FROM_PEAK_USD — the working stop: halt if equity falls
#      this far below its own high-water mark. This is the trigger that
#      protects profit already banked — with +$107 realized at the time
#      this was configured, the floor alone would allow giving back all of
#      it plus $25 before firing.
#
# Once triggered the halt LATCHES (persisted in bot_risk_state, survives
# restarts) until manually cleared with `python3 reset_kill_switch.py` —
# a breached limit means a human reviews before risk resumes.
PAPER_BANKROLL_USD = 125.0
EQUITY_FLOOR_USD = 100.0
MAX_DRAWDOWN_FROM_PEAK_USD = 50.0

# Files (all inside this project directory)
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_PATH = os.path.join(BASE_DIR, "trades_log.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

# Shared SQLite DB (packages/db owns the schema/migrations; db.py only ever
# does SELECT/INSERT/UPDATE/DELETE against it, never CREATE/ALTER TABLE).
# Absolute path, same resolution rule as packages/db/src/env.ts, so both
# sides always agree on one file regardless of each process's cwd.
SQLITE_PATH = os.path.join(BASE_DIR, "data", "app.db")

# "static": get_tracked_traders() returns TRACKED_TRADERS above, unchanged
#   behavior — the default until the new TS leaderboard-scan/scoring layer's
#   output (wallet_profile.status) has been running long enough to trust.
# "db": get_tracked_traders() instead queries wallet_profile for
#   status='track' AND circuit_breaker_muted=0, returned in the same
#   {address: nickname} shape, so every call site is unaffected by the switch.
TRACKED_TRADERS_SOURCE = "static"

# Phase C hardening (see TRACKED_TRADERS_SOURCE="db"): if the DB query ever
# returns fewer than this many traders, bot.py fails loudly at startup
# rather than silently trading a near-empty tracker list.
MIN_TRACKED_TRADERS = 3
