"""
Configuration for the Polymarket copytrading bot.
"""

import os

# Traders to mirror (address -> nickname, used only for readable logs).
# Selected 2026-07-14: top 10 by 7-day realized PnL, filtered to
# volume_7d >= $1,000, copyability_tier != "not_recommended",
# risk_tier != "degen", bots/farmers hidden. All 10 are still only
# "low_copyability" tier — see the trader list Claude reported when this
# was generated for the full rationale/caveats before trusting them with
# real funds.
TRACKED_TRADERS = {
    # strict-1 KICKED 2026-07-26 (Rule 31 wallet audit): all-time WORST
    # performer of any wallet ever tracked — 11 closed trades, -80.0%
    # EV/dollar-staked, -$59.12 total P&L, 100% of closes were pure
    # hold-to-resolution (the pattern AND negative EV genuinely coincide
    # here, unlike strict-10 below). Zero open positions at removal time,
    # nothing orphaned. NOT actively re-monitored by any new code — its
    # wallet_profile row still exists and will keep getting refreshed by
    # any future `pnpm scan:wallets` run (scores wallets broadly, not just
    # TRACKED_TRADERS members), so its real on-chain performance stays
    # visible without us paying to copy it. Worth a periodic manual check
    # (`wallet_profile.composite_score`/`win_rate`) if reconsidering later.
    # strict-2 KICKED 2026-07-26 (Rule 34 mute review): was already
    # auto-muted 2026-07-25 for a 3-loss streak; reviewed on request rather
    # than left to rot indefinitely muted. 4 closed trades, 3 of them total
    # losses (-100% each, -$6.00/-$6.69/-$6.00), one small win (+$0.59).
    # Consistent, not noise -- dropped rather than reinstated.
    #
    # strict-4 REINSTATED 2026-07-26 (Rule 34 mute review): muted
    # 2026-07-16 -- 10 days BEFORE this session's strict-10 re-add, and
    # never actually re-evaluated since. Only 1 closed trade on record
    # (+$2.44, +48.7%) -- never fairly tested under current tracking,
    # circuit_breaker_muted cleared.
    "0xE16D3F2A5807999b358aFfD9445C3a09E45E5e30": "strict-4",
    "0x5B4ec9c06B284eE52C41A761974d836992880232": "strict-5",
    "0x65018f9FC473F6E920B8929a375d39C26a461220": "strict-7",
    "0x5966Db1fE50763C9e3C014d756369BAd07E1F804": "strict-8",
    "0x6211f97A76Ed5C4b1D658f637041AC5F293Db89E": "strict-9",
    # strict-10 RE-ADDED 2026-07-26 (Rule 31 wallet audit) — the 2026-07-24
    # drop was a real miscalibration: that curation only looked at the
    # narrow recent `paper_trade` sample (6 trades, -$2.65), but the FULL
    # `bot_event_log` history (78 trades) shows this is the single BEST
    # all-time performer of any wallet ever tracked: +21.8% EV/dollar-
    # staked, +$163.34 total P&L, 69.2% win rate. Yes, it's a 100%
    # hold-to-resolution wallet (the exact "yield farmer" pattern the
    # 2026-07-25 filter request wanted to drop on sight) — the audit's own
    # finding was that pattern alone doesn't predict performance; EV does,
    # and this wallet's EV is strongly positive.
    #
    # MUTE BUG FOUND AND CLEARED 2026-07-26 (Rule 34): despite the re-add
    # above, this wallet was STILL silently blocked from trading this whole
    # time -- circuit_breaker_muted had been set 2026-07-16, 10 days before
    # the re-add, and re-adding a wallet to TRACKED_TRADERS never clears an
    # existing wallet_profile mute (two independent mechanisms). Never
    # caught until an explicit mute review. circuit_breaker_muted cleared.
    "0x56acAb44cfCa2E88bb9B3406890Aea7bFA0CD77e": "strict-10",
    # strict-3/6 stay dropped (2026-07-24): composite_score 0.0, both
    # wallet_profile.status="ignore", and strict-6 additionally has a real
    # negative all-time track record (-$25.00, 0% win rate, 3 trades) —
    # unlike strict-10, nothing in the fuller history contradicts that
    # original call. Their open positions (none, in either case) still get
    # resolved normally by run_closeout_sweep even after de-tracking.

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
    # geo-denizz KICKED 2026-07-26 (Rule 34 mute review): auto-muted
    # 2026-07-25 for a 3-loss streak. 6 closed trades, structurally
    # negative -- 3 total losses (-100% each) vs. 3 partial trailing_tp
    # wins (+33% to +56%), losing everything when wrong and only some when
    # right. Consistent asymmetry, not noise.
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
    "0x4478d7bd8a295691ac84f60a5ec2b47e122102a4": "fed-warren-buffett",  # profit_factor 1.35, win 60.2%, vol_30d $1.10M, Politics-primary, whale, 3753 trades
    "0xcaab19659b995951a44cc992447cb2ad5be324dd": "fed-qmg-core",  # profit_factor 2.10, win 73.2%, vol_30d $903K, Mixed category, whale, 2323 trades
    # expand-2/expand-3/geo-anon-4/fed-559b dropped 2026-07-24 (strategy-
    # tiered curation): all composite_score 0.0-0.128, wallet_profile.
    # status="ignore" under the current scorer, and no meaningful real
    # paper-trading track record to counterbalance that.

    # Added 2026-07-24 (Strategy-Tiered Expanded Discovery): a fresh
    # leaderboard scan (scan:leaderboard -> scan:wallets -> Stage-3
    # category-specialist discovery), classified by trading-strategy tier
    # rather than just category, with EVERY existing safety rule active
    # (20/21/23/24/27). Each individually deep-dive-reviewed for mechanical/
    # sniping signatures (entry-price CV, single-market concentration) per
    # the 0xc21ea96b lesson before being trusted here — see
    # docs/copy-trading/RISK_MANAGEMENT.md for the full writeup.
    "0x2b3d1e9bdf941d435dc91a8b974b86f7064c8db7": "quant-generalist-1",  # Cross-Category Quant Generalist: 3 categories (other/sports/pop-culture), top-3 concentration 10.2%, t-stats 2.66-5.79
    "0x510904c9a58f5c5ad799a1b44947077564175e9c": "political-whale-1",  # Political Macro Whale: politics avg_pnl $70.34/trade, 43 trades, t=3.09; also strong "other" bucket (1209 trades, t=5.40)
    "0xe154165732b79548f7533fc45168b102dd7a0b7f": "quant-generalist-2",  # 4 active categories (other/politics/sports/crypto), t-stats 2.13-4.75, real entry-price variance (CV 51-357%) -- just short of the 30% top-3-concentration bar (36.1%) for the formal Generalist tag
    "0xd52b8dcb4b812c4056faf41164a6f6c73f4c5c72": "yield-farmer-1",  # Tail-End Yield Farmer + Generalist: pop-culture win_rate 100%/avg_entry $0.93/CV 22.6% (real variance, not a sniping signature), 4 categories at 28.6% top-3 concentration
    # generalist-weak-1 KICKED 2026-07-26 (Rule 34 mute review): flagged
    # "weakest of this batch" at add time, and it played out that way --
    # auto-muted 2026-07-25 for a 3-loss streak, 5 closed trades net
    # -$18.26, 3 of 5 total losses (-100% each). Confirms the original
    # borderline-watch flag rather than contradicting it.
    "0x011f2d377e56119fb09196dffb0948ae55711122": "sports-scalper-1",  # Sports specialist: 252 trades, win_rate 89.7%, t=6.04, avg_entry $0.10 (real CV 83.8%); ignore its thin 6-trade politics tail (avg_entry $0.005, likely mechanical, below Rule 27's floor)
    # crypto-specialist-1 REINSTATED 2026-07-26 (Rule 34 mute review):
    # auto-muted 2026-07-25 for a 3-loss streak early in its copy history,
    # but its CURRENT rolling 10-trade window (post-streak) is 10/10 wins
    # -- the mute is a one-way latch that never reassessed after the
    # streak ended. circuit_breaker_muted cleared.
    "0xdc767c90ec054dd8250cf27cb4b2f675c9807842": "crypto-specialist-1",  # Crypto specialist: 35 trades, win_rate 68.6%, t=2.47, real CV 53.2%
}

# Half-Kelly position sizing (2026-07-22: confidence-weighted linear ramp;
# REPLACED 2026-07-24 with actual half-Kelly — see bot.compute_trade_size_usd()'s
# docstring for the full formula). How much (USD) WE spend on each copied BUY:
#   1. Pick a (win_rate, trade_count) pair — category-specific if available
#      (db.get_wallet_composite_scores()'s "categories", from
#      scoreWalletCategories.ts), else the wallet's lifetime rolling win rate
#      (wallet_profile.win_rate/trade_count_all_time, from scoreWallets.ts).
#   2. Shrink win_rate toward the market's OWN current price (the crowd's own
#      implied probability) by KELLY_SHRINKAGE_PSEUDO_COUNT worth of "phantom"
#      prior observations — an empirical-Bayes/Beta-Binomial estimator, so a
#      thin sample (e.g. 5 category trades) barely moves off the market price,
#      while a deep sample (e.g. 200+ trades) is trusted close to face value.
#   3. Kelly fraction from the shrunk win rate and the price-implied net odds
#      (b = (1-price)/price for a share paying 1.0/0.0), then halved
#      (KELLY_FRACTION_MULTIPLIER) — full Kelly reliably overbets under the
#      systematic overconfidence prediction-market traders show; half-Kelly is
#      the standard correction (~75% of full-Kelly growth, dramatically less
#      variance — see 2026 Kelly-under-uncertainty research).
#   4. Clamped to [0, 1] and mapped into the SAME bounded MIN/MAX_TRADE_USD
#      range this bot has always used — not a bankroll-fraction bet. True
#      bankroll-fraction Kelly would need to know total capital and would
#      reshape how this interacts with the portfolio risk manager's own
#      exposure ceiling (Rule 6) — a bigger, separate change, not made here.
#   No track record at all (wallet never scored) -> BASE_TRADE_USD, unchanged
#   from the original flat behavior.
# Selling is proportional regardless: if the trader sells N% of their
# (observed) position, we sell N% of ours.
BASE_TRADE_USD = 5.0
MIN_TRADE_USD = 3.0
MAX_TRADE_USD = 10.0

# k in the shrinkage formula p_shrunk = (n*win_rate + k*price) / (n + k) — see
# compute_shrunk_win_rate()'s docstring. Solved, not guessed: at n=200 (the
# "bankable" sample size a 2026 trader-persistence study, Polyburg, cites),
# weight on the observed win rate is n/(n+k) = 200/225 ≈ 89%; at n=5 (this
# codebase's own existing hard minimum sample, DEFAULT_RULES.hardMinTrades on
# the TS side), weight is 5/30 ≈ 17% — i.e. heavily discounted toward the
# market's own price, exactly the "small samples should be heavily discounted"
# requirement, anchored to the same external research already cited elsewhere
# in this codebase's docs, not a new arbitrary number.
KELLY_SHRINKAGE_PSEUDO_COUNT = 25

# Half, not full — full Kelly overbets badly under the ~5-10 percentage point
# systematic overconfidence prediction-market traders commonly show (2026
# Kelly-under-parameter-uncertainty research). Named here (not hardcoded in
# bot.py) so a different fraction can be tried later without touching the
# sizing function itself.
KELLY_FRACTION_MULTIPLIER = 0.5

# Category-specific wallet scoring (added 2026-07-22). Polymarket has no clean
# top-level category enum anywhere in its API — a market's parent EVENT carries
# a `tags` array that's really a large, flat folksonomy (thousands of narrow
# tags like individual athlete/celebrity names mixed in with broad topics).
# This is a small, explicit, EDITABLE list of the broad tag slugs worth
# bucketing separately for scoring purposes — not an attempt to enumerate
# Polymarket's full tag space. Each one verified live against a real event's
# `tags` array before being added here, not guessed:
#   politics    -> confirmed on event "democratic-presidential-nominee-2028"
#   crypto      -> confirmed on event "what-price-will-bitcoin-hit-before-2027"
#   sports      -> confirmed via a live /events sweep (label "Sports")
#   pop-culture -> confirmed on event "what-will-happen-before-gta-vi"
# resolve_market_category() (polymarket_simulator.py) checks an event's tags
# against this list IN ORDER (first match wins) and buckets anything matching
# none of them as "other" — see that function's docstring for the multi-tag
# ambiguity this implies (one event can carry both a broad and a narrow tag,
# or even two different broad tags, e.g. a novelty event spanning both
# Politics and Pop Culture sub-markets).
CATEGORY_TAG_SLUGS = ["politics", "sports", "crypto", "pop-culture"]

# Hard-skip threshold for a wallet's category performance (added 2026-07-23).
# NOT an arbitrary score cutoff — a one-tailed critical z-value for a
# one-sample t-test (Student's t-test, standard since Gosset 1908) on
# whether a category's mean realized PnL is significantly LESS than zero
# (scoreWalletCategories.ts computes the actual t-statistic, pnl_t_stat, per
# category). 1.645 is the conventional one-tailed 95%-confidence critical
# value (citable to any standard statistics reference / z-table) — one-
# tailed because we only care about detecting harm, not flagging an
# unusually GOOD category (that direction is already rewarded via
# compute_trade_size_usd()'s score-based sizing, not something to gate on).
# bot.should_skip_category() compares pnl_t_stat against
# -CATEGORY_SKIP_Z_CRITICAL: a category whose t-statistic is more negative
# than this is skipped entirely rather than floor-sized, since a
# statistically weak result (small sample, high variance) should NOT
# trigger a hard skip even if its raw score happens to look low.
CATEGORY_SKIP_Z_CRITICAL = 1.645

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

# Entry-side (BUY) Marketable Limit Order ceiling, 2026-07-26 -- replaces
# the plain market `buy --max-price` call with `limit-buy --expiration fak`
# (bullpen's real Fill-And-Kill: fills whatever's available immediately up
# to the limit price, cancels the unfilled remainder -- confirmed live via
# `bullpen polymarket limit-buy --help`, not assumed). See
# bot.compute_entry_slippage_ceiling_pct() and RISK_MANAGEMENT.md Rule 33
# for the full reasoning: 145 real order-book-measured shortfalls
# (bot_event_log) showed a fat-tailed distribution (p50=3%, p90=100%,
# driven by a handful of illiquid one-off markets) that no single fixed
# ceiling fits well -- so the ceiling is tied to the strategy's own live
# edge instead (db.compute_live_edge_pct()): paying more in entry slippage
# than the whole realized edge is a guaranteed loser regardless of
# resolution outcome.
#
# OPT-IN, default OFF: existing LIVE_MODE buy call sites keep the current
# `buy --max-price` behavior unchanged unless this is explicitly flipped.
ENABLE_ENTRY_SLIPPAGE_CEILING_FAK = False

# clamp(SLIPPAGE_PROTECTION_FRACTION * live_edge_pct, floor, cap). Reuses
# SLIPPAGE_PROTECTION_FRACTION (0.30, already defined below for the exit
# side) rather than a second constant with the same value under a
# different name -- same conceptual role on both sides: "don't let
# execution cost eat more than 30% of live edge." floor=SLIPPAGE_TOLERANCE
# (this ceiling is never LESS protective than the existing static 5% ever
# was). cap=0.30: the approximate p90 of this project's own non-outlier
# historical shortfalls (114 of 145 measured trades, excluding a handful of
# illiquid-market outliers up to 18,775%) -- beyond this, a strong live
# edge would otherwise license an absurdly wide ceiling that defeats the
# point of having one.
ENTRY_SLIPPAGE_CEILING_CAP_PCT = 0.30

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
# EV-based circuit breaker (2026-07-26, Rule 35 rewrite -- replaces the
# original streak/win-rate triggers entirely). Confirmed live, twice, that
# a raw 3-loss-streak count false-positives on genuinely good wallets:
# strict-7 (+44.6% EV/dollar-staked over 31 trades) and crypto-specialist-1
# both got permanently muted by a short losing run inside an otherwise
# strong track record. This fixes that case, verified against both real
# histories.
#
# Does NOT fix the opposite failure: geo-anon-3 (83% win rate, -11% EV
# from rare catastrophic losses among many small wins) still isn't caught
# -- verified against its real 12-trade history, t=-0.92, nowhere near
# significant. A t-test needs a CONSISTENT edge; one huge outlier inflates
# variance enough that a handful of samples can't distinguish a
# fat-tailed/rare-catastrophic-loss wallet from noise. Stated plainly:
# that pattern still needs manual review (how it was actually caught),
# not automatic muting.
#
# The fix: a one-sample t-test on the wallet's own recent REAL (non-dust)
# per-dollar-staked returns, testing whether the mean is significantly
# NEGATIVE -- mirrors should_skip_category()'s existing pnl_t_stat
# approach (scoreWalletCategories.ts) exactly, computed live here since a
# wallet's own recent copy-trade history isn't available from that offline
# job. Reuses CATEGORY_SKIP_Z_CRITICAL as the same critical value, for the
# same reason: consistency, not a second invented threshold for the
# identical kind of decision. See bot.compute_wallet_ev_t_statistic().
#
# - MUTE_EV_MIN_SAMPLES: minimum real (non-dust) closed trades before the
#   t-test is even evaluated -- same value/reasoning as the old
#   MIN_TRADES_FOR_WIN_RATE_MUTE it replaces (a smaller sample makes any
#   statistical test unreliable).
# - MUTE_MIN_TRADE_COST_USD: a trade with cost_basis_usd below this is
#   dust -- excluded entirely from circuit-breaker tracking (doesn't count
#   as a win OR a loss). Confirmed live (fed-warren-buffett): a wallet
#   unwinding its own position in many small increments can produce
#   sub-cent "losses" (e.g. -$0.000006) via our proportional copy-sell,
#   which the OLD `pnl_usd > 0` check counted identically to a real loss.
#   Set to $1 -- our own smallest real configured trade is $3
#   (MIN_TRADE_USD), so anything materially below $1 cannot be a
#   deliberate copy, only a fragment/remainder.
MUTE_EV_MIN_SAMPLES = 10
MUTE_MIN_TRADE_COST_USD = 1.0

# Shadow Rehab (2026-07-27, Rule 37 -- the roadmap item Rule 35 deferred).
# A muted wallet's mute was previously permanent: check_circuit_breaker()
# only ever LATCHES a mute, and process_trade() blocks all new real BUYs
# for a muted trader, which means the exact tracking that would ever prove
# a recovery (real closed copy-trades) can never accumulate again. This
# replaces "never" with "once there's real statistical evidence of
# recovery": a muted wallet's REAL trades are still visible (mutes never
# block detection, only OUR copy) -- while muted, bot.py now simulates
# what a copy would have earned in an isolated, paper-only ledger
# (paper_trade.strategy="shadow_rehab", never touching real risk/exposure
# calculations, which read strategy="bot_filtered" exclusively) and
# reinstates the wallet once that shadow history shows a statistically
# significant POSITIVE edge -- the same t-test/critical-value machinery
# Rule 36 already built for muting, run in reverse. See
# bot.sweep_shadow_rehab()/bot._execute_shadow_buy().
#
# ENABLE_SHADOW_REHAB default True (not opt-in-off like Priority 3/4's
# execution-affecting mechanisms): shadow trades never touch real capital
# or real risk state, so the blast radius of a wrong rehab decision is
# bounded to "we resume copying a wallet a human could also have manually
# reinstated" -- the same risk already accepted for every manual
# reinstate this session (strict-4/strict-10/crypto-specialist-1/
# fed-warren-buffett/strict-7).
ENABLE_SHADOW_REHAB = True

# Fixed reference size for a shadow copy -- deliberately NOT the tiered
# wallet-score sizing a real copy would use: the rehab decision is driven
# entirely by RETURN ratios (pnl_usd/cost_basis_usd), which are
# scale-invariant, so a real copy's exact dollar size carries no extra
# signal here. Mid-point of MIN_TRADE_USD/MAX_TRADE_USD.
SHADOW_REHAB_TRADE_USD = 5.0

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

# --- Daily portfolio snapshot (2026-07-28, Grafana personal dashboard) ----
# One row/day in daily_portfolio_snapshots — equity, cash, unrealized PnL,
# today's realized PnL, and active-trader count, for a personal Grafana
# dashboard tracking long-term equity growth/edge/system stability. Purely
# additive logging — reads risk_manager.compute_equity_breakdown() and
# db.realized_pnl_today(), writes nothing back into any trading decision.

# UTC, not local time — same reason every other timestamp in this codebase
# is UTC (see now_iso()). 23 means "any poll cycle at or after 23:00 UTC
# that hasn't already snapshotted today" — the idempotency check is
# DB-backed (db.has_snapshot_for_today()), not a fixed-minute trigger, so
# this fires reliably even if the bot isn't polling at exactly 23:59:00.
DAILY_SNAPSHOT_TRIGGER_HOUR_UTC = 23

# How often the trigger condition itself gets checked — same cadence as
# the TTP sweep. The actual snapshot only ever writes once per day
# regardless of this interval; this just bounds how many "is it time yet"
# checks happen (a single indexed DB lookup each time, cheap either way).
DAILY_SNAPSHOT_CHECK_INTERVAL_SECONDS = 300

# --- "Zombie position" dump exit (2026-07-27) -----------------------------
# Found live: MAX_BOOK_AGE_SECONDS=15 is deliberately tight (see that
# constant's own comment) and correctly, routinely refuses individual TTP
# price reads on thin/illiquid markets — a real ~10-30% per-sweep miss rate
# that's fine on its own, since the next sweep usually succeeds. But a small
# subset of held markets are genuinely dead (near-zero real order flow, so
# Polymarket's own book timestamp never refreshes inside 15s, ever) or
# outright delisted/renamed ("no market found for slug") — for those, no
# amount of waiting fixes it, and capital just sits trapped. This is the
# escape hatch, deliberately kept SEPARATE from the main TTP sweep/closer
# rather than loosening MAX_BOOK_AGE_SECONDS or check_spread_tolerance
# globally, which would weaken protection for the ~99% of positions that are
# priced fine — see sweep_zombie_positions/close_position_zombie_dump.

# A position with no SUCCESSFUL price read in this long becomes eligible
# for the dump exit. Deliberately much longer than TRAILING_TP_CHECK_
# INTERVAL_SECONDS (300s) or even CLOSEOUT_INTERVAL_SECONDS (3600s) — this
# must clearly outlast an ordinary bad stretch of transient staleness
# before concluding a position is actually stuck, not just unlucky.
ZOMBIE_POSITION_THRESHOLD_SECONDS = 86400  # 24h

# How often the zombie sweep itself runs. Detection is nearly free (reads a
# timestamp already on each position, no network call) — this interval only
# governs how promptly an ALREADY-eligible position gets its exit attempt,
# not how often every position gets checked.
ZOMBIE_SWEEP_INTERVAL_SECONDS = 21600  # 6h

# Every Nth consecutive "market lookup itself is broken" failure (delisted/
# renamed slug, not just a stale book) re-logs a reminder, mirroring
# _closeout_fetch_failures' exact throttling reasoning in bot.py: the first
# failure is always logged loudly, repeats are suppressed so a chronically
# unresolvable position can't spam the log every sweep, but also can't
# silently vanish from it either.
ZOMBIE_UNRESOLVABLE_LOG_EVERY = 4  # ~daily at the 6h sweep interval

# The zombie dump's own price floor, in place of the normal 5%
# SLIPPAGE_TOLERANCE (deliberately NOT reused: a wide spread is expected —
# likely the very reason the position went stale in the first place — so
# gating the escape hatch on the same tolerance that let it get stuck would
# defeat the point). Looser than normal on purpose, but still a real floor:
# this is "aggressive," never "sell at any price."
ZOMBIE_EXIT_MAX_SLIPPAGE = 0.25

# OPT-IN, default OFF: sweep_zombie_positions' detection and the throttled
# "unresolvable" alert above always run and log regardless of this flag —
# only the actual dump-sell (close_position_zombie_dump) is gated behind
# it, so the first rollout is "watch what WOULD happen in the log" before
# any real position actually gets force-closed this way.
ENABLE_ZOMBIE_POSITION_DUMP = False

# Seconds between polls of the tracked-wallet feed.
POLL_INTERVAL_SECONDS = 30

# Per-wallet trade fetch limit for the direct Polymarket Data API poll
# (polymarket_data_api.py, cutover 2026-07-22 — see docs/copy-trading/
# RISK_MANAGEMENT.md Rule 14). Each wallet gets its own request now instead
# of one combined call across all wallets (the old FEED_LIMIT=150 model),
# so this only needs to comfortably exceed what ONE wallet could trade
# between polls — 20 is generous headroom even for a very active wallet at
# a 30s cadence.
DIRECT_API_PER_WALLET_LIMIT = 20

# How many recently-seen trade_ids to remember PER WALLET for dedup (2026-07-
# 31, replacing a single GLOBAL 2000-trade_id cap across all wallets
# combined). The old global cap let one busy wallet's volume evict a quiet
# wallet's older trade_ids; since DIRECT_API_PER_WALLET_LIMIT above always
# returns each wallet's most recent N trades regardless of how long ago they
# happened, an evicted-but-still-fetched trade_id would get treated as brand
# new on the next bot.py restart — confirmed live: two restarts in one
# session each triggered a burst of "new" copies of month-old,
# already-resolved-market trades from quiet wallets. 100 is 5x headroom over
# DIRECT_API_PER_WALLET_LIMIT (20) — comfortably more than one wallet could
# ever need to never re-see its own already-processed trades.
SEEN_TRADE_IDS_PER_WALLET_CAP = 100

# Belt-and-suspenders guard (2026-07-31), alongside the _mark_trade_seen()
# idempotency fix in bot.py: a BUY signal whose own reported timestamp is
# older than this gets one extra live resolution check
# (_market_already_resolved()) before opening a position, refusing to buy
# if the market has already settled. Confirmed live in production: a dedup
# gap (fixed alongside this) let a handful of already-resolved-market
# trades get silently un-deduped and re-copied as "new" roughly hourly for
# 14+ hours, each closed out again by the next hourly closeout sweep —
# repeatedly injecting phantom realized PnL into realized_pnl_total(),
# which corrupted risk_manager.compute_equity() (the drawdown kill switch)
# with a fake equity swing. 1 hour: comfortably longer than any genuine
# copy-trading signal should ever be (this bot's own poll cadence is 30s),
# short enough that this extra fetch essentially never fires for real,
# fresh activity — only for exactly the pathological stale-replay case
# this exists to catch.
STALE_TRADE_RESOLUTION_CHECK_SECONDS = 3600

# Seconds between recovery-check attempts once the bot has HALTED on a
# bullpen authentication failure (exit code 2 — see BullpenAuthError in
# bullpen_client.py). Deliberately much slower than POLL_INTERVAL_SECONDS:
# added 2026-07-21 after a real incident where an expired session caused
# ~2 hours of continuous failed polls at the normal 30s cadence (264 error
# rows for one outage alone) instead of stopping and waiting for a human to
# re-run `bullpen login`. NOTE (2026-07-22): the tracking feed itself no
# longer uses bullpen at all (see DIRECT_API_PER_WALLET_LIMIT above), so
# this specific halt-and-recover path is currently dormant for tracking —
# left in place, still tested, since bullpen remains in use elsewhere
# (execution, shortfall previews, closeout sweeps) where the same pattern
# could still matter.
AUTH_RECHECK_INTERVAL_SECONDS = 120

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

# Hard subprocess timeouts (seconds) for bullpen calls, split by call class
# (added 2026-07-19, defense-in-depth after the 2026-07-18 overnight freeze
# — the machine suspending mid-call froze the whole poll loop; a timeout
# can't prevent an OS suspend, but it guarantees no single call can wedge
# the loop for longer than this once the machine is awake again).
#
# - BULLPEN_CALL_TIMEOUT_SECONDS: default for every call. Deliberately
#   generous for buy/sell — a tight ceiling there would create MORE
#   unknown_fill_state outcomes (order submitted, response leg cut off),
#   which is the worst failure mode we have. Don't lower this one.
# - FEED_POLL_TIMEOUT_SECONDS: tracker-feed poll only — the bot's highest-
#   frequency call and the one that froze. A feed read slower than the
#   30s poll interval is already a failed cycle in practice, so it gets a
#   tight ceiling; with FEED_FETCH_RETRIES=3 the worst case stays ~1 min.
BULLPEN_CALL_TIMEOUT_SECONDS = 60
FEED_POLL_TIMEOUT_SECONDS = 20

# Optional private Polygon RPC endpoint (e.g. an Alchemy/QuickNode app URL)
# to cut latency and get `eth_getLogs` support the public fallback RPC
# lacks. Leave unset (env var absent) to use bullpen's built-in public
# Polygon RPC. Get this from https://dashboard.alchemy.com -> your app ->
# HTTPS URL. Bullpen reads this via the BULLPEN_POLYGON_RPC_URL env var
# (verified via `bullpen --help` and the CLI's own error-message text) —
# bot.py sets that env var from this value before making any bullpen
# subprocess call, which covers ALL bullpen commands (feed polling AND live
# buy/sell), not just a subset. There is no in-repo web3/RPC client of our
# own to point at it; bullpen owns 100% of the on-chain interaction for
# this bot.
#
# Read from the PRIVATE_POLYGON_RPC_URL env var (added 2026-07-22 for the
# AWS deployment: the cloud deploy script injects this from SSM Parameter
# Store at container-start time — see infra/deploy/run.sh — so the real
# value never lives in this committed file or in the Docker image). Local
# dev behavior is unchanged: env var unset -> None -> same as the old
# hardcoded default.
PRIVATE_POLYGON_RPC_URL = os.environ.get("PRIVATE_POLYGON_RPC_URL")

# --- Portfolio-level risk controls (risk_manager.py) -------------------------
# These gate NEW BUYs only — sells, trailing-TP exits, and closeouts are
# never blocked (a risk layer that traps you in positions adds risk instead
# of removing it). Applied in BOTH paper and live mode so paper runs stay
# representative. Chosen 2026-07-18 ("Moderate" bundle + owner's bankroll
# numbers, see below); any limit can be disabled by setting it to None.

# Hard ceiling on total USD deployed (sum of open positions' cost basis).
# A BUY that would push total exposure ABOVE this is skipped. Raised
# 2026-07-25 from $250 to $1250 (Joey added $1000 more capital) after
# real data showed the OLD $250 cap was the dominant bottleneck on trade
# volume — 3,347 skip_risk_exposure_ceiling events over the prior 2 days
# (100% against this specific cap, not the per-event/per-wallet ones),
# blocking 416 distinct (wallet, market) signals while the portfolio sat
# at $247.22/$250. MAX_EVENT_EXPOSURE_USD/MAX_WALLET_EXPOSURE_USD below
# deliberately NOT scaled alongside this — left at their original $30/$50,
# which now means fuller utilization of the new $1250 requires broader
# diversification (~42 events / ~25 wallets to fully deploy), a reasonable
# side effect, not an inconsistency, unlike PAPER_BANKROLL_USD/
# EQUITY_FLOOR_USD/MAX_DRAWDOWN_FROM_PEAK_USD below, which HAD to move
# together with this to keep the kill switch's equity math accurate.
MAX_TOTAL_EXPOSURE_USD = 1250.0

# Cap on USD deployed within a single Polymarket EVENT (markets are grouped
# into events; e.g. every "What price will Bitcoin hit in July?" strike is
# one event). Resolved via `bullpen polymarket market <slug>` ->
# events[0].slug, cached in bot_market_event. NOTE this only catches
# same-event concentration — economically-correlated bets split across
# different events (e.g. "BTC reach 72.5k" vs "BTC dip to 50k") are NOT
# caught; cross-event correlation stays unmodeled for now.
MAX_EVENT_EXPOSURE_USD = 30.0

# Cap on USD deployed across ALL open positions tied to a single TRADER
# (added 2026-07-24, Rule 26 — motivated by the category-quota system, Rule
# 24, allowing one wallet to occupy multiple category slots when its stats
# justify it: a wallet's edge might be process-driven — speed, arbitrage —
# rather than domain-specific, so multi-category tracking is allowed on
# purpose, but shouldn't mean multiplying that wallet's capital budget by
# however many slots it fills). A genuinely different axis from the two
# caps above: MAX_TOTAL_EXPOSURE_USD is portfolio-wide, MAX_EVENT_EXPOSURE_USD
# is per-EVENT (could span several wallets), this is per-WALLET (could span
# several events/categories for the same trader). See
# risk_manager.wallet_exposure_usd().
MAX_WALLET_EXPOSURE_USD = 50.0

# VIP override, per wallet, of MAX_WALLET_EXPOSURE_USD above (2026-07-26,
# Rule 35) -- proven, high-EV, high-volume wallets get a higher individual
# cap so their edge isn't left underdeployed at the same flat $50 limit as
# an unproven wallet. Manually curated, address-keyed (lowercase — see
# risk_manager.wallet_exposure_cap_usd()), not automatic: with the
# EV-based circuit breaker and Shadow Rehab (below) still new, there isn't
# yet a trustworthy automatic signal to key a bigger allocation off, so
# this is a human decision for now, same status as TRACKED_TRADERS
# membership itself. MAX_TOTAL_EXPOSURE_USD (the portfolio-wide ceiling)
# still applies on top regardless -- this only raises the per-wallet
# sub-limit, never the total pot.
VIP_WALLET_EXPOSURE_CAP_USD = {
    # strict-7: 31 closed trades, +44.6% EV/dollar-staked, +$56.83 total
    # P&L, 74% win rate -- highest-volume strong performer of any tracked
    # wallet (2026-07-26 Rule 34 review).
    "0x65018f9fc473f6e920b8929a375d39c26a461220": 150.0,
    # political-whale-1: 4 closed trades, +183.9% EV/dollar-staked,
    # +$36.52 total P&L, 75% win rate -- best EV of any tracked wallet,
    # smaller sample than strict-7 so a more conservative cap.
    "0x510904c9a58f5c5ad799a1b44947077564175e9c": 100.0,
}

# Depth-Aware Trade Sizing (added 2026-07-28) — clamps a SINGLE trade's own
# size to a fraction of ITS market's visible order-book depth
# (risk_manager.depth_capped_trade_size_usd()), independent of
# MAX_EVENT_EXPOSURE_USD/MAX_WALLET_EXPOSURE_USD above. Deliberately NOT a
# change to those two caps: book depth is a property of the specific market
# being traded right now, not something that aggregates the way dollars-
# deployed-across-positions does — there's no single "depth" number for a
# whole wallet's exposure, which can span many different markets each with
# their own book. See docs/copy-trading/RISK_MANAGEMENT.md's Depth-Aware
# Trade Sizing rule for the full reasoning.
#
# OPT-IN, default OFF: bot.py's process_trade() always fetches and logs what
# the depth-capped size WOULD have been (event_type="depth_cap_would_apply",
# only when it would have actually bound) regardless of this flag — only the
# actual clamp (shrinking trade_usd before _execute_buy) is gated behind it,
# same "watch what would happen in the log first" rollout pattern as
# ENABLE_ZOMBIE_POSITION_DUMP.
ENABLE_DEPTH_AWARE_TRADE_SIZING = False

# Max fraction of a market's own visible ask-side book depth (in USD, summed
# across levels) a single trade may consume. 0.05 (5%): an explicit judgment
# call, not derived from data — same "chosen, not measured" status as
# SLIPPAGE_PROTECTION_FRACTION and every other constant in this file labeled
# that way. Revisit once ENABLE_DEPTH_AWARE_TRADE_SIZING has run in
# observability-only mode long enough to show how often it would actually bind.
TRADE_SIZE_DEPTH_FRACTION = 0.05

# "Dip & Rebound" resting paper orders (added 2026-07-24, Rule 29 — pilot
# scoped to strict-4 only). Wallets in this set skip the normal immediate
# copy path entirely: instead of buying at whatever price is live the
# moment their trade is observed, bot.py tracks their VWAP cost basis
# (anchor_price, see bot_source_position.cost_basis_usd) and opens a
# pending_execution row, which only fires once BOTH (a) price has dipped
# below that anchor and rebounded off the resulting local low by a
# confirmed margin (see compute_rebound_threshold()), and (b) the wallet is
# independently confirmed to still hold the position (see
# whale_still_holding()) — never on a bare touch of the anchor price. See
# docs/copy-trading/RISK_MANAGEMENT.md Rule 29 for the full adverse-selection
# rationale (a naive "buy the moment price touches the whale's cost basis"
# design is exactly the "picked off by informed flow" failure mode the
# limit-order literature warns about — a dip is more often real news moving
# against the position than mean-reverting noise).
#
# Empty by default; wallets are opted in explicitly, one at a time, not
# switched on globally — this is a genuinely different execution model from
# every other tracked wallet's, worth validating narrowly before widening.
LIMIT_ORDER_TRACKED_WALLETS = {"0xE16D3F2A5807999b358aFfD9445C3a09E45E5e30"}  # strict-4, pilot only

# Polymarket's own minimum price increment — the unit compute_rebound_threshold()
# expresses its tick-floor component in.
LIMIT_ORDER_TICK_SIZE = 0.01

# How long a pending_execution rests before being abandoned if price never
# confirms a rebound (or confirms one but has already run past anchor_price
# — see sweep_pending_executions()'s no-chase guard). 4 hours: long enough
# to ride out a single esports match or a slow-moving political news cycle,
# short enough that a stale, no-longer-live signal doesn't rest forever —
# an explicit judgment call, not derived from data, same status as
# TCA_MIN_ENTRY_PRICE's own "explicit, not dressed up as rigorous" framing.
LIMIT_ORDER_TTL_SECONDS = 4 * 3600

# Rebound-confirmation threshold, evaluated as
# max(LIMIT_ORDER_REBOUND_TICK_FLOOR * LIMIT_ORDER_TICK_SIZE,
#     LIMIT_ORDER_REBOUND_PCT * lowest_seen_price) — see
# compute_rebound_threshold()'s docstring for why a hybrid, not either alone:
# a pure percentage degenerates to sub-tick noise at longshot prices (5% of
# $0.05 is a quarter of one tick — meaningless as "confirmation" of
# anything), while a pure tick count under-confirms near $0.95 and
# over-confirms near $0.05 in relative terms. The tick floor dominates at
# low prices, the percentage dominates in the mid-to-high range, and the
# threshold scales continuously between them — no separate branching logic
# needed at the call site.
LIMIT_ORDER_REBOUND_TICK_FLOOR = 2
LIMIT_ORDER_REBOUND_PCT = 0.05

# Guard 4: before ever firing on a confirmed rebound, the wallet must still
# hold at least this fraction of the shares it held when the pending_execution
# was created (source_positions[key], via db.get_pending_execution's
# whale_shares_at_creation snapshot) — if it's dropped below this, the whale
# has exited/materially trimmed the position, which is a much stronger "this
# dip is real, not noise" signal than the price move alone, and the pending
# order is invalidated rather than fired. 0.5 (must still hold at least
# half): a genuine full/majority exit should block the buy; a small routine
# trim (a whale taking some profit off a large position) should not nuke
# every pending order on a minor rebalance — an explicit judgment call
# between those two failure modes, not derived from data.
LIMIT_ORDER_WHALE_HOLD_MIN_FRACTION = 0.5

# Consumer sweep for wss_listener.py/token_sync_worker.py's producer tables
# (live_whale_event/token_registry, see bot.sweep_live_whale_events()).
# Caps how many unconsumed on-chain events get processed in one poll cycle
# — a large backlog (e.g. after the WSS listener was down for a while)
# shouldn't monopolize a single cycle; whatever's left over is simply
# picked up on the next one, 30s later. Not a correctness requirement, a
# defensive bound.
WHALE_EVENT_SWEEP_BATCH_LIMIT = 50

# 'Unknown token' on-demand fallback (2026-07-25, Rule 30 addendum): when a
# live_whale_event's token_id has no token_registry match yet (a brand-new
# market wss_listener.py detected before token_sync_worker.py's periodic
# sync ever saw it), the sweep calls Gamma directly
# (polymarket_simulator.fetch_market_by_token_id) rather than skipping the
# event. If that ALSO comes back empty (Gamma genuinely hasn't indexed the
# market yet either), the event is left unconsumed and retried on later
# sweeps — but not forever: past this age, the sweep gives up and marks it
# consumed rather than retrying indefinitely for a token_id that may never
# resolve. An explicit judgment call, not derived from data on how long
# Gamma indexing actually lags in practice.
WHALE_EVENT_FALLBACK_MAX_AGE_SECONDS = 3600

# 'Dual-Track' WSS-as-primary-trigger (2026-07-25): when a live_whale_event
# has no derivable whale price (the collateral-transfer leg wasn't found —
# see wss_listener.py), the sweep no longer skips it. It executes anyway,
# using OUR OWN current market price (get_market_ask_price) as the
# reference for sizing, and reconstructs an accurate source-side dollar
# value from the on-chain event's raw share_amount (always known exactly,
# unlike price) times that reference price — so source_positions[key]'s
# SHARE COUNT is always exact, only its dollar cost basis is an estimate
# until the polling loop's reconciliation pass corrects it (see
# bot.process_trade()'s reconciliation branch). This decimals assumption
# must match wss_listener.py's own OUTCOME_TOKEN_DECIMALS env var if that's
# ever overridden — a real, flagged drift risk between the two separate
# processes, not resolved by a shared source of truth here.
OUTCOME_TOKEN_DECIMALS_ASSUMPTION = 6

# --- Bifurcated dynamic order pegging for SELL/exit execution ("Priority
# 3", 2026-07-26) --------------------------------------------------------
# OPT-IN, default OFF: existing SELL call sites keep their current
# immediate-execution behavior unchanged unless this is explicitly flipped.
# Even when enabled, this can NEVER result in an exit silently not
# happening — every pending_exit_order terminates in either a real fill or
# a guaranteed market-sell fallback (see bot.sweep_pending_exit_orders()) —
# "never delay an exit indefinitely" stays true regardless of this flag.
ENABLE_PATIENT_EXIT_PEGGING = False

# Slippage floor = PROTECTION_FRACTION * the LIVE measured edge (see
# db.compute_live_edge_pct()), not a hardcoded snapshot — the whole point
# is protecting whatever the edge actually is right now, and this session's
# own sizing research showed that number moving materially within hours.
# 0.30 (protect at most 30% of the edge to slippage on a patient exit): an
# explicit judgment call, not derived from data — same status as every
# other "chosen, not measured" constant in this file (see
# TCA_MIN_ENTRY_PRICE's own comment for the same framing).
SLIPPAGE_PROTECTION_FRACTION = 0.30

# db.compute_live_edge_pct() returns None below this many samples (too
# thin to trust) — this is the fallback edge assumption used instead, so
# the floor calculation always has a number to work with even early on
# with little closed-trade history. Deliberately conservative (smaller
# than the ~20% currently measured) rather than assuming a favorable edge
# that hasn't been earned yet.
LIVE_EDGE_MIN_SAMPLES = 20
ORDER_PEG_FALLBACK_EDGE_PCT = 0.05

ORDER_PEG_TICK_DECREMENT = 0.01

# Liquidity regime check: spread_ratio = (ask - bid) / mid. Above this
# ratio is treated as "low liquidity" (reprice less often — don't chase a
# thin book down every 30s and give away edge to noise); at or below is
# "high liquidity" (reprice quickly — a tight book means a stale quote is
# more likely to just be sitting there missing fills for no reason).
ORDER_PEG_LOW_LIQUIDITY_SPREAD_RATIO_THRESHOLD = 0.05
ORDER_PEG_LOW_LIQUIDITY_INTERVAL_SECONDS = 120
ORDER_PEG_HIGH_LIQUIDITY_INTERVAL_SECONDS = 30

# Hard bound on total patient-wait time before giving up and firing an
# immediate market sell instead — NOT the "maybe an hour" originally
# floated. An hour of resting exposure while trying to shave a few cents
# is exactly the "risk layer traps you in a position" failure mode Rule
# 6/11 exist to prevent; 10 minutes is a judgment call favoring "still get
# a materially better price most of the time" over "let the market run
# away from us while we wait."
ORDER_PEG_MAX_TOTAL_WAIT_SECONDS = 600

# --- Theta-decay trailing take-profit activation ("Priority 4",
# 2026-07-26) ---------------------------------------------------------
# Replaces the single fixed TRAILING_TP_ACTIVATION_PCT (0.50) with a
# threshold that scales DOWN as a market's resolution date approaches — a
# spike far from resolution has more time to reverse (require a bigger,
# more-convincing move before trusting it); a spike close to resolution
# has less time left to reverse (trust a smaller move). Motivated directly
# by real evidence: 4 resolved positions this session peaked between
# 19%-50% unrealized profit and never got protected because they never
# reached the fixed 50% bar (see the 2026-07-25 sizing research report,
# section 6.2) — a real $11.92 swing on just those four. THETA_DECAY_MIN/
# MAX and the 7-day window are explicit judgment calls (Joey's own
# specification), not derived from a larger backtest — same "test before
# fully trusting" caveat already on record for that finding.
THETA_DECAY_TP_MIN_ACTIVATION_PCT = 0.15
THETA_DECAY_TP_MAX_ACTIVATION_PCT = 0.50
THETA_DECAY_TP_WINDOW_DAYS = 7

# OPT-IN, default OFF — same reasoning as ENABLE_PATIENT_EXIT_PEGGING:
# this changes a real, already-live trading behavior
# (TRAILING_TP_ACTIVATION_PCT's role in check_trailing_take_profit), and
# depends on a market end-date lookup that doesn't exist anywhere in this
# codebase yet (see resolve_market_end_date() in bot.py) — not something
# to silently switch on.
ENABLE_THETA_DECAY_TP_ACTIVATION = False

# Kill switch: portfolio equity is defined as PAPER_BANKROLL_USD + realized
# PnL + unrealized PnL (unrealized comes from the trailing-TP sweep's price
# fetches, so equity refreshes every TRAILING_TP_CHECK_INTERVAL_SECONDS ~5
# min, not per-trade). TWO independent triggers latch the same halt:
#
#   1. EQUITY_FLOOR_USD — the catastrophic stop: halt if equity ever drops
#      below this absolute level. Original owner's framing (2026-07-18):
#      "trading with $125, stop if I'm down to $100" — an 80%-of-bankroll
#      floor (allow up to a 20% total loss).
#   2. MAX_DRAWDOWN_FROM_PEAK_USD — the working stop: halt if equity falls
#      this far below its own high-water mark. Originally 40% of bankroll.
#
# Rescaled 2026-07-25 alongside MAX_TOTAL_EXPOSURE_USD's $250->$1250 raise
# (Joey added $1000 more capital): PAPER_BANKROLL_USD moved from $125 to
# $1125 to match the real capital added, and EQUITY_FLOOR_USD/
# MAX_DRAWDOWN_FROM_PEAK_USD were scaled proportionally to preserve the
# SAME risk tolerance (80%-of-bankroll floor, 40%-of-bankroll max
# drawdown) rather than staying fixed in dollar terms — leaving them at
# the old $100/$50 would have meant the kill switch tripping on a ~4-9%
# move against the new, much larger bankroll, dramatically tighter than
# originally intended.
#
# Once triggered the halt LATCHES (persisted in bot_risk_state, survives
# restarts) until manually cleared with `python3 reset_kill_switch.py` —
# a breached limit means a human reviews before risk resumes.
PAPER_BANKROLL_USD = 1125.0
EQUITY_FLOOR_USD = 900.0
MAX_DRAWDOWN_FROM_PEAK_USD = 450.0

# risk_manager.compute_unrealized_pnl() excludes any position bought within
# this distance of $0 or $1 from the kill switch's mark-to-market calc
# (carried at cost instead) — see that function's docstring. Confirmed live
# 2026-07-31: a handful of ultra-longshot 2028-election bets (avg_entry_price
# 0.001-0.008, $5-10 cost basis each — a tiny dollar cost implies a HUGE
# share count at these prices) coincided with the drawdown kill switch's
# equity swinging ~$4900-5000 in a single sweep from one bad/stale CLOB read
# on an illiquid market, latching the kill switch on a fabricated drawdown.
# 0.02 matches DEFAULT_TCA_MIN_ENTRY_PRICE (packages/copy-trading/src/
# discoverCategorySpecialists.ts) — same "near-tick-extreme" judgment call,
# reused here for a different subsystem, not re-derived.
EQUITY_MARK_MIN_ENTRY_PRICE = 0.02

# Per-TRADE entry-price floor (2026-07-31) — process_trade()'s BUY branch
# skips copying an individual trade priced within this distance of $0 or $1,
# regardless of which wallet made it. Rule 27's TCA_MIN_ENTRY_PRICE (TS,
# discoverCategorySpecialists.ts) already does the analogous check at
# WALLET-discovery time (reject a whole candidate wallet); this is the
# missing per-trade counterpart for an otherwise-normal tracked wallet that
# occasionally also dabbles in an extreme-tail bet. Investigated live: 74
# distinct open positions were hitting polymarket_simulator.
# MAX_BOOK_AGE_SECONDS's staleness guard 100+ times/day EACH, because these
# specific chronically-thin markets (2028-election longshots, multi-year-out
# crypto price targets, etc.) rarely trade at all — before the
# get_market_prices() stale-tolerant-fallback fix (same commit), this froze
# their TTP peak-tracking entirely, structurally forcing them into the
# held-to-resolution bucket the 2026-07-25 sizing report already found is
# net-NEGATIVE EV (-13% of stake) — nearly all this bot's real positive edge
# comes from copying the whale's own sell or a live trailing-TP exit, both
# of which need a workable price feed to fire at all. Same 0.02 value as
# EQUITY_MARK_MIN_ENTRY_PRICE/TCA_MIN_ENTRY_PRICE, not re-derived.
PER_TRADE_ENTRY_PRICE_FLOOR = 0.02

# Files (all inside this project directory)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_PATH = os.path.join(BASE_DIR, "trades_log.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

# Shared SQLite DB (packages/db owns the schema/migrations; db.py only ever
# does SELECT/INSERT/UPDATE/DELETE against it, never CREATE/ALTER TABLE).
# Absolute path, same resolution rule as packages/db/src/env.ts, so both
# sides always agree on one file regardless of each process's cwd.
SQLITE_PATH = os.path.join(BASE_DIR, "data", "app.db")

# Console/status log files (2026-07-22, disk-exhaustion hardening). These
# are PLAIN TEXT FILES (bot.py/dashboard.py's own print()->logger.info()
# operational messages — startup banner, SIGTERM notices, kill-switch
# alerts) — NOT the same thing as bot_event_log, which is a SQLite TABLE
# written via direct SQL INSERT in db.append_log() and has never gone
# through Python's `logging` module. logging.handlers.RotatingFileHandler
# genuinely applies to these two files; it has no purchase on a database
# table (see EVENT_LOG_RETENTION_DAYS below for that one instead).
BOT_LOG_PATH = os.path.join(BASE_DIR, "bot.out.log")
DASHBOARD_LOG_PATH = os.path.join(BASE_DIR, "dashboard.out.log")
LOG_MAX_BYTES = 50 * 1024 * 1024  # 50MB
LOG_BACKUP_COUNT = 5

# bot_event_log (SQLite table, data/app.db) retention. Age-based, not a row
# cap: a fixed window of recent history is more useful for this table
# (debugging, shortfall/PnL analysis) than an arbitrary row ceiling. 180
# days is generous enough to keep months of research data available while
# still bounding the table's long-run growth (was 127MB/15k+ rows and
# growing with zero pruning before this).
EVENT_LOG_RETENTION_DAYS = 180
PRUNE_INTERVAL_SECONDS = 86400  # once/day — cheap, no need to run more often

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

# Pool-refill target (2026-07-27, Rule 37 -- the other Rule 35 roadmap
# item): Joey's stated goal is an ACTIVE (tracked AND not muted) copying
# pool near 20, not just a TRACKED_TRADERS count of 20 -- muting will
# always take some wallets out of rotation even with Shadow Rehab
# recovering others. propose_pool_refill.py compares the real active
# count (from bot_risk_state["tracked_traders"] minus circuit_breaker_muted,
# the same source the dashboard now reads) against this target and, if
# below it, PROPOSES the next-best untried candidate -- deliberately never
# auto-promotes; every TRACKED_TRADERS change this session has gone
# through explicit review, and this doesn't change that.
TARGET_ACTIVE_TRADER_COUNT = 20

# Minimum wallet_profile.composite_score for a candidate to even be
# proposed by propose_pool_refill.py -- matches the discovery pipeline's
# own existing default filter (discoverCategorySpecialists.ts), reused for
# consistency rather than a second invented bar for the same kind of
# decision.
POOL_REFILL_MIN_COMPOSITE_SCORE = 0.2

# How many extra candidates propose_pool_refill.py pulls beyond the actual
# gap, as a buffer against bot-detection rejections (2026-07-27 addition —
# the first real run proposed 4 candidates, ALL 4 confirmed
# is_likely_bot=true via `bullpen polymarket wallet-stats` once actually
# checked; wallet_profile's own is_likely_bot column was NULL for all of
# them, so the composite_score filter alone couldn't have caught this).
# 4x is a judgment call, not measured from a large sample -- one data
# point (0-for-4 real candidates) isn't enough to fit a real bot-rate
# estimate, so this errs generous rather than pretending precision it
# doesn't have.
POOL_REFILL_CANDIDATE_FETCH_MULTIPLIER = 4

# Sample size for propose_pool_refill.py's per-candidate activity pull
# (2026-07-27, second revision -- see that script's module docstring for
# why is_likely_bot alone was replaced with real evidence: being
# algorithmic isn't itself disqualifying, being unreplicable is --
# liquidity-rewards/rebate/micro-arbitrage edge specifically, confirmed
# live by pulling real `bullpen polymarket activity` for 2 of the first 4
# candidates and finding the exact pattern: repeated SELLs at the same
# near-zero price in the same market, wildly varying sizes minutes apart).
# 50 is a quick-look sample, not a full history -- enough to see a
# repeated-quote pattern if one exists, not meant to be exhaustive.
POOL_REFILL_ACTIVITY_SAMPLE_SIZE = 50

# --- Phase 1 observability (2026-07-31): Prometheus metrics + Telegram
# alerts -- see docs/copy-trading/SAFETY.md Sec.54. Built directly in
# response to tonight's kill-switch incident, which was only found because
# Joey happened to ask "how's the bot running" -- these should surface that
# kind of thing without a manual SSH check.

# bot.py exposes a Prometheus /metrics endpoint on this port (localhost
# only -- see monitoring/docker-compose.yml, Prometheus/Grafana are the only
# consumers, both on the same EC2 box; never exposed to the public internet,
# same "no new open ports" posture as the rest of this deploy post-2026-07-29).
METRICS_PORT = 9100

# Telegram alerts no-op safely (never raise, never block the main loop) if
# TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID aren't set in .env -- see
# telegram_alerts.py. Both env vars already existed in .env.example
# (added 2026-07-26 for a TS end-of-day report that was never actually
# built) -- reused here, not re-invented.
ENABLE_TELEGRAM_ALERTS = True

# A single Python exception (event_type="error") sends an immediate
# Telegram alert, but no more than one per this many seconds -- a burst of
# the same underlying failure (e.g. a CLOB outage hitting every open
# position in one sweep) must not flood Telegram the way it would flood a
# human's phone. Suppressed-during-the-window errors are folded into the
# next alert's count rather than silently dropped. 5 min matches the TTP
# sweep cadence this codebase already treats as its "reaction time" unit
# elsewhere (risk_manager.py's kill-switch equity refresh).
TELEGRAM_ERROR_ALERT_THROTTLE_SECONDS = 300

# The daily Telegram PnL summary piggybacks on
# maybe_snapshot_daily_portfolio()'s existing once-per-UTC-day trigger
# (config.DAILY_SNAPSHOT_TRIGGER_HOUR_UTC above) rather than a second
# schedule -- no new constant needed here.
