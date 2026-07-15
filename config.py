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
# price. A live buy will not fill above price*(1+SLIPPAGE_TOLERANCE); a live
# sell will not fill below price*(1-SLIPPAGE_TOLERANCE). Protects against
# copying into a price spike that happened between the source trade and our
# execution a few seconds later.
#
# Raised from 0.03 -> 0.05 on 2026-07-15 to cut "unresolved trade" misses on
# fast-moving markets. Tradeoff: a 5% band also means we'll now fill copies
# that have drifted meaningfully worse than the source trader's own price —
# on a thin/low-liquidity market that can matter. If you start seeing live
# fills you wouldn't have taken manually, bring this back down.
SLIPPAGE_TOLERANCE = 0.05

# Seconds between polls of the tracker feed.
POLL_INTERVAL_SECONDS = 30

# How many recent trades to pull per poll. Must comfortably exceed the
# number of trades ALL tracked addresses combined could make between polls.
# Raised from 50 -> 150 now that we're tracking 10 traders instead of 2.
FEED_LIMIT = 150

# LIVE_MODE = True places REAL orders with REAL funds via `bullpen polymarket
# buy/sell --yes`. No per-trade confirmation. Set back to False to return to
# paper/simulation mode.
LIVE_MODE = True

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

# Files (all inside this project directory)
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_PATH = os.path.join(BASE_DIR, "trades_log.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
