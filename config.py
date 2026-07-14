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
SLIPPAGE_TOLERANCE = 0.03

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

# Files (all inside this project directory)
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_PATH = os.path.join(BASE_DIR, "trades_log.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
