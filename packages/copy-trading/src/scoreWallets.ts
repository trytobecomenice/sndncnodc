// npm run scan:wallets
//
// =============================================================================
// WHAT THIS SCRIPT DOES, IN ONE PARAGRAPH
// =============================================================================
// `scanLeaderboard.ts` already found ~160 candidate wallet addresses and
// saved them to the `leaderboard_scan` table — but that was just "here's who
// exists," not "here's who's actually worth copying." THIS script is the
// one that answers that second question: for every candidate wallet, it
// fetches real performance data from the `bullpen` CLI, runs it through a
// scoring formula (ROI + consistency + win rate + copyability, all scoped to
// a RECENT rolling window — see SECTION 4b below), ranks the survivors, and
// writes a `status` of "track" / "watch" / "ignore" onto each wallet in the
// `wallet_profile` table. `bot.py` reads that `status` column to decide who
// to actually copy-trade, once config.TRACKED_TRADERS_SOURCE is flipped from
// "static" to "db" (the mechanism exists in bot.py/db.py; the switch itself
// is currently left "static" pending review of a scored batch).
//
// This is meant to be re-run MONTHLY (a deliberate design, not an
// afterthought — see "MONTHLY REBALANCING" below): each run re-scores every
// candidate from scratch off the CURRENT rolling window, so a wallet that
// was hot last month but has gone cold gets dropped, and a wallet that just
// became hot gets picked up, without any manual bookkeeping.
//
// =============================================================================
// ROLLING WINDOW: WHY LIFETIME-HISTORY SCORING WAS WRONG FOR THIS STRATEGY
// =============================================================================
// The original version of this script scored wallets off their ENTIRE
// trading history (lifetime trade count, lifetime days-since-first-trade,
// the full pnl-series back to account creation). That systematically
// under-scores a real, valuable pattern: a wallet that was DORMANT for many
// months and then became highly active and consistently profitable in the
// last 2-3 months. Two concrete mechanisms caused this:
//
//   1. Copyability's "trades per day" was `lifetime trade count / days since
//      first trade`. A wallet active 90 days out of a 2-year-old account
//      gets its true recent pace divided by ~10x too many days, landing
//      near the "too rare to copy" floor even though it currently trades
//      multiple times a day.
//   2. Consistency (Sharpe-proxy) and the one-hit-wonder check were computed
//      over the wallet's FULL pnl-series (every ~hourly snapshot bullpen
//      has, often 1-2 years deep). A long flat dormant stretch dilutes the
//      average gain-per-period without shrinking the volatility of the real
//      recent trend proportionally — the dormant history drags the score
//      down for no good reason.
//
// The fix, applied throughout SECTION 4 below: every performance metric
// that can be computed from RECENT data now is — `rollingWindowDays`
// (default 90) trims the pnl-series before consistency/one-hit-wonder/win-
// rate are computed, and copyability's frequency term uses bullpen's own
// `avg_trades_per_day_30d` field (a real 30-day rate) instead of a
// lifetime-diluted average. A wallet's ancient history no longer punishes
// it — only whether it's good and active RIGHT NOW does.
//
// =============================================================================
// MONTHLY REBALANCING: THE OTHER HALF OF THIS DESIGN
// =============================================================================
// Scoring off a recent window only fixes half the problem — the other half
// is making sure a wallet that stops being hot gets DROPPED, not left
// tracking forever off a score computed months ago. Two mechanisms:
//
//   - A hard recency gate (`maxDaysSinceLastTrade`, default 30): a wallet
//     that hasn't traded at all within that window is forced to "ignore"
//     regardless of how good its historical composite score looks — see
//     `finalizeAndWrite`. This is deliberately independent of the composite
//     score math so it can't be argued away by a good ROI number.
//   - Re-running this script re-evaluates EVERY candidate from scratch each
//     time — there's no "grandfathering" of a wallet's previous status. If
//     it's cold this month, it's out this month, full stop.
//
// =============================================================================
// TOP-15 POOL (WITH A SIGNAL-VOLUME SAFETY VALVE)
// =============================================================================
// Previously, every wallet independently clearing the `track` composite-
// score threshold became "track" — no cap. This version ranks all wallets
// that clear the threshold by compositeScore and keeps only the top
// `topNPoolSize` (default 15) as "track"; the rest are demoted to "watch"
// (kept warm for next month, not fully dropped).
//
// The pool size isn't perfectly fixed at 15, though: if the bot's actual
// recent copy-trading activity (bot_event_log, last 30 days) is thin — i.e.
// the current pool isn't generating enough real trades to learn from —
// the pool automatically expands by `topNPoolExpansion` (default 5, so 20
// total) to bring in more signal. Once trade volume is healthy again, it
// shrinks back to the base 15. See `computeDynamicPoolSize`.
//
// =============================================================================
// TOXIC-FLOW HARD GATE: LOW CONSISTENCY DESPITE A HIGH WIN RATE
// =============================================================================
// After the first real v2 run, a real pattern showed up: a few wallets
// scored a high win rate (60-84% of rolling-window periods positive) but a
// near-zero consistencyScore (Sharpe-proxy). That combination is a known
// signature of volume-farming / rebate-harvesting: lots of individually
// "winning" small trades, but the mark-to-market portfolio series between
// them is choppy/noisy rather than a clean upward trend — the wallet's
// small realized edge doesn't show up as a smooth gain because it's mixed
// in with volatile open-position marks. Copying that pattern through this
// bot's fixed-size, poll-interval, real-slippage copy mechanism inherits
// the choppiness without the source wallet's razor-thin rebate edge —
// toxic flow for a copier even when it's a fine strategy for its owner.
//
// `minConsistencyScore` (default 0.20) is a HARD gate: any wallet whose
// rolling-window consistencyScore falls below it is forced to
// `status = "ignore"` with reason "toxic flow - volume farmer (low
// consistency)", BEFORE compositeScore or the top-N ranking get a say —
// see finalizeAndWrite. A sky-high ROI or win rate cannot buy its way past
// this gate.
//
// ONE CARVE-OUT, added deliberately: analyzePnlSeries also returns
// consistencyScore = 0 when there simply isn't enough rolling-window data
// yet (fewer than 3 windowed points) — that's "we don't know," not "we know
// it's toxic." Gating on the raw number without distinguishing the two
// would mislabel a data-availability gap as a confirmed volume farmer, so
// low-data wallets are exempted from THIS gate and fall through to the
// normal composite-score path instead, where a low/neutral consistencyScore
// already weighs the outcome down proportionally rather than accusing it of
// anything. See SeriesAnalysis.insufficientData.
//
// =============================================================================
// WHY IT RUNS IN TWO PASSES (this matters for understanding the code below)
// =============================================================================
// Getting a wallet's FULL history (the `pnl-series` call) takes ~15 seconds
// per wallet, empirically measured against the real `bullpen` CLI. Doing
// that for all ~160 candidates would take the better part of an hour. So,
// like a real research pipeline would:
//
//   PASS 1 (cheap, ~9.5s/wallet): fetch just the summary numbers (lifetime
//   PnL, 30-day ROI, trade count). Anything that's obviously too thin on
//   data or too weak on ROI gets marked "ignore" RIGHT HERE and skips pass 2
//   entirely — we never pay for the expensive calls on a wallet that was
//   never going to pass anyway.
//
//   PASS 2 (expensive, only for pass-1 survivors): fetch the wallet's full
//   pnl-series history, its `behavior` section (recent trade frequency +
//   win rate), and its `activity` section (last-trade timestamp, for the
//   recency gate) — three calls per wallet, run in parallel per wallet.
//
// Both passes use a small "concurrency pool" (see @copybot/shared) so we run
// several wallets at once instead of one at a time, without overwhelming the
// API.
//
// =============================================================================
// DATABASE SAFETY BOUNDARY (see docs/copy-trading/SAFETY.md)
// =============================================================================
// READS FROM:  leaderboard_scan (who are the candidates?), rule_set (what
//              scoring weights/thresholds are currently active?),
//              bot_event_log (READ-ONLY — recent copy-trade count, purely to
//              size the top-N pool; bot.py/db.py remain the only writers).
// WRITES TO:   wallet_profile — but ONLY the scoring-related columns and
//              `status`/`statusReason`/`statusChangedAt`.
// NEVER TOUCHES: wallet_profile.circuitBreakerMuted, muteReason, mutedAt,
//              consecutiveLosses, recentResultsJson — those five columns
//              belong exclusively to bot.py's circuit breaker (see db.py).
//              The one function that writes to wallet_profile
//              (upsertWalletProfile, near the bottom of this file) simply
//              never mentions those column names, so Drizzle can't touch
//              them even by accident.

import { and, eq, gte, inArray, sql } from "drizzle-orm";
import { botEventLog, botRiskState, db, leaderboardScan, ruleSet, walletProfile } from "@copybot/db";
import { mapWithConcurrency } from "@copybot/shared";
import { fetchOnePage } from "./polymarketDataApi";
import { queueApprovalRequest } from "./walletApprovalQueue";

const PASS1_CONCURRENCY = 5; // how many wallets' cheap calls run at once
const PASS2_CONCURRENCY = 5; // how many wallets' expensive calls run at once
const RECENT_TRADES_SAMPLE_SIZE = 50; // matches propose_pool_refill.py's POOL_REFILL_ACTIVITY_SAMPLE_SIZE

// Ethereum addresses are case-INsensitive, but different bullpen commands
// return them in different casing (checksummed mixed-case vs. lowercase).
// scanLeaderboard.ts now normalizes on write, but this script defensively
// normalizes again here too — any leaderboard_scan rows written before that
// fix, or a future data source that isn't as careful, would otherwise
// create duplicate wallet_profile rows for the same real wallet (this
// happened: 8 wallets were duplicated before this fix, verified via
// `GROUP BY LOWER(wallet_address) HAVING COUNT(*) > 1`).
function normalizeAddress(address: string): string {
  return address.toLowerCase();
}

// =============================================================================
// SECTION 1: THE SCORING RULES (weights, thresholds, saturation points)
// =============================================================================
// Every "magic number" the scoring formulas use lives in here — NOT
// scattered through the code as hardcoded constants. Why: this whole object
// gets saved into the `rule_set` database table (see getActiveRuleSet
// below), so a future script (`updateRules.ts`, not built yet) can tune
// these numbers based on real performance data, with every change logged
// and explained. Version 2 (this file) supersedes version 1's lifetime-
// history scoring with the rolling-window/monthly-rebalance approach
// documented at the top of this file — see getActiveRuleSet for how an
// existing v1 database row gets migrated to v2 automatically.

export interface ScoringRules {
  version: number;

  // How much each of the four sub-scores (0..1 each) contributes to the
  // final compositeScore. These four numbers should add up to 1.0.
  weights: {
    roi: number;
    consistency: number;
    winRate: number;
    copyability: number;
  };

  // roi_30d (a fraction, e.g. 0.5 = 50% monthly return) at or above this
  // value maps to a perfect roiScore of 1.0. Below 0 maps to 0.
  roiSaturation: number;

  // Our simplified Sharpe ratio (mean gain per unit of volatility), computed
  // over the rolling window only, at or above this value maps to a perfect
  // consistencyScore of 1.0.
  sharpeSaturation: number;

  // recentWinRate (fraction of rolling-window periods with a positive
  // portfolio-value delta) at or above this value maps to a perfect
  // winRateScore of 1.0. Floored at 0.5 (coin-flip baseline) — see
  // computeWinRateScore.
  winRateSaturation: number;

  // How much damage the one-hit-wonder penalty can do to the final score.
  // 0.8 means: even a MAXIMALLY concentrated wallet (all gains from one
  // spike) only loses 80% of its score, never 100% — a single red flag
  // shouldn't be treated identically to "definitely worthless."
  oneHitWonderPenaltyStrength: number;

  // Lifetime trade count at or above this maps to full (1.0) confidence in
  // this wallet's other scores. Fewer trades linearly discounts everything.
  // Deliberately still LIFETIME (not windowed) — this is a "do we trust this
  // wallet has a real track record at all" floor, not a recency signal.
  sampleConfidenceTradesFloor: number;

  // Absolute floor: below this many lifetime trades, we don't trust this
  // wallet AT ALL, regardless of how good its numbers look. Forces "ignore."
  hardMinTrades: number;

  // Closed-position count at or above this maps to full (1.0) confidence in
  // a PositionTracker-computed realized PnL/win-rate — see
  // computePositionConfidence. Found live (2026-07-27, the yield-farmer-1
  // reconciliation investigation): a wallet's realized-only PnL can differ
  // enormously from bullpen's mark-to-market figure when a lot of its
  // recent activity is still open/unresolved — a wallet with 2 closed
  // positions and 0 open ones would pass a naive closed/(closed+open)
  // ratio at 100%, but 2 closes is not a real sample. Same ramp shape as
  // sampleConfidenceTradesFloor, deliberately a SEPARATE constant: this one
  // is measured in closed POSITIONS (a PositionTracker concept), not raw
  // trades (a bullpen wallet-stats concept) — the two aren't comparable
  // units, sharing one threshold would conflate them.
  closedPositionConfidenceFloor: number;

  // --- Capital multiplier (v7, 2026-07-28) ------------------------------
  // Scales bot.py's compute_trade_size_usd() MIN/MAX_TRADE_USD band
  // per-wallet, based on the RAW (unsaturated) sharpeProxy from
  // analyzePnlSeries — see SeriesAnalysis.sharpeProxy's own doc comment
  // for why the already-saturated consistencyScore can't be reused here
  // (it pins at 1.0 for every "track"-tier wallet, killing exactly the
  // differentiation this needs). Deliberately NOT a replacement for Rule
  // 25's Kelly formula, which needs the market price at TRADE time (only
  // bot.py has that) — this only stretches the RANGE Kelly then operates
  // within. formula: 1 + clamp(sharpeProxy / capitalMultiplierSaturation,
  // 0, 1) * (maxCapitalMultiplier - 1).
  capitalMultiplier: {
    // Deliberately HIGHER than sharpeSaturation (0.15, the track/watch
    // cutoff) — 0.15 already maps every qualifying wallet to a perfect
    // consistencyScore, so reusing it here would give everyone the max
    // multiplier immediately. 0.35 reserves the full multiplier for
    // genuinely exceptional track records, not merely-qualifying ones.
    // Explicitly confirmed with Joey (2026-07-28), not picked unilaterally.
    saturation: number;
    // Multiplier at/above saturation — e.g. 2.0 means a top-tier wallet's
    // sizing band doubles (e.g. $3-$10 -> $6-$20). A conservative starting
    // point, explicitly confirmed with Joey (2026-07-28) rather than
    // picked unilaterally — real capital-at-risk implications, easy to
    // widen later once this is observed live.
    max: number;
  };

  // --- Tiered scoring (2026-07-28) ---------------------------------------
  // Self-throttling re-score cadence, so ONE frequently-run scheduler job
  // (not three separately-scheduled ones) naturally does almost nothing
  // once the candidate pool is "warm" — see the pass-1 due-filter in
  // runPass1 and getActiveTierForWallet. Tier 1 = currently in bot.py's
  // REAL, live config.TRACKED_TRADERS (read from bot_risk_state.
  // tracked_traders, NOT wallet_profile.status='track' — those two are
  // known to drift apart, confirmed live during tonight's reconciliation
  // work). Tier 2 = status='watch'. Tier 3 = everything else still scored.
  // Tier 1's REAL-TIME polling is bot.py's own 30s loop, a completely
  // separate system — this cadence only governs periodic RE-SCORING for
  // scoring-health purposes (catching drift, feeding future pool-refill
  // decisions), not live trade detection.
  tierRescoreIntervalDays: {
    tier1: number;
    tier2: number;
    tier3: number;
  };

  // Pass 1 preliminary score (roiScore x sampleConfidence) must clear this
  // bar to "survive" into the expensive pass 2. Kept low on purpose — pass 1
  // is a coarse filter, not the final word.
  pass1CutoffScore: number;

  // Final compositeScore thresholds for the status decision.
  statusThresholds: {
    track: number;
    watch: number;
  };

  // Parameters for the "copyability" sub-score (see computeCopyabilityScore).
  copyability: {
    floor: number; // trades/day at or below this -> frequency contribution = 0
    minGood: number; // trades/day from here to maxGood -> frequency contribution = 1.0 (the "sweet spot")
    maxGood: number;
    ceiling: number; // trades/day at or above this -> frequency contribution = 0 (too frantic to follow)
    volumePresenceSaturation: number; // volume_30d ($) at or above this -> "still active" contribution = 1.0
  };

  // --- Rolling window / monthly rebalancing (v2) -----------------------------

  // How many days of pnl-series history feed consistency/one-hit-wonder/
  // win-rate. Everything older is excluded entirely — a dormant stretch
  // before this window simply never enters the calculation. See the
  // "ROLLING WINDOW" doc comment at the top of this file.
  rollingWindowDays: number;

  // A wallet with no trade at all within this many days is forced to
  // "ignore" regardless of compositeScore — see finalizeAndWrite. This is
  // what makes "drop whoever went cold this month" automatic.
  maxDaysSinceLastTrade: number;

  // Base size of the "track" pool — see TOP-15 POOL doc comment at the top
  // of this file.
  topNPoolSize: number;

  // How many EXTRA wallets to admit into the pool (on top of topNPoolSize)
  // when recent bot copy-trade volume is thin — see computeDynamicPoolSize.
  topNPoolExpansion: number;

  // If the bot generated fewer than this many actual copy-trades
  // (paper_buy/live_buy events) in the trailing 30 days, the pool expands by
  // topNPoolExpansion. This is a starting guess (roughly "a couple of
  // trades a week"), meant to be tuned once real post-rollout trade volume
  // is observed — not a number derived from data yet.
  minMonthlyTradesForFullPool: number;

  // --- Toxic-flow hard gate (v3) ----------------------------------------------

  // Hard gate: a wallet whose rolling-window consistencyScore falls below
  // this is forced to "ignore" regardless of compositeScore/ROI/winRate —
  // see the "TOXIC-FLOW HARD GATE" doc comment at the top of this file and
  // finalizeAndWrite. Wallets with too little rolling-window data to compute
  // a real consistencyScore are exempted from this specific gate (see
  // SeriesAnalysis.insufficientData) — missing data isn't evidence of toxic
  // flow, it's just unknown, and is handled by the normal composite-score
  // path instead.
  minConsistencyScore: number;

  // --- Liquidity-farming hard gate (v5) ---------------------------------------
  //
  // Motivated directly by a live finding (2026-07-27): 6 of 6 top-composite-
  // score candidates checked by hand across two separate discovery scans
  // showed the same disqualifying pattern -- NOT "being algorithmic" (an
  // algo trader with genuine directional edge is fine to copy), but profit
  // structurally unreplicable via a copy with lag and taker fees: repeated
  // SELLs at the identical near-zero-or-near-one price in the SAME market,
  // minutes apart, sizes varying wildly (e.g. 0.06/78.0/0.08 shares at the
  // exact same $0.007 quote) -- liquidity-rewards farming, not conviction.
  // A separate candidate showed the SAME-second opposite-side pattern
  // across overlapping markets (cross-market arbitrage). Neither toxic-flow
  // (consistencyScore) nor recency catches this: these wallets often show
  // HIGH consistency and recent activity precisely because selling
  // long-shot NO tokens at scale looks smooth and profitable on paper.
  // This is a genuinely different failure mode from toxic flow, not a
  // duplicate of it.
  //
  // v5 CORRECTION (2026-07-27, same day, next scan run): v4 shipped without
  // a buy/sell check and immediately caught a real false positive --
  // "quant-generalist-2" (0xe154165732...), $179K lifetime profit, a
  // near-coin-flip 47.6% win rate (the OPPOSITE of a farmer's near-100%
  // win rate on extreme-price fills), 44 of 47 trades BUY not SELL. Its
  // "7x repeated quote" was 7 buys at the same $0.02 price within a
  // 5-SECOND window, sizes varying (52.8/117.1/132.9/50/50/150/117.1
  // shares) -- sweeping a thin ask side to build a longshot position, not
  // sitting there farming liquidity. The confirmed-bad wallets (Rule 40)
  // were 98-100% SELL on their extreme-price fills; this false positive
  // was ~5% SELL (2 of 41). Buy vs. sell direction is NOT decoration on
  // top of the other two signals -- it's the difference between "farms
  // rebates by selling into extreme prices" and "genuinely bets on
  // longshots by buying them," which is exactly the distinction this whole
  // gate exists to draw (see Rule 40: being algorithmic isn't
  // disqualifying).
  //
  // A sample of this wallet's real recent trades (fetchRecentTrades, a NEW
  // pass-2 call -- the direct Polymarket Data API (polymarketDataApi.ts),
  // NOT bullpen, distinct from the existing `wallet-stats --section
  // activity` which only returns first/last timestamps, not per-trade
  // price/side/slug) is checked for:
  //   - what fraction sit at an extreme price (<0.05 or >0.95) -- betting
  //     near-certain/near-impossible outcomes at scale, consistent with
  //     farming Polymarket's liquidity-rewards program near the edges of
  //     the price range, not genuine directional conviction.
  //   - how many times the single most-repeated (market, price) pair
  //     recurs -- hitting the exact same quote repeatedly is a
  //     liquidity-provision signature; a real conviction bet doesn't need
  //     the same fill 10+ times in one market.
  //   - of THOSE extreme-price trades specifically, what fraction are
  //     SELL side -- farming means repeatedly selling into the extreme
  //     price (collecting the spread/rebate); buying into it repeatedly is
  //     a directional longshot bet, the opposite of what this gate exists
  //     to catch.
  // Gated on ALL THREE conditions together (not any one or two alone): a
  // wallet that's heavily concentrated at extreme prices, AND repeatedly
  // hits the same quote, AND is doing so predominantly by SELLING is the
  // confirmed live pattern; dropping the sell-side requirement is exactly
  // what let the v4 false positive through.
  liquidityFarming: {
    // Minimum trades in the sample before this gate is even evaluated --
    // below this, the sample is too thin to trust any of the three stats
    // (mirrors hardMinTrades' same "don't gate on too little data"
    // reasoning).
    minSampleSize: number;
    maxExtremePricePct: number; // fraction (0..1) of sampled trades at price <0.05 or >0.95
    minRepeatedQuoteCount: number; // times the single most-common (market, price) pair recurs
    // fraction (0..1) of the EXTREME-PRICE trades specifically that must
    // be SELL side before this looks like farming rather than longshot
    // buying -- 0.5 (a plain majority) comfortably separates the
    // confirmed-bad wallets (98-100% sell) from the v4 false positive
    // (~5% sell), so this isn't a knife-edge number.
    minExtremePriceSellPct: number;
  };
}

export const DEFAULT_RULES: ScoringRules = {
  version: 7,
  weights: { roi: 0.3, consistency: 0.2, winRate: 0.3, copyability: 0.2 },
  roiSaturation: 0.5,
  sharpeSaturation: 0.15,
  winRateSaturation: 0.65,
  oneHitWonderPenaltyStrength: 0.8,
  sampleConfidenceTradesFloor: 50,
  hardMinTrades: 5,
  closedPositionConfidenceFloor: 20,
  capitalMultiplier: { saturation: 0.35, max: 2.0 },
  tierRescoreIntervalDays: { tier1: 1, tier2: 1, tier3: 7 },
  pass1CutoffScore: 0.15,
  statusThresholds: { track: 0.55, watch: 0.3 },
  copyability: { floor: 0.2, minGood: 2, maxGood: 10, ceiling: 50, volumePresenceSaturation: 50000 },
  rollingWindowDays: 90,
  maxDaysSinceLastTrade: 30,
  topNPoolSize: 15,
  topNPoolExpansion: 5,
  minMonthlyTradesForFullPool: 10,
  minConsistencyScore: 0.2,
  liquidityFarming: {
    minSampleSize: 20,
    maxExtremePricePct: 0.5,
    minRepeatedQuoteCount: 5,
    minExtremePriceSellPct: 0.5,
  },
};

/**
 * Reads the currently-active scoring rules from the database. If none exist
 * yet (very first time this script has ever run), it creates the current
 * DEFAULT_RULES version and saves it. If an OLDER version is active (e.g.
 * v1's lifetime-history scoring, or v2 before the toxic-flow gate), it's
 * deactivated and the current version becomes active instead — every future
 * run then reuses that same saved version, so results stay comparable
 * across runs, until `updateRules.ts` deliberately creates a new version.
 *
 * INPUT:  none (reads/writes the `rule_set` table)
 * OUTPUT: the active ScoringRules object to use for this run
 */
async function getActiveRuleSet(): Promise<ScoringRules> {
  const rows = await db.select().from(ruleSet).where(eq(ruleSet.isActive, true)).limit(1);
  if (rows.length > 0) {
    const active = JSON.parse(rows[0].thresholdsJson) as ScoringRules;
    if (active.version === DEFAULT_RULES.version) {
      return active;
    }
    console.log(
      `Active rule_set is v${active.version}, but this script now defines v${DEFAULT_RULES.version} ` +
        `(rolling-window scoring + win rate + top-N pool + toxic-flow consistency gate + liquidity-farming ` +
        `gate with a sell-side-majority check + a closedPositionConfidenceFloor field for the not-yet-wired ` +
        `PositionTracker confidence discount + capital_multiplier sizing + tiered-scoring due-filter) — ` +
        `deactivating v${active.version} and activating v${DEFAULT_RULES.version}.`
    );
    await db.update(ruleSet).set({ isActive: false }).where(eq(ruleSet.id, rows[0].id));
  } else {
    console.log("No active rule_set found — bootstrapping.");
  }

  await db.insert(ruleSet).values({
    version: DEFAULT_RULES.version,
    isActive: true,
    thresholdsJson: JSON.stringify(DEFAULT_RULES),
    description:
      "Rolling-window wallet-scoring: 30% ROI (30d), 20% consistency (Sharpe-proxy, 90d rolling window), " +
      "30% win rate (90d rolling window, coin-flip-baselined), 20% copyability (recent 30d trade pace + " +
      "volume presence), with a one-hit-wonder penalty and a trade-count confidence multiplier. A wallet " +
      "with no trade in the last 30 days is force-ignored regardless of score. A wallet whose rolling-window " +
      "consistencyScore is below 0.20 is force-ignored as 'toxic flow - volume farmer', regardless of ROI or " +
      "win rate, unless it has too little windowed data to assess (exempted from that gate, not the same as " +
      "passing it). A wallet whose sampled recent trades are >=50% extreme-price (<0.05 or >0.95) AND whose " +
      "single most-repeated (market, price) pair recurs >=5 times AND whose extreme-price trades are >=50% " +
      "SELL-side is force-ignored as 'liquidity farming / unreplicable edge', regardless of composite score " +
      "— being algorithmic isn't itself disqualifying, profit structurally unreplicable via a copy with " +
      "lag/fees is; the sell-side-majority requirement (added in v5) distinguishes farming (repeatedly " +
      "SELLING into extreme prices) from genuine longshot buying (the opposite side), after v4 caught a " +
      "confirmed false positive lacking it. v6 added closedPositionConfidenceFloor (20) for " +
      "computePositionConfidence — a discount for PositionTracker-computed realized PnL/win-rate based on " +
      "closed-vs-open position ratio AND closed-position sample size, motivated by the yield-farmer-1 " +
      "reconciliation finding (realized-only PnL can differ enormously from bullpen's mark-to-market figure " +
      "when a lot of recent activity is still open) — NOT wired into compositeScore, prepared infrastructure " +
      "only. v7 adds capitalMultiplier (saturation=0.35, max=2.0x) — computeCapitalMultiplier() scales " +
      "bot.py's compute_trade_size_usd() sizing RANGE per-wallet off the raw (unsaturated) Sharpe proxy, " +
      "reserving the full 2x multiplier for genuinely exceptional track records (0.35 is well above the " +
      "0.15 track/watch cutoff, deliberately, so merely-qualifying wallets don't all max out); does NOT " +
      "replace Rule 25's Kelly formula, which needs the live market price at trade time. v7 also adds " +
      "tierRescoreIntervalDays (tier1/tier2=1d, tier3=7d) — a self-throttling due-filter ahead of pass 1 so " +
      "one frequently-scheduled run mostly no-ops once the pool is warm, instead of three separately-" +
      "scheduled jobs. Only the top 15 (or 20 if recent bot trade volume is thin) qualifying wallets become " +
      "'track'; the rest are demoted to 'watch'. Supersedes v1's lifetime-history scoring (under-rated " +
      "dormant-then-hot wallets), v2 (no defense against high-win-rate/low-consistency volume-farmer flow), " +
      "v3 (no defense against liquidity-rewards farming / micro-arbitrage), and v4 (liquidity-farming gate " +
      "had no buy/sell check, so a genuine longshot buyer could be misidentified as a farmer). Seeded by " +
      "scoreWallets.ts.",
  });
  return DEFAULT_RULES;
}

// =============================================================================
// SECTION 2: SMALL MATH HELPERS
// =============================================================================

/** Clamps `value` into the [min, max] range. Used everywhere to keep every
 * sub-score in the 0..1 range we promised the database schema. */
export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

export function mean(values: number[]): number {
  if (values.length === 0) return 0;
  return values.reduce((sum, v) => sum + v, 0) / values.length;
}

/** Sample standard deviation (uses n-1, the standard choice when the values
 * we have are treated as a SAMPLE of the wallet's true behavior, not the
 * entire universe of it). Returns 0 if there aren't at least 2 values. */
export function sampleStdev(values: number[]): number {
  if (values.length < 2) return 0;
  const m = mean(values);
  const variance = values.reduce((sum, v) => sum + (v - m) ** 2, 0) / (values.length - 1);
  return Math.sqrt(variance);
}

/**
 * Trims a pnl-series to only the points within the last `windowDays` days,
 * sorted oldest-first. This is THE mechanism that keeps a wallet's ancient
 * (e.g. dormant-6-months-ago) history out of consistency/one-hit-wonder/
 * win-rate scoring entirely — see the "ROLLING WINDOW" doc comment at the
 * top of this file.
 *
 * `t` is unix SECONDS (verified empirically against a real bullpen
 * pnl-series response — points land roughly hourly, ~3600s apart).
 *
 * Deliberately does NOT include the single point just before the window
 * boundary: including it would create one artificial "delta" spanning the
 * entire excluded gap (e.g. a dormant-to-active transition), which is a
 * window-boundary artifact, not a real within-window trading pattern.
 */
export function trimToRollingWindow(
  series: Array<{ p: number; t: number }>,
  windowDays: number
): Array<{ p: number; t: number }> {
  const cutoffSeconds = Date.now() / 1000 - windowDays * 24 * 60 * 60;
  return series.filter((point) => point.t >= cutoffSeconds).sort((a, b) => a.t - b.t);
}

// =============================================================================
// SECTION 3: FETCHING DATA FROM THE `bullpen` CLI
// =============================================================================
// These five functions are the ONLY places this script talks to the outside
// world. Each one is defensive: if the call fails or times out (which we
// verified DOES happen regularly for some bullpen endpoints — see the plan
// discussion), we log a warning and return null/empty rather than crashing
// the whole script over one bad wallet.

/** Shape of the data bullpen returns for `wallet-stats --section summary`. */
export interface WalletStatsData {
  wallet_address: string;
  lifetime_pnl: number;
  lifetime_volume: number;
  pnl_24h: number;
  pnl_30d: number;
  pnl_7d: number;
  rank: number;
  roi_30d: number;
  total_position_value: number;
  trades_count: number;
  capital_deployed_30d: number;
  join_date: string; // ISO timestamp string, e.g. "2024-08-13T04:23:43.838000Z"
}

/** Shape of the data bullpen returns for `wallet-stats --section flow`. */
export interface TradeFlowData {
  wallet_address: string;
  volume_24h: number;
  volume_30d: number;
  volume_7d: number;
  net_flow_24h: number;
  net_flow_30d: number;
  net_flow_7d: number;
}

/**
 * Shape of the data bullpen returns for `wallet-stats --section behavior`.
 * The `_30d`/`_7d`/`_1d` fields are genuine ROLLING-WINDOW rates computed
 * server-side by bullpen — NOT lifetime averages — which is exactly what
 * fixes copyability's dormant-then-hot underrating (see computeCopyabilityScore).
 * Verified against a live tracked wallet: `avg_trades_per_day_lifetime` was
 * 19.2 for a wallet trading ~30/day for the last 30 days — a 2026-07-17
 * spot-check that's the concrete example this whole rewrite is built around.
 */
export interface BehaviorStatsData {
  wallet_address: string;
  avg_trades_per_day_30d: number;
  avg_trades_per_day_7d: number;
  avg_trades_per_day_1d: number;
  avg_trades_per_day_lifetime: number;
  win_rate_7d: number;
  win_rate_1d: number;
  total_trades: number;
  is_likely_bot: boolean;
  trader_tier: string;
}

/**
 * Shape of the data bullpen returns for `wallet-stats --section activity`.
 * `last_trade_timestamp` (unix seconds) is what drives the "stopped trading
 * in the last 30 days -> force ignore" gate in finalizeAndWrite.
 */
interface ActivityBoundsData {
  wallet_address: string;
  first_trade_at: string;
  first_trade_timestamp: number;
  last_trade_at: string;
  last_trade_timestamp: number;
  total_trades: number;
}

/**
 * Fetches the PnL/ROI/trade-count summary for one wallet.
 * INPUT:  a wallet address (e.g. "0x1234...")
 * OUTPUT: the WalletStatsData object, or null if the call failed
 */
async function fetchWalletStatsSummary(address: string): Promise<WalletStatsData | null> {
  console.warn(`  ${address}: legacy Bullpen summary scorer is disabled; no official equivalent is ready`);
  return null;
}

/**
 * Fetches recent trading volume / net cash flow for one wallet.
 * INPUT:  a wallet address
 * OUTPUT: the TradeFlowData object, or null if the call failed
 */
async function fetchTradeFlow(address: string): Promise<TradeFlowData | null> {
  void address;
  return null;
}

/**
 * Fetches recent (1d/7d/30d) trading behavior — the rolling-window trade
 * frequency and win-rate fields this rewrite depends on.
 * INPUT:  a wallet address
 * OUTPUT: the BehaviorStatsData object, or null if the call failed
 */
async function fetchBehaviorStats(address: string): Promise<BehaviorStatsData | null> {
  void address;
  return null;
}

/**
 * Fetches first/last trade timestamps for one wallet — used only for the
 * "has this wallet gone cold?" recency gate.
 * INPUT:  a wallet address
 * OUTPUT: the ActivityBoundsData object, or null if the call failed
 */
async function fetchActivityBounds(address: string): Promise<ActivityBoundsData | null> {
  void address;
  return null;
}

/**
 * Minimal shape computeLiquidityFarmingSignal actually needs from a trade
 * record — deliberately decoupled from polymarketDataApi.ts's full
 * RawActivityRecord (which structurally satisfies this) so the pure
 * scoring function isn't coupled to that module's exact fetch shape, and
 * so its own unit tests can keep constructing minimal fixtures.
 */
interface RecentTradeRecord {
  price?: number;
  slug?: string;
  side?: string;
}

/**
 * Fetches a sample of this wallet's actual recent trades (price/slug per
 * trade) — the liquidity-farming gate's input. Deliberately a DIFFERENT
 * source from fetchActivityBounds above: `wallet-stats --section activity`
 * only returns first/last timestamps and a count, not per-trade records.
 *
 * Direct Polymarket Data API (2026-07-27), NOT bullpen — mirrors the same
 * migration bot.py's tracking/pricing already made (Rule 14/16). Uses
 * fetchOnePage (not fetchWalletTrades) deliberately: fetchWalletTrades
 * auto-paginates until a SHORT page, so asking it for "50" on a wallet
 * with >=50 trades would keep fetching well past the intended sample size
 * — fetchOnePage(..., offset=0) is a genuine single bounded fetch, exactly
 * matching bullpen's old `--limit 50` semantics.
 *
 * INPUT:  a wallet address
 * OUTPUT: an array of TRADE-type activity records (empty array if the call
 *         failed or the wallet has no trade history)
 */
async function fetchRecentTrades(address: string): Promise<RecentTradeRecord[]> {
  try {
    return await fetchOnePage(address, RECENT_TRADES_SAMPLE_SIZE, 0);
  } catch (err) {
    console.warn(`  fetchOnePage (recent trades) failed for ${address}: ${(err as Error).message}`);
    return [];
  }
}

/**
 * Plain diagnostic stats computed from a real trade sample — the same
 * numbers propose_pool_refill.py's summarize_liquidity_farming_signal
 * surfaces for human review, now used programmatically as a hard gate:
 *   - extremePricePct: fraction of sampled trades at price <0.05 or >0.95
 *     (betting near-certain/near-impossible outcomes at scale, consistent
 *     with farming Polymarket's liquidity-rewards program, not conviction).
 *   - topRepeatedQuoteCount: how many times the single most-common
 *     (market, price) pair recurs in the sample (hitting the exact same
 *     quote repeatedly is a liquidity-provision signature).
 *   - extremePriceSellPct (added v5, 2026-07-27, after a confirmed false
 *     positive — see the "Liquidity-farming hard gate (v5)" doc comment on
 *     ScoringRules): of the extreme-price trades specifically, what
 *     fraction are SELL side. Farming means repeatedly SELLING into an
 *     extreme price to collect the spread/rebate; repeatedly BUYING into
 *     one is a directional longshot bet — the opposite pattern. null when
 *     there are no extreme-price trades to measure a side-split over.
 * INPUT:  an array of TRADE records from fetchRecentTrades
 * OUTPUT: { sampleSize, extremePricePct, topRepeatedQuoteCount,
 *         extremePriceSellPct }, or null if the sample is empty (no trade
 *         history / fetch failed)
 */
export function computeLiquidityFarmingSignal(
  trades: RecentTradeRecord[]
): {
  sampleSize: number;
  extremePricePct: number;
  topRepeatedQuoteCount: number;
  extremePriceSellPct: number | null;
} | null {
  if (trades.length === 0) return null;
  const priced = trades.filter((t) => typeof t.price === "number");
  const extremeTrades = priced.filter((t) => t.price! < 0.05 || t.price! > 0.95);
  const extreme = extremeTrades.length;
  const quoteCounts = new Map<string, number>();
  for (const t of priced) {
    const key = `${t.slug ?? ""}|${t.price!.toFixed(3)}`;
    quoteCounts.set(key, (quoteCounts.get(key) ?? 0) + 1);
  }
  const topRepeatedQuoteCount = quoteCounts.size > 0 ? Math.max(...quoteCounts.values()) : 0;
  const extremeSellCount = extremeTrades.filter((t) => t.side === "SELL").length;
  const extremePriceSellPct = extremeTrades.length > 0 ? extremeSellCount / extremeTrades.length : null;
  return {
    sampleSize: trades.length,
    extremePricePct: priced.length > 0 ? extreme / priced.length : 0,
    topRepeatedQuoteCount,
    extremePriceSellPct,
  };
}

/**
 * Fetches the wallet's full portfolio-value history — a list of
 * {p: value in USD, t: unix timestamp} points, roughly one per hour, going
 * back as far as bullpen has data. THIS is the expensive pass-2-only call;
 * trimToRollingWindow() cuts it down to the recent window before it's used.
 * INPUT:  a wallet address
 * OUTPUT: an array of {p, t} points (empty array if the call failed)
 */
async function fetchPnlSeries(address: string): Promise<Array<{ p: number; t: number }>> {
  void address;
  return [];
}

// =============================================================================
// SECTION 4: THE SCORING FORMULAS
// =============================================================================
// Each function below computes exactly ONE piece of the puzzle. They're kept
// small and separate on purpose, so each one is easy to reason about (and
// easy to unit-test later) on its own.

/**
 * ROI score: how good was this wallet's return over the last 30 days?
 * INPUT:  roi_30d — a fraction from bullpen, e.g. 0.5 means "+50% over 30 days"
 * OUTPUT: a 0..1 score. Anything at/above `roiSaturation` (default 50%)
 *         scores a perfect 1.0 — we don't need to tell a 60% wallet apart
 *         from a 300% one, both are simply "excellent." Negative ROI
 *         clamps down to 0.
 */
export function computeRoiScore(roi30d: number, rules: ScoringRules): number {
  return clamp(roi30d / rules.roiSaturation, 0, 1);
}

/**
 * Sample confidence: how much should we trust ALL of this wallet's other
 * scores, given how many trades we're basing them on?
 * INPUT:  tradesCount — lifetime number of trades this wallet has made
 * OUTPUT: a 0..1 multiplier. A wallet with 6 trades could just be a lucky
 *         coin flip; a wallet with 6,000 trades has a real track record.
 *         This scales linearly from 0 trades (0.0 confidence) up to
 *         `sampleConfidenceTradesFloor` trades (1.0 confidence, the cap).
 *         Deliberately LIFETIME, not windowed — this is "do we trust this
 *         wallet has a real track record," independent of how recently that
 *         record was built.
 */
export function computeSampleConfidence(tradesCount: number, rules: ScoringRules): number {
  return clamp(tradesCount / rules.sampleConfidenceTradesFloor, 0, 1);
}

/**
 * Position confidence: how much should we trust a PositionTracker-computed
 * realized PnL/win-rate for this wallet, given (a) how much of its recent
 * activity has actually closed out, and (b) how many closed positions that
 * actually is? Motivated directly by the yield-farmer-1 reconciliation
 * finding (2026-07-27): its realized-only PnL (-$8,467) diverged wildly
 * from bullpen's mark-to-market pnl_30d (+$449,944) specifically because
 * most of its recent activity was still open, unresolved — a real,
 * structural gap, not a bug (see positionTracker.ts's own doc comment on
 * why open positions are excluded rather than estimated).
 *
 * Two factors, multiplied together — deliberately not ONE ratio trying to
 * do both jobs:
 *   - completeness: closedCount / (closedCount + openCount). High means
 *     "we're seeing the full picture of what happened"; low means a lot
 *     of this wallet's recent activity is still pending, so its realized
 *     PnL is necessarily a partial view of its true recent performance.
 *   - closedSampleConfidence: same clamped ramp shape as
 *     computeSampleConfidence, but over CLOSED POSITIONS (a
 *     PositionTracker concept), not raw trades (a bullpen wallet-stats
 *     concept) — a wallet with 2 closed and 0 open positions would score
 *     100% on completeness alone, but 2 closes isn't a real sample either.
 *
 * INPUT:  closedCount, openCount — from WalletMetrics/PositionTracker;
 *         rules — active ScoringRules
 * OUTPUT: a 0..1 multiplier, or null if there's no position data at all
 *         (closedCount + openCount === 0) — genuinely unknown, not zero
 *         trust, same "missing data isn't evidence" posture as
 *         insufficientConsistencyData elsewhere in this file. A wallet
 *         with activity but zero closes (closedCount === 0, openCount > 0)
 *         is NOT this case — that's a confirmed "no realized track record
 *         yet," which correctly scores 0, not null.
 */
export function computePositionConfidence(closedCount: number, openCount: number, rules: ScoringRules): number | null {
  const total = closedCount + openCount;
  if (total === 0) return null;
  const completeness = closedCount / total;
  const closedSampleConfidence = clamp(closedCount / rules.closedPositionConfidenceFloor, 0, 1);
  return completeness * closedSampleConfidence;
}

/**
 * Capital multiplier (v7, 2026-07-28): how much should this wallet's
 * Half-Kelly sizing RANGE (bot.py's MIN/MAX_TRADE_USD) be stretched,
 * based on risk-adjusted (not raw) performance? See the
 * "Capital multiplier" doc comment on ScoringRules for why Sharpe over
 * ROI (a high-ROI, high-volatility wallet is exactly what shouldn't get
 * sized up — that's the whole point of Kelly-style risk management) and
 * why the RAW sharpeProxy is required here rather than the already-
 * saturated consistencyScore (which pins at 1.0 for every "track"-tier
 * wallet, before this function would ever get a chance to differentiate
 * among them).
 *
 * INPUT:  sharpeProxy — the RAW, unsaturated Sharpe proxy from
 *         analyzePnlSeries (SeriesAnalysis.sharpeProxy), NOT
 *         consistencyScore; rules — active ScoringRules
 * OUTPUT: a multiplier >= 1.0 (never shrinks the base range, only
 *         stretches it) — 1.0 for a wallet with no measurable edge, up to
 *         rules.capitalMultiplier.max for one at/above
 *         rules.capitalMultiplier.saturation. A negative sharpeProxy
 *         (net-losing, or losing steadily) clamps to exactly 1.0, same as
 *         zero — this function only ever REWARDS documented skill, never
 *         penalizes below the base range (that's what compositeScore's
 *         track/watch/ignore decision and the hard gates already do).
 */
export function computeCapitalMultiplier(sharpeProxy: number, rules: ScoringRules): number {
  const saturationFraction = clamp(sharpeProxy / rules.capitalMultiplier.saturation, 0, 1);
  return 1 + saturationFraction * (rules.capitalMultiplier.max - 1);
}

export interface SeriesAnalysis {
  consistencyScore: number;
  oneHitWonderPenalty: number;
  recentWinRate: number;
  // True only when there wasn't enough rolling-window data (<3 points) to
  // compute a real consistencyScore, i.e. the 0 below means "unknown," not
  // "confirmed low." The toxic-flow hard gate (finalizeAndWrite) checks this
  // before treating a low consistencyScore as evidence of volume-farming —
  // see the "TOXIC-FLOW HARD GATE" doc comment at the top of this file.
  insufficientData: boolean;
  // The RAW (unsaturated) Sharpe proxy consistencyScore is derived from —
  // consistencyScore = clamp(sharpeProxy / sharpeSaturation, 0, 1) pins at
  // 1.0 for every wallet whose sharpeProxy clears 0.15, which is exactly
  // right for a track/watch/ignore CUTOFF but useless for differentiating
  // among top performers for capital_multiplier (2026-07-28, rule_set v7)
  // — that needs the real, unclamped number, saturated separately at a
  // much higher bar (capitalMultiplierSaturation) reserved for genuinely
  // exceptional track records. 0 when insufficientData is true, same as
  // the other fields here — not a real reading either way.
  sharpeProxy: number;
}

/**
 * Analyzes a wallet's ROLLING-WINDOW portfolio-value history (already
 * trimmed to the last `rules.rollingWindowDays` days — see
 * trimToRollingWindow) to answer three questions: (1) does this wallet gain
 * steadily, or wildly swing up and down, WITHIN the window? (2) did most of
 * its RECENT profit come from one lucky spike, or is it spread across many
 * periods? (3) in what fraction of recent periods did it actually make
 * money?
 *
 * INPUT:  series — the raw {p: value, t: timestamp} points from pnl-series
 *         (NOT yet trimmed — trimming happens inside this function so every
 *         caller gets the same window logic for free)
 * OUTPUT: { consistencyScore, oneHitWonderPenalty, recentWinRate }, all 0..1
 *         (recentWinRate is a raw fraction, not yet saturation-scored — see
 *         computeWinRateScore for that), plus `insufficientData` (true when
 *         there wasn't enough windowed history to trust these numbers at
 *         all — see the toxic-flow gate carve-out in finalizeAndWrite)
 *
 * HOW IT WORKS:
 *   Step 0 (windowing): trim to the last rollingWindowDays days ONLY. A
 *     wallet dormant 6 months ago and hot for the last 90 days is scored
 *     purely on those last 90 days — the dormant stretch never enters the
 *     calculation at all. This is the single biggest fix in this file; see
 *     the "ROLLING WINDOW" doc comment at the top for the mechanism this
 *     replaces and why it under-scored exactly this wallet shape.
 *
 *   Step 1: turn the raw VALUES into period-over-period CHANGES ("deltas").
 *     e.g. if the wallet's portfolio went $100 -> $110 -> $105, the deltas
 *     are [+10, -5]. We analyze the deltas, not the raw values, because
 *     what we care about is "how did they perform in each period," not
 *     "how big is their account."
 *
 *   Step 2 (consistency): compute a simplified SHARPE RATIO — a standard
 *     quant-finance metric — as (average delta) / (volatility of deltas),
 *     over the windowed deltas only. A wallet that gains a little bit
 *     steadily has a HIGH Sharpe ratio even if its total profit is modest.
 *     A wallet that swings wildly has a LOW Sharpe ratio even if profitable
 *     overall, because the pattern is too noisy to trust going forward.
 *
 *   Step 3 (one-hit-wonder): look ONLY at the positive deltas WITHIN the
 *     window and ask: what fraction of ALL the recent gains came from the
 *     single best period? If one spike is 90% of everything this wallet
 *     made in the last 90 days, that's a red flag even if the wallet is
 *     otherwise "recently active."
 *
 *   Step 4 (recent win rate, new in v2): what fraction of windowed periods
 *     had a positive delta at all? A flat (delta == 0) period counts as
 *     NOT a win — this is meant to answer "how often did they make money,"
 *     not "how often did they not lose."
 *
 *   HONEST LIMITATION: pnl-series gives us roughly hourly snapshots, not
 *   one row per individual trade. So this technically measures "how much
 *   did one concentrated PERIOD dominate the gains" / "what fraction of
 *   PERIODS were winners," which is a good proxy for "one lucky trade" /
 *   "win rate" but isn't a perfectly literal per-trade measurement — a
 *   wallet could make several trades within the same hour. Worth knowing,
 *   not worth over-engineering around today.
 */
export function analyzePnlSeries(series: Array<{ p: number; t: number }>, rules: ScoringRules): SeriesAnalysis {
  const windowed = trimToRollingWindow(series, rules.rollingWindowDays);

  if (windowed.length < 3) {
    // Not enough RECENT history to say anything meaningful about
    // consistency, concentration, or win rate — score neutral-low rather
    // than guessing from noise. recentWinRate defaults to the coin-flip
    // baseline (0.5) rather than 0, since "no data" isn't evidence of a
    // losing wallet. insufficientData=true is what stops the toxic-flow
    // gate from misreading this as a confirmed volume farmer.
    return { consistencyScore: 0, oneHitWonderPenalty: 0, recentWinRate: 0.5, insufficientData: true, sharpeProxy: 0 };
  }

  const deltas: number[] = [];
  for (let i = 1; i < windowed.length; i++) {
    deltas.push(windowed[i].p - windowed[i - 1].p);
  }

  // --- Consistency score (simplified Sharpe ratio, windowed) ---
  const meanDelta = mean(deltas);
  const stdevDelta = sampleStdev(deltas);
  const sharpeProxy = stdevDelta > 0 ? meanDelta / stdevDelta : 0;
  const consistencyScore = clamp(sharpeProxy / rules.sharpeSaturation, 0, 1);

  // --- One-hit-wonder penalty (gain concentration, windowed) ---
  const positiveDeltas = deltas.filter((d) => d > 0);
  const totalGain = positiveDeltas.reduce((sum, d) => sum + d, 0);
  const maxSingleGain = positiveDeltas.length > 0 ? Math.max(...positiveDeltas) : 0;
  const oneHitWonderPenalty = totalGain > 0 ? maxSingleGain / totalGain : 0;

  // --- Recent win rate (windowed, new in v2) ---
  const winningPeriods = positiveDeltas.length;
  const recentWinRate = deltas.length > 0 ? winningPeriods / deltas.length : 0.5;

  return { consistencyScore, oneHitWonderPenalty, recentWinRate, insufficientData: false, sharpeProxy };
}

/**
 * Win-rate score: turns the raw recentWinRate fraction (from
 * analyzePnlSeries) into a 0..1 score.
 * INPUT:  recentWinRate — fraction of rolling-window periods with a
 *         positive delta (0.5 = won exactly half the time)
 * OUTPUT: a 0..1 score, FLOORED AT THE COIN-FLIP BASELINE: a wallet winning
 *         exactly 50% of periods scores 0, not 0.5 — winning at a coin-flip
 *         rate isn't a signal worth crediting for a strategy that's
 *         specifically trying to identify skill. Ramps to a perfect 1.0 at
 *         `winRateSaturation` (default 65%) — consistently beating a coin
 *         flip by that margin in prediction markets is already excellent,
 *         no need to distinguish 65% from 90%.
 */
export function computeWinRateScore(recentWinRate: number, rules: ScoringRules): number {
  return clamp((recentWinRate - 0.5) / (rules.winRateSaturation - 0.5), 0, 1);
}

/**
 * Copyability score: could our bot actually, practically follow this
 * wallet's trades RIGHT NOW?
 *
 * INPUT:  behaviorStats  — bullpen's `behavior` section data, or null if
 *                          that call failed for this wallet
 *         fallbackTradesCount / fallbackJoinDateIso — lifetime trade count
 *                          and join date, used ONLY as a degraded fallback
 *                          when behaviorStats is unavailable (see below)
 *         volume30d      — dollar volume traded in the last 30 days
 * OUTPUT: a 0..1 score, blending two things:
 *   (a) "frequency fit" — is this wallet trading at a pace we could
 *       realistically keep up with RIGHT NOW? Too RARE means too little
 *       signal to build a read on them. Too FRANTIC (dozens of trades a
 *       day, probably a bot) means every individual trade is noise we
 *       can't meaningfully copy one at a time on our 30-second poll loop.
 *       The best copyability is a comfortable middle ground.
 *   (b) "volume presence" — is this wallet still actually active with real
 *       money recently? A wallet that made a killing 8 months ago and has
 *       gone quiet since is far less useful than one still trading size
 *       right now.
 *
 * v2 CHANGE: frequency fit now uses bullpen's `avg_trades_per_day_30d` — a
 * genuine 30-day rolling rate — instead of `lifetime trade count / days
 * since first trade`. The old lifetime-average calculation is exactly what
 * under-rated a wallet dormant for 6 months and hyperactive for the last 90
 * days (a 2-year-old account with 90 days of real activity landed near the
 * "too rare" floor even while trading multiple times daily). It's kept ONLY
 * as a fallback for when the `behavior` bullpen call itself fails — that's
 * an availability concern, not a deliberate scoring choice.
 */
export function computeCopyabilityScore(
  behaviorStats: BehaviorStatsData | null,
  fallbackTradesCount: number,
  fallbackJoinDateIso: string,
  volume30d: number,
  rules: ScoringRules
): number {
  const { floor, minGood, maxGood, ceiling, volumePresenceSaturation } = rules.copyability;

  let tradesPerDay: number;
  if (behaviorStats && Number.isFinite(behaviorStats.avg_trades_per_day_30d)) {
    tradesPerDay = behaviorStats.avg_trades_per_day_30d;
  } else {
    // Degraded fallback only — see the v2 CHANGE note above.
    const joinDateMs = new Date(fallbackJoinDateIso).getTime();
    const daysActive = Math.max(1, (Date.now() - joinDateMs) / (1000 * 60 * 60 * 24));
    tradesPerDay = fallbackTradesCount / daysActive;
  }

  // "Frequency fit" is shaped like a trapezoid, not a straight line:
  //   tradesPerDay <= floor or >= ceiling  -> 0 (too rare, or too frantic)
  //   minGood <= tradesPerDay <= maxGood   -> 1 (the sweet spot)
  //   in between                            -> ramps linearly
  let frequencyFit: number;
  if (tradesPerDay <= floor || tradesPerDay >= ceiling) {
    frequencyFit = 0;
  } else if (tradesPerDay >= minGood && tradesPerDay <= maxGood) {
    frequencyFit = 1;
  } else if (tradesPerDay < minGood) {
    frequencyFit = (tradesPerDay - floor) / (minGood - floor);
  } else {
    frequencyFit = (ceiling - tradesPerDay) / (ceiling - maxGood);
  }

  const volumePresence = clamp(volume30d / volumePresenceSaturation, 0, 1);

  return 0.6 * frequencyFit + 0.4 * volumePresence;
}

/**
 * Combines all the sub-scores into the single number that decides a
 * wallet's fate.
 * INPUT:  the four positive sub-scores (roi, consistency, winRate,
 *         copyability) plus oneHitWonderPenalty and sampleConfidence
 * OUTPUT: a single 0..1 compositeScore
 *
 * ORDER OF OPERATIONS (each step matters):
 *   1. Blend the four positive sub-scores using the weights from rule_set.
 *   2. Apply the one-hit-wonder penalty as a PERCENTAGE REDUCTION (not a
 *      flat subtraction) — a wallet that was already scoring low doesn't
 *      get pushed into negative territory, it just loses a share of
 *      whatever score it had.
 *   3. Multiply by sampleConfidence LAST, so a low-trade-count wallet has
 *      its entire final score pulled toward zero, no matter how good its
 *      individual numbers looked.
 */
export function computeCompositeScore(
  roiScore: number,
  consistencyScore: number,
  winRateScore: number,
  copyabilityScore: number,
  oneHitWonderPenalty: number,
  sampleConfidence: number,
  rules: ScoringRules
): number {
  const { roi, consistency, winRate, copyability } = rules.weights;
  const blended =
    roi * roiScore + consistency * consistencyScore + winRate * winRateScore + copyability * copyabilityScore;
  const afterPenalty = blended * (1 - rules.oneHitWonderPenaltyStrength * oneHitWonderPenalty);
  return afterPenalty * sampleConfidence;
}

/**
 * Turns a compositeScore into the RAW track/watch/ignore decision (before
 * the recency gate and top-N pool cap are applied in finalizeAndWrite),
 * along with a human-readable reason (this reason gets saved to the
 * database so you can always see WHY a wallet ended up where it did).
 */
export function decideStatus(
  compositeScore: number,
  tradesCount: number,
  rules: ScoringRules
): { status: "track" | "watch" | "ignore"; reason: string } {
  if (tradesCount < rules.hardMinTrades) {
    return {
      status: "ignore",
      reason: `only ${tradesCount} lifetime trades, below hard minimum of ${rules.hardMinTrades}`,
    };
  }
  if (compositeScore >= rules.statusThresholds.track) {
    return {
      status: "track",
      reason: `composite score ${compositeScore.toFixed(3)} >= track threshold ${rules.statusThresholds.track}`,
    };
  }
  if (compositeScore >= rules.statusThresholds.watch) {
    return {
      status: "watch",
      reason: `composite score ${compositeScore.toFixed(3)} >= watch threshold ${rules.statusThresholds.watch}, below track threshold`,
    };
  }
  return {
    status: "ignore",
    reason: `composite score ${compositeScore.toFixed(3)} below watch threshold ${rules.statusThresholds.watch}`,
  };
}

/**
 * Telegram approval workflow (2026-08-01): decides whether a wallet's raw
 * recommendation should be redirected into the Telegram approval queue.
 * Promotions AND demotions of an existing approved tier require approval;
 * a brand-new non-track research candidate simply starts inert at watch.
 * Extracted as its own pure
 * function purely for direct unit-testability, same "extract the pure
 * decision" pattern as decideStatus/checkToxicFlowGate/
 * computeDemotedAddresses above.
 */
export function shouldRedirectToApprovalQueue(decidedStatus: string, priorStatus: string | null): boolean {
  if (priorStatus === decidedStatus) return false;
  if (decidedStatus === "track") return true;
  return priorStatus === "track" || priorStatus === "bench";
}

// =============================================================================
// SECTION 5: WRITING RESULTS TO THE DATABASE (the safety-boundary function)
// =============================================================================

interface UpsertArgs {
  walletAddress: string;
  displayName: string | null;
  walletStats: WalletStatsData;
  tradeFlow: TradeFlowData | null;
  roiScore: number;
  consistencyScore: number | null;
  copyabilityScore: number | null;
  oneHitWonderPenalty: number | null;
  recentWinRate: number | null;
  compositeScore: number;
  status: string;
  statusReason: string;
  scoreBreakdown: Record<string, unknown>;
  // null for a pass-1 rejection — capitalMultiplier is only ever computed
  // in pass 2 (needs the pnl-series-derived sharpeProxy). bot.py's own
  // reader treats a NULL/missing value as 1.0 (no adjustment), never as 0.
  capitalMultiplier: number | null;
  // The live tracked-wallet set (getTrackedWalletAddresses), threaded
  // through so tier/nextRescoreDueAt can be derived right here — see the
  // "Tiered scoring" doc comment on ScoringRules. Passed in rather than
  // fetched inside this function so every write within one scan run uses
  // the SAME snapshot, not a fresh (and possibly inconsistent) read per row.
  trackedAddresses: Set<string>;
  rules: ScoringRules;
}

/**
 * The ONLY function in this whole file that writes to wallet_profile.
 * Keeping all writes funneled through one function makes the safety
 * boundary easy to verify at a glance: just check the fields listed below,
 * and nowhere else.
 *
 * INPUT:  everything we computed for one wallet (see UpsertArgs)
 * OUTPUT: none — writes one row into wallet_profile (creating it if this
 *         wallet has never been scored before, updating it otherwise)
 *
 * SAFETY BOUNDARY: notice that circuitBreakerMuted, muteReason, mutedAt,
 * consecutiveLosses, and recentResultsJson are NEVER mentioned anywhere in
 * this function. Those columns belong exclusively to bot.py's circuit
 * breaker (see db.py). Because Drizzle's onConflictDoUpdate only touches
 * the columns you explicitly list in `set`, simply never listing those five
 * columns here means this script can never accidentally clobber a mute
 * bot.py just applied.
 */
async function upsertWalletProfile(args: UpsertArgs): Promise<void> {
  const now = new Date();
  const tier = deriveTier(args.walletAddress, "watch", args.trackedAddresses);
  const nextRescoreDueAt = computeNextRescoreDueAt(tier, args.rules, now);

  const scoredValues = {
    // Normalized again here, defensively, right at the write boundary — see
    // the normalizeAddress comment near the top of this file for why.
    walletAddress: normalizeAddress(args.walletAddress),
    recommendedStatus: args.status,
    recommendationReason: args.statusReason,
    recommendationSource: "legacy_scorer_disabled",
    recommendationVersion: "disabled-v1",
    recommendationAt: now,
    derivedMetricsSource: "legacy_unverified",
    derivedMetricsVersion: "disabled-v1",
    derivedMetricsReady: false,
    volume30d: args.tradeFlow?.volume_30d ?? null,
    pnl7d: args.walletStats.pnl_7d,
    pnl30d: args.walletStats.pnl_30d,
    pnlAllTime: args.walletStats.lifetime_pnl,
    tradeCountAllTime: args.walletStats.trades_count,
    // Raw rolling-window win rate (0..1 fraction, not the saturation-curved
    // sub-score — that lives in scoreBreakdownJson only, alongside the other
    // three sub-scores' computed values, since this column predates v2 and
    // was already reserved for a raw rate, not a 0..1 "score").
    winRate: args.recentWinRate,
    roiScore: args.roiScore,
    consistencyScore: args.consistencyScore,
    copyabilityScore: args.copyabilityScore,
    oneHitWonderPenalty: args.oneHitWonderPenalty,
    compositeScore: args.compositeScore,
    capitalMultiplier: args.capitalMultiplier,
    nextRescoreDueAt,
    scoreBreakdownJson: JSON.stringify(args.scoreBreakdown),
    lastScoredAt: now,
    updatedAt: now,
  };

  await db
    .insert(walletProfile)
    .values({
      ...scoredValues,
      // New research candidates start inert. Only an approved resolver may
      // move this field away from watch.
      status: "watch",
      statusReason: "research candidate; no roster approval",
      statusChangedAt: now,
      // On a brand-new wallet we've never seen before, use whatever
      // display name we found (may be null — that's fine).
      nickname: args.displayName,
    })
    .onConflictDoUpdate({
      target: walletProfile.walletAddress,
      set: {
        ...scoredValues,
        // On an UPDATE, only overwrite the nickname if we found a new one
        // this run. Otherwise keep whatever nickname the row already had —
        // this is a SQL COALESCE: "use the first of these that isn't null."
        nickname: args.displayName ?? sql`${walletProfile.nickname}`,
      },
    });
}

// =============================================================================
// SECTION 6: THE TWO PASSES + FINAL RANKING
// =============================================================================

export type WalletTier = "tier1" | "tier2" | "tier3";

/**
 * Fetches the LIVE, ground-truth set of wallets bot.py is actually
 * copying right now — bot_risk_state.tracked_traders, the same source
 * reconcilePositionTracker.ts uses and for the same reason: it's published
 * fresh at every bot.py startup, unlike wallet_profile.status='track'
 * (this scorer's own RECOMMENDATION, confirmed live to drift from what
 * bot.py actually runs — see RISK_MANAGEMENT.md's reconciliation-script
 * writeup). Returns an empty Set (not an error) if bot.py has never
 * published this key — every wallet then correctly falls through to
 * tier2/tier3 via deriveTier rather than crashing the whole scan over a
 * bot.py that simply hasn't started yet.
 */
async function getTrackedWalletAddresses(): Promise<Set<string>> {
  const rows = await db.select().from(botRiskState).where(eq(botRiskState.key, "tracked_traders")).limit(1);
  if (rows.length === 0) return new Set();
  const tracked = JSON.parse(rows[0].valueJson) as Record<string, string>; // {address_lower: nickname}
  return new Set(Object.keys(tracked).map(normalizeAddress));
}

/**
 * Tier 1 = currently in bot.py's REAL tracked-wallet set (see
 * getTrackedWalletAddresses — deliberately NOT wallet_profile.status,
 * which is this scorer's recommendation, not bot.py's ground truth).
 * Tier 2 = this scorer's own 'watch' recommendation. Tier 3 = everything
 * else still being scored. A wallet manually kept in TRACKED_TRADERS
 * despite a poor score is still Tier 1 — tier reflects what's actually
 * being copied, not whether the scorer currently approves of it.
 */
export function deriveTier(address: string, status: string, trackedAddresses: Set<string>): WalletTier {
  if (trackedAddresses.has(normalizeAddress(address))) return "tier1";
  if (status === "watch") return "tier2";
  return "tier3";
}

/**
 * When this wallet is next due for re-scoring, per its tier's cadence
 * (rules.tierRescoreIntervalDays) — see the "Tiered scoring" doc comment
 * on ScoringRules for the self-throttling design this feeds.
 * INPUT:  tier — from deriveTier; rules — active ScoringRules; now —
 *         injected for testability, defaults to the real current time
 * OUTPUT: a Date this many days in the future
 */
export function computeNextRescoreDueAt(tier: WalletTier, rules: ScoringRules, now: Date = new Date()): Date {
  const days =
    tier === "tier1"
      ? rules.tierRescoreIntervalDays.tier1
      : tier === "tier2"
        ? rules.tierRescoreIntervalDays.tier2
        : rules.tierRescoreIntervalDays.tier3;
  return new Date(now.getTime() + days * 86400 * 1000);
}

/**
 * Reads every distinct wallet address we've ever seen across all
 * leaderboard scans, along with the best display name we have for each
 * (handy for a human-readable nickname later).
 * INPUT:  none (reads the `leaderboard_scan` table)
 * OUTPUT: a Map of walletAddress -> displayName (or null if none found).
 *         We do the "distinct" step in plain JavaScript rather than a SQL
 *         GROUP BY — our scans are only a few hundred rows today, so a
 *         simple in-memory pass is simpler to read and plenty fast.
 */
async function getCandidateWallets(): Promise<Map<string, string | null>> {
  const rows = await db
    .select({ walletAddress: leaderboardScan.walletAddress, displayName: leaderboardScan.displayName })
    .from(leaderboardScan);

  const candidates = new Map<string, string | null>();
  for (const row of rows) {
    // Normalize BEFORE using the address as a Map key — this is what makes
    // "0xABC..." and "0xabc..." collapse into one candidate instead of two.
    const address = normalizeAddress(row.walletAddress);
    const existing = candidates.get(address);
    if (existing === undefined || (!existing && row.displayName)) {
      candidates.set(address, row.displayName ?? existing ?? null);
    }
  }
  return candidates;
}

/**
 * The tiered-scoring self-throttle: drops any candidate whose
 * wallet_profile.nextRescoreDueAt is still in the future, so a frequently-
 * scheduled run mostly no-ops once the pool is "warm" instead of re-paying
 * for a full pass-1/pass-2 cycle on wallets that were just scored
 * yesterday. A candidate with no wallet_profile row yet (never scored) or
 * a NULL nextRescoreDueAt is always due — new candidates are never
 * starved by this filter.
 * INPUT:  candidates — the Map from getCandidateWallets()
 * OUTPUT: the subset still due for re-scoring right now
 */
async function filterDueForRescore(candidates: Map<string, string | null>): Promise<Map<string, string | null>> {
  const addresses = [...candidates.keys()];
  if (addresses.length === 0) return candidates;

  const now = new Date();
  const rows = await db
    .select({ walletAddress: walletProfile.walletAddress, nextRescoreDueAt: walletProfile.nextRescoreDueAt })
    .from(walletProfile)
    .where(inArray(walletProfile.walletAddress, addresses));
  // Only candidates that already HAVE a row can possibly be "not due yet"
  // — anything absent from this map was never scored, and stays due by
  // default (handled by the `dueMap.get(address)` fallthrough below).
  const dueMap = new Map(rows.map((r) => [normalizeAddress(r.walletAddress), r.nextRescoreDueAt]));

  const due = new Map<string, string | null>();
  for (const [address, displayName] of candidates) {
    const nextDue = dueMap.get(address);
    if (nextDue === undefined || nextDue === null || nextDue <= now) {
      due.set(address, displayName);
    }
  }
  return due;
}

interface Pass1Survivor {
  address: string;
  displayName: string | null;
  walletStats: WalletStatsData;
  tradeFlow: TradeFlowData | null;
  roiScore: number;
  sampleConfidence: number;
}

/**
 * PASS 1 — the cheap screen. Fetches summary + flow for every candidate
 * (bounded concurrency), computes a preliminary score, and immediately
 * writes an "ignore" verdict (with a clear reason) for anything that
 * doesn't clear the bar — without ever paying for the expensive pass-2
 * calls on wallets that were never going to pass anyway.
 *
 * INPUT:  candidates — the Map from getCandidateWallets(); rules — active ScoringRules
 * OUTPUT: the list of wallets that survived, ready for pass 2
 */
async function runPass1(
  candidates: Map<string, string | null>,
  rules: ScoringRules,
  trackedAddresses: Set<string>
): Promise<{ survivors: Pass1Survivor[]; rejected: number }> {
  const addresses = [...candidates.keys()];
  console.log(
    `Pass 1 (cheap screen): fetching wallet-stats summary+flow for ${addresses.length} wallets, ` +
      `${PASS1_CONCURRENCY} at a time...`
  );

  const fetched = await mapWithConcurrency(addresses, PASS1_CONCURRENCY, async (address) => {
    const [walletStats, tradeFlow] = await Promise.all([fetchWalletStatsSummary(address), fetchTradeFlow(address)]);
    return { address, walletStats, tradeFlow };
  });

  const survivors: Pass1Survivor[] = [];
  let rejected = 0;

  for (const { address, walletStats, tradeFlow } of fetched) {
    if (!walletStats) {
      // Couldn't even get the basic numbers for this wallet — skip it
      // rather than score off missing data. Doesn't write anything to the
      // database; a future run may succeed once the API is healthy again.
      rejected++;
      continue;
    }

    const roiScore = computeRoiScore(walletStats.roi_30d, rules);
    const sampleConfidence = computeSampleConfidence(walletStats.trades_count, rules);
    const prelimScore = roiScore * sampleConfidence;

    const failsHardFloor = walletStats.trades_count < rules.hardMinTrades;
    const failsPrelimBar = prelimScore < rules.pass1CutoffScore;

    if (failsHardFloor || failsPrelimBar) {
      await upsertWalletProfile({
        walletAddress: address,
        displayName: candidates.get(address) ?? null,
        walletStats,
        tradeFlow,
        roiScore,
        consistencyScore: null, // never computed — this wallet never reached pass 2
        copyabilityScore: null,
        oneHitWonderPenalty: null,
        recentWinRate: null,
        compositeScore: prelimScore,
        status: "ignore",
        statusReason: failsHardFloor
          ? `only ${walletStats.trades_count} lifetime trades, below hard minimum of ${rules.hardMinTrades}`
          : `failed pass-1 screen: roiScore=${roiScore.toFixed(3)} x sampleConfidence=${sampleConfidence.toFixed(3)} ` +
            `= ${prelimScore.toFixed(3)}, below cutoff ${rules.pass1CutoffScore}`,
        scoreBreakdown: { pass: 1, roiScore, sampleConfidence, prelimScore },
        capitalMultiplier: null, // never computed — this wallet never reached pass 2
        trackedAddresses,
        rules,
      });
      rejected++;
      continue;
    }

    survivors.push({ address, displayName: candidates.get(address) ?? null, walletStats, tradeFlow, roiScore, sampleConfidence });
  }

  return { survivors, rejected };
}

export interface Pass2Result {
  address: string;
  displayName: string | null;
  walletStats: WalletStatsData;
  tradeFlow: TradeFlowData | null;
  roiScore: number;
  consistencyScore: number;
  winRateScore: number;
  recentWinRate: number;
  copyabilityScore: number;
  oneHitWonderPenalty: number;
  compositeScore: number;
  daysSinceLastTrade: number | null;
  // See SeriesAnalysis.insufficientData — true means consistencyScore is
  // "unknown," not "confirmed low," and this wallet is exempt from the
  // toxic-flow hard gate in finalizeAndWrite.
  insufficientConsistencyData: boolean;
  // From computeLiquidityFarmingSignal — null if the trade-sample fetch
  // failed or the wallet has no trade history (exempt from the liquidity-
  // farming gate in finalizeAndWrite, same "missing data isn't evidence"
  // reasoning as insufficientConsistencyData above).
  liquidityFarmingSignal: {
    sampleSize: number;
    extremePricePct: number;
    topRepeatedQuoteCount: number;
    extremePriceSellPct: number | null;
  } | null;
  // From computeCapitalMultiplier — bot.py's compute_trade_size_usd() half-
  // Kelly sizing RANGE multiplier for this wallet (v7). >= 1.0 always; 1.0
  // for no measurable edge, up to capitalMultiplier.max for an exceptional
  // one. See ScoringRules.capitalMultiplier's own doc comment.
  capitalMultiplier: number;
  scoreBreakdown: Record<string, unknown>;
}

/**
 * PASS 2 — the deep-dive. Only runs on wallets that survived pass 1.
 * Fetches each survivor's full pnl-series history, its `behavior` section
 * (recent trade frequency + win rate), and its `activity` section (last
 * trade timestamp), in parallel per wallet. Computes every sub-score but
 * does NOT decide final status or write to the database — that's deferred
 * to finalizeAndWrite so the recency gate and top-N pool ranking can see
 * every survivor's score at once before any status is final.
 *
 * INPUT:  survivors — the list from runPass1(); rules — active ScoringRules
 * OUTPUT: one Pass2Result per survivor, in the same order
 */
async function runPass2(survivors: Pass1Survivor[], rules: ScoringRules): Promise<Pass2Result[]> {
  console.log(
    `Pass 2 (deep dive): fetching pnl-series + behavior + activity + recent trades for ${survivors.length} ` +
      `wallets that passed pass 1, ${PASS2_CONCURRENCY} at a time...`
  );

  return mapWithConcurrency(survivors, PASS2_CONCURRENCY, async (candidate) => {
    const [series, behaviorStats, activityBounds, recentTrades] = await Promise.all([
      fetchPnlSeries(candidate.address),
      fetchBehaviorStats(candidate.address),
      fetchActivityBounds(candidate.address),
      fetchRecentTrades(candidate.address),
    ]);
    const liquidityFarmingSignal = computeLiquidityFarmingSignal(recentTrades);

    const { consistencyScore, oneHitWonderPenalty, recentWinRate, insufficientData, sharpeProxy } = analyzePnlSeries(
      series,
      rules
    );
    const capitalMultiplier = computeCapitalMultiplier(sharpeProxy, rules);
    const winRateScore = computeWinRateScore(recentWinRate, rules);
    const copyabilityScore = computeCopyabilityScore(
      behaviorStats,
      candidate.walletStats.trades_count,
      candidate.walletStats.join_date,
      candidate.tradeFlow?.volume_30d ?? 0,
      rules
    );
    const compositeScore = computeCompositeScore(
      candidate.roiScore,
      consistencyScore,
      winRateScore,
      copyabilityScore,
      oneHitWonderPenalty,
      candidate.sampleConfidence,
      rules
    );

    const daysSinceLastTrade = activityBounds
      ? (Date.now() / 1000 - activityBounds.last_trade_timestamp) / 86400
      : null;

    return {
      address: candidate.address,
      displayName: candidate.displayName,
      walletStats: candidate.walletStats,
      tradeFlow: candidate.tradeFlow,
      roiScore: candidate.roiScore,
      consistencyScore,
      winRateScore,
      recentWinRate,
      copyabilityScore,
      oneHitWonderPenalty,
      compositeScore,
      daysSinceLastTrade,
      insufficientConsistencyData: insufficientData,
      liquidityFarmingSignal,
      capitalMultiplier,
      scoreBreakdown: {
        pass: 2,
        roiScore: candidate.roiScore,
        consistencyScore,
        winRateScore,
        recentWinRate,
        copyabilityScore,
        oneHitWonderPenalty,
        sampleConfidence: candidate.sampleConfidence,
        compositeScore,
        rollingWindowDays: rules.rollingWindowDays,
        pnlSeriesPointsTotal: series.length,
        insufficientConsistencyData: insufficientData,
        behaviorWinRate7d: behaviorStats?.win_rate_7d ?? null,
        behaviorTradesPerDay30d: behaviorStats?.avg_trades_per_day_30d ?? null,
        daysSinceLastTrade,
        liquidityFarmingSignal,
        sharpeProxy,
        capitalMultiplier,
      },
    };
  });
}

/**
 * Counts how many actual copy-trades (paper_buy/live_buy) bot.py has
 * generated in the trailing 30 days, and uses that to decide whether the
 * top-N pool should expand — see the "TOP-15 POOL" doc comment at the top
 * of this file.
 *
 * INPUT:  rules — active ScoringRules
 * OUTPUT: { poolSize, recentCopyTradeCount }
 */
async function computeDynamicPoolSize(rules: ScoringRules): Promise<{ poolSize: number; recentCopyTradeCount: number }> {
  const cutoff = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000);
  const rows = await db
    .select({ count: sql<number>`count(*)` })
    .from(botEventLog)
    .where(and(inArray(botEventLog.eventType, ["paper_buy", "live_buy"]), gte(botEventLog.timestamp, cutoff)));

  const recentCopyTradeCount = Number(rows[0]?.count ?? 0);
  const poolSize =
    recentCopyTradeCount < rules.minMonthlyTradesForFullPool
      ? rules.topNPoolSize + rules.topNPoolExpansion
      : rules.topNPoolSize;

  return { poolSize, recentCopyTradeCount };
}

/**
 * Toxic-flow hard gate (pure decision logic — see the "TOXIC-FLOW HARD
 * GATE" doc comment at the top of this file). Extracted out of
 * finalizeAndWrite as its own function specifically so it's unit-testable
 * without mocking the database — this is real-money-adjacent decision
 * logic and deserves direct test coverage, not just an eyeballed
 * production run.
 *
 * INPUT:  r — a Pass2Result; rules — active ScoringRules
 * OUTPUT: the ignore verdict + reason if gated, or null if this wallet
 *         passes through to normal compositeScore-based scoring. Wallets
 *         with insufficientConsistencyData=true (too little rolling-window
 *         data to compute a real consistencyScore) always pass through —
 *         see SeriesAnalysis.insufficientData for why "unknown" must never
 *         be treated as "confirmed toxic."
 */
export function checkToxicFlowGate(
  r: Pass2Result,
  rules: ScoringRules
): { status: "ignore"; reason: string } | null {
  if (r.insufficientConsistencyData || r.consistencyScore >= rules.minConsistencyScore) {
    return null;
  }
  return {
    status: "ignore",
    reason:
      `toxic flow - volume farmer (low consistency): consistencyScore=${r.consistencyScore.toFixed(3)} ` +
      `< ${rules.minConsistencyScore} threshold (composite score was ${r.compositeScore.toFixed(3)}, ` +
      `roiScore=${r.roiScore.toFixed(3)}, winRateScore=${r.winRateScore.toFixed(3)} — ignored regardless ` +
      `of either)`,
  };
}

/**
 * Recency hard gate (pure decision logic — see finalizeAndWrite's ORDER OF
 * OPERATIONS doc comment). Extracted for the same testability reason as
 * checkToxicFlowGate.
 *
 * INPUT:  r — a Pass2Result; rules — active ScoringRules
 * OUTPUT: the ignore verdict + reason if gated, or null if this wallet
 *         passes through. daysSinceLastTrade === null (the bullpen
 *         `activity` call failed for this wallet) always passes through —
 *         this gate fails OPEN on missing data, it never gates on it.
 */
export function checkRecencyGate(r: Pass2Result, rules: ScoringRules): { status: "ignore"; reason: string } | null {
  if (r.daysSinceLastTrade === null || r.daysSinceLastTrade <= rules.maxDaysSinceLastTrade) {
    return null;
  }
  return {
    status: "ignore",
    reason:
      `inactive — no trade in ${Math.floor(r.daysSinceLastTrade)} day(s), ` +
      `exceeds the ${rules.maxDaysSinceLastTrade}-day recency limit (composite score was ` +
      `${r.compositeScore.toFixed(3)}, ignored regardless of score)`,
  };
}

/**
 * Liquidity-farming hard gate (v5) (pure decision logic — see the
 * "Liquidity-farming hard gate (v5)" doc comment on ScoringRules). Extracted
 * for the same testability reason as checkToxicFlowGate/checkRecencyGate.
 *
 * Gated on ALL THREE conditions together, not any one or two alone — a
 * wallet heavily concentrated at extreme prices, AND repeatedly hitting
 * the same quote, AND doing so predominantly by SELLING is the confirmed
 * live pattern (Asperatus/pizzabillgates/etc., 98-100% sell on their
 * extreme-price fills). v4 gated on the first two alone and caught a real
 * false positive — "quant-generalist-2," 44 of 47 trades BUY, a
 * near-coin-flip win rate, $179K real lifetime profit — because
 * legitimately buying several DIFFERENT longshots repeatedly isn't
 * farming any single one, and the extreme-price-% / repeated-quote-count
 * signals alone can't tell that apart from actual farming.
 *
 * INPUT:  r — a Pass2Result; rules — active ScoringRules
 * OUTPUT: the ignore verdict + reason if gated, or null if this wallet
 *         passes through. liquidityFarmingSignal === null (the activity
 *         fetch failed or the wallet has no trade history), a sample
 *         below rules.liquidityFarming.minSampleSize, or a null
 *         extremePriceSellPct (no extreme-price trades to measure a
 *         side-split over — shouldn't happen once extremeGated is true,
 *         but checked explicitly rather than assumed) always passes
 *         through — this gate fails OPEN on missing/thin data, same
 *         "unknown isn't confirmed" reasoning as the toxic-flow gate's
 *         insufficientData exemption.
 */
export function checkLiquidityFarmingGate(
  r: Pass2Result,
  rules: ScoringRules
): { status: "ignore"; reason: string } | null {
  const signal = r.liquidityFarmingSignal;
  if (!signal || signal.sampleSize < rules.liquidityFarming.minSampleSize) {
    return null;
  }
  const extremeGated = signal.extremePricePct >= rules.liquidityFarming.maxExtremePricePct;
  const repeatedQuoteGated = signal.topRepeatedQuoteCount >= rules.liquidityFarming.minRepeatedQuoteCount;
  const sellGated =
    signal.extremePriceSellPct !== null &&
    signal.extremePriceSellPct >= rules.liquidityFarming.minExtremePriceSellPct;
  if (!extremeGated || !repeatedQuoteGated || !sellGated) {
    return null;
  }
  return {
    status: "ignore",
    reason:
      `liquidity farming / unreplicable edge: ${(signal.extremePricePct * 100).toFixed(1)}% of ${signal.sampleSize} ` +
      `sampled trades were at an extreme price (<0.05 or >0.95), ${(signal.extremePriceSellPct! * 100).toFixed(1)}% ` +
      `of those extreme-price trades were SELL-side (>= ${(rules.liquidityFarming.minExtremePriceSellPct * 100).toFixed(0)}% ` +
      `threshold — repeatedly selling into extreme prices, not buying longshots), and the single most-repeated ` +
      `(market, price) pair recurred ${signal.topRepeatedQuoteCount} times — >= ${rules.liquidityFarming.minRepeatedQuoteCount} ` +
      `threshold (composite score was ${r.compositeScore.toFixed(3)}, ignored regardless of score: being ` +
      `algorithmic isn't disqualifying, profit unreplicable via a copy with lag/fees is)`,
  };
}

/**
 * Top-N pool cap (pure decision logic — see the "TOP-15 POOL" doc comment
 * at the top of this file). Extracted for the same testability reason as
 * checkToxicFlowGate/checkRecencyGate.
 *
 * INPUT:  decided — every wallet's RAW decideStatus() result (status +
 *         compositeScore + address); poolSize — from computeDynamicPoolSize
 * OUTPUT: the set of wallet addresses that independently earned "track" but
 *         fall outside the top `poolSize` by compositeScore — the caller
 *         demotes exactly these to "watch".
 */
export function computeDemotedAddresses<T extends { address: string; status: string; compositeScore: number }>(
  decided: T[],
  poolSize: number
): Set<string> {
  const trackCandidates = decided
    .filter((r) => r.status === "track")
    .sort((a, b) => b.compositeScore - a.compositeScore);
  return new Set(trackCandidates.slice(poolSize).map((r) => r.address));
}

/**
 * Reads wallet_profile.status as it stood BEFORE this run, for exactly the
 * given addresses — lets finalizeAndWrite tell "already track, just
 * reconfirming" apart from "newly crossing the track threshold this run"
 * (2026-08-01, Telegram approval workflow). A wallet absent from
 * wallet_profile entirely (never scored before) is correctly treated as
 * `null`, same fallthrough shape as filterDueForRescore's dueMap above.
 */
async function getPriorStatuses(addresses: string[]): Promise<Map<string, string | null>> {
  if (addresses.length === 0) return new Map();
  const rows = await db
    .select({ walletAddress: walletProfile.walletAddress, status: walletProfile.status })
    .from(walletProfile)
    .where(inArray(walletProfile.walletAddress, addresses));
  return new Map(rows.map((r) => [normalizeAddress(r.walletAddress), r.status]));
}

/**
 * The final stage: applies the toxic-flow gate, the recency gate, decides
 * raw status per wallet, applies the top-N pool cap, and is the ONLY place
 * in pass 2 that actually writes to wallet_profile.
 *
 * ORDER OF OPERATIONS:
 *   1. Toxic-flow gate: any wallet with a KNOWN (not insufficient-data)
 *      rolling-window consistencyScore below minConsistencyScore is
 *      force-"ignore"'d immediately as a volume-farmer, independent of
 *      compositeScore/ROI/winRate, and written right away — it never
 *      reaches compositeScore-based logic or competes for a pool slot. See
 *      the "TOXIC-FLOW HARD GATE" doc comment at the top of this file.
 *   2. Recency gate: any wallet with no trade within maxDaysSinceLastTrade
 *      is force-"ignore"'d immediately, independent of its score, and
 *      written right away — it never competes for a pool slot.
 *   3. Liquidity-farming gate (v4): any wallet whose sampled recent trades
 *      are dominated by extreme-price fills AND a heavily-repeated quote is
 *      force-"ignore"'d immediately, independent of compositeScore — see
 *      the "Liquidity-farming hard gate (v4)" doc comment on ScoringRules.
 *      Being algorithmic isn't disqualifying; this catches profit that's
 *      structurally unreplicable via a copy with lag/fees, which the
 *      composite-score formula alone rewards rather than penalizes.
 *   4. Every remaining wallet gets its RAW status via decideStatus (the
 *      same absolute-threshold logic as before — unchanged).
 *   5. Among wallets that independently earned "track", only the top
 *      `poolSize` (by compositeScore) keep "track"; the rest are demoted to
 *      "watch" — still a good wallet, just not one of this month's picks.
 *
 * INPUT:  results — every Pass2Result from runPass2(); rules — active ScoringRules
 * OUTPUT: none (writes one wallet_profile row per result)
 */
async function finalizeAndWrite(results: Pass2Result[], rules: ScoringRules, trackedAddresses: Set<string>): Promise<void> {
  const { poolSize, recentCopyTradeCount } = await computeDynamicPoolSize(rules);
  console.log(
    `Top-N pool size for this run: ${poolSize} (base ${rules.topNPoolSize}` +
      (poolSize > rules.topNPoolSize
        ? `, expanded by ${rules.topNPoolExpansion} because only ${recentCopyTradeCount} copy-trade(s) ` +
          `landed in the last 30 days, below the ${rules.minMonthlyTradesForFullPool} floor`
        : `; ${recentCopyTradeCount} copy-trade(s) in the last 30 days is enough signal, no expansion needed`) +
      `)`
  );

  const afterToxicFlowGate: Pass2Result[] = [];
  let toxicFlowDropped = 0;

  for (const r of results) {
    const gate = checkToxicFlowGate(r, rules);
    if (gate) {
      await upsertWalletProfile({
        walletAddress: r.address,
        displayName: r.displayName,
        walletStats: r.walletStats,
        tradeFlow: r.tradeFlow,
        roiScore: r.roiScore,
        consistencyScore: r.consistencyScore,
        copyabilityScore: r.copyabilityScore,
        oneHitWonderPenalty: r.oneHitWonderPenalty,
        recentWinRate: r.recentWinRate,
        compositeScore: r.compositeScore,
        status: gate.status,
        statusReason: gate.reason,
        scoreBreakdown: r.scoreBreakdown,
        capitalMultiplier: r.capitalMultiplier,
        trackedAddresses,
        rules,
      });
      toxicFlowDropped++;
      continue;
    }
    afterToxicFlowGate.push(r);
  }

  const eligible: Pass2Result[] = [];
  let recencyDropped = 0;

  for (const r of afterToxicFlowGate) {
    const gate = checkRecencyGate(r, rules);
    if (gate) {
      await upsertWalletProfile({
        walletAddress: r.address,
        displayName: r.displayName,
        walletStats: r.walletStats,
        tradeFlow: r.tradeFlow,
        roiScore: r.roiScore,
        consistencyScore: r.consistencyScore,
        copyabilityScore: r.copyabilityScore,
        oneHitWonderPenalty: r.oneHitWonderPenalty,
        recentWinRate: r.recentWinRate,
        compositeScore: r.compositeScore,
        status: gate.status,
        statusReason: gate.reason,
        scoreBreakdown: r.scoreBreakdown,
        capitalMultiplier: r.capitalMultiplier,
        trackedAddresses,
        rules,
      });
      recencyDropped++;
      continue;
    }
    eligible.push(r);
  }

  const afterLiquidityFarmingGate: Pass2Result[] = [];
  let liquidityFarmingDropped = 0;

  for (const r of eligible) {
    const gate = checkLiquidityFarmingGate(r, rules);
    if (gate) {
      await upsertWalletProfile({
        walletAddress: r.address,
        displayName: r.displayName,
        walletStats: r.walletStats,
        tradeFlow: r.tradeFlow,
        roiScore: r.roiScore,
        consistencyScore: r.consistencyScore,
        copyabilityScore: r.copyabilityScore,
        oneHitWonderPenalty: r.oneHitWonderPenalty,
        recentWinRate: r.recentWinRate,
        compositeScore: r.compositeScore,
        status: gate.status,
        statusReason: gate.reason,
        scoreBreakdown: r.scoreBreakdown,
        capitalMultiplier: r.capitalMultiplier,
        trackedAddresses,
        rules,
      });
      liquidityFarmingDropped++;
      continue;
    }
    afterLiquidityFarmingGate.push(r);
  }

  const decided = afterLiquidityFarmingGate.map((r) => ({
    ...r,
    ...decideStatus(r.compositeScore, r.walletStats.trades_count, rules),
  }));

  const demotedAddresses = computeDemotedAddresses(decided, poolSize);
  const priorStatuses = await getPriorStatuses(decided.map((r) => r.address));

  let tracked = 0;
  let benched = 0;
  let watched = 0;
  let ignored = 0;
  let queuedForApproval = 0;

  for (const r of decided) {
    const isDemoted = demotedAddresses.has(r.address);
    const decidedStatus = isDemoted ? "watch" : r.status;
    const decidedReason = isDemoted
      ? `${r.reason}; demoted to watch — outside this month's top-${poolSize} pool by compositeScore`
      : r.reason;

    // Telegram approval workflow (2026-08-01): a wallet newly crossing the
    // track threshold this run does NOT get committed to wallet_profile
    // directly anymore — this global top-N pool used to be a second,
    // independent auto-promotion path alongside discoverCategorySpecialists.
    // ts's category-quota system, with no human review either way (see
    // walletApprovalQueue.ts's module doc comment). Only a wallet that was
    // ALREADY 'track' before this run (i.e. reconfirming, not newly
    // promoted) still writes 'track' directly — no re-approval spam for
    // wallets Joey already approved.
    const priorStatus = priorStatuses.get(normalizeAddress(r.address)) ?? null;
    // Bench membership is owned by the category-quota + Telegram approval
    // workflow (discoverCategorySpecialists.ts --queue-approvals), not this
    // global pass — decideStatus() only ever returns
    // 'track'/'watch'/'ignore', so without this guard a routine rescore
    // would silently wipe a Joey-approved bench wallet back to 'watch'/
    // 'ignore' the moment its GLOBAL score (a different signal from the
    // category-specific one that earned it 'bench') dipped. (The
    // force-ignore gates above this point — toxic-flow/recency/liquidity-
    // farming — are NOT exempted: those are safety overrides that already
    // apply regardless of tier, same as for a currently-'track' wallet.)
    const isBenchPreserved = priorStatus === "bench";
    let finalStatus = isBenchPreserved ? "bench" : decidedStatus;
    let finalReason = decidedReason;

    if (shouldRedirectToApprovalQueue(decidedStatus, priorStatus)) {
      // Covers two cases with one queue call: a genuinely new promotion
      // (priorStatus 'watch'/null) AND a bench->track promotion signal
      // (priorStatus 'bench', a legitimate reason to ask, but the wallet
      // stays 'bench' — not demoted to 'watch' — while the request is
      // pending, so it keeps getting live bench-tier paper trades meanwhile
      // instead of going dark).
      const { queued } = await queueApprovalRequest({
        walletAddress: r.address,
        requestedTier: decidedStatus,
        source: decidedStatus === "track" ? "global_pool" : "global_pool_demotion",
        category: null,
        scoreSnapshot: {
          compositeScore: r.compositeScore,
          winRate: r.recentWinRate,
          tradeCount: r.walletStats.trades_count,
        },
        reason: decidedReason,
      });
      finalStatus = priorStatus ?? "watch";
      const queuedNote = decidedStatus === "track"
        ? `pending Telegram approval to promote (stays '${finalStatus}' meanwhile)`
        : `pending Telegram approval to demote to ${decidedStatus} (stays '${finalStatus}' meanwhile)`;
      finalReason = queued
        ? `${decidedReason}; ${queuedNote}`
        : `${decidedReason}; already has a pending/recently-rejected Telegram approval request, not re-queued`;
      queuedForApproval++;
    } else if (isBenchPreserved) {
      finalReason = `${decidedReason}; wallet_profile.status left at 'bench' — owned by the category-quota/Telegram workflow, not this global pass`;
    }

    if (finalStatus === "track") tracked++;
    else if (finalStatus === "bench") benched++;
    else if (finalStatus === "watch") watched++;
    else ignored++;

    await upsertWalletProfile({
      walletAddress: r.address,
      displayName: r.displayName,
      walletStats: r.walletStats,
      tradeFlow: r.tradeFlow,
      roiScore: r.roiScore,
      consistencyScore: r.consistencyScore,
      copyabilityScore: r.copyabilityScore,
      oneHitWonderPenalty: r.oneHitWonderPenalty,
      recentWinRate: r.recentWinRate,
      compositeScore: r.compositeScore,
      status: finalStatus,
      statusReason: finalReason,
      scoreBreakdown: r.scoreBreakdown,
      capitalMultiplier: r.capitalMultiplier,
      trackedAddresses,
      rules,
    });
  }

  const totalIgnored = ignored + recencyDropped + toxicFlowDropped + liquidityFarmingDropped;
  console.log(
    `Pass 2 + ranking complete: ${tracked} track, ${benched} bench (unchanged, owned by the category-quota ` +
      `workflow), ${watched} watch, ${totalIgnored} ignore ` +
      `(${toxicFlowDropped} force-ignored as toxic flow / volume farmers, ${liquidityFarmingDropped} ` +
      `force-ignored as liquidity farming / unreplicable edge, ${recencyDropped} force-ignored for going ` +
      `cold, ${demotedAddresses.size} demoted from track by the pool cap, ${queuedForApproval} newly-` +
      `qualifying wallet(s) queued for Telegram approval instead of auto-tracked).`
  );
}

// =============================================================================
// SECTION 7: ENTRY POINT
// =============================================================================

async function main() {
  console.log("scan:wallets safety stop — legacy Bullpen-derived global scorer is disabled.");
  console.log("Official raw category scoring remains available via score:wallet-categories; no roster or sizing fields were changed.");
  return;

  const rules = await getActiveRuleSet();
  console.log(
    `Using rule_set v${rules.version} (weights: roi=${rules.weights.roi}, consistency=${rules.weights.consistency}, ` +
      `winRate=${rules.weights.winRate}, copyability=${rules.weights.copyability}; ` +
      `rolling window=${rules.rollingWindowDays}d; recency limit=${rules.maxDaysSinceLastTrade}d; ` +
      `min consistency=${rules.minConsistencyScore} (toxic-flow gate); ` +
      `liquidity-farming gate: >=${rules.liquidityFarming.minSampleSize} sample, ` +
      `>=${(rules.liquidityFarming.maxExtremePricePct * 100).toFixed(0)}% extreme price + ` +
      `>=${rules.liquidityFarming.minRepeatedQuoteCount}x repeated quote + ` +
      `>=${(rules.liquidityFarming.minExtremePriceSellPct * 100).toFixed(0)}% of those SELL-side; ` +
      `top-N pool=${rules.topNPoolSize} (+${rules.topNPoolExpansion} if signal is thin))`
  );

  const trackedAddresses = await getTrackedWalletAddresses();
  console.log(
    `Tier 1 (live-tracked, from bot_risk_state): ${trackedAddresses.size} wallet(s). Rescore cadence: ` +
      `tier1=${rules.tierRescoreIntervalDays.tier1}d, tier2=${rules.tierRescoreIntervalDays.tier2}d, ` +
      `tier3=${rules.tierRescoreIntervalDays.tier3}d.`
  );

  const allCandidates = await getCandidateWallets();
  console.log(`Found ${allCandidates.size} distinct candidate wallet(s) from leaderboard_scan.`);
  if (allCandidates.size === 0) {
    console.log("No candidates found — run `pnpm scan:leaderboard` first.");
    return;
  }

  const candidates = await filterDueForRescore(allCandidates);
  console.log(
    `${candidates.size} of ${allCandidates.size} candidate(s) are due for re-scoring right now ` +
      `(the rest were scored recently enough per their tier's cadence — see nextRescoreDueAt).`
  );
  if (candidates.size === 0) {
    console.log("Nothing due for re-scoring — done.");
    return;
  }

  const { survivors, rejected } = await runPass1(candidates, rules, trackedAddresses);
  console.log(`Pass 1 complete: ${survivors.length} survived, ${rejected} rejected (never paid for pass 2).`);

  if (survivors.length > 0) {
    const results = await runPass2(survivors, rules);
    await finalizeAndWrite(results, rules, trackedAddresses);
  }

  console.log(
    `Done. Scored ${candidates.size} wallet(s) total (${survivors.length} got the full pass-2 deep-dive analysis).`
  );
}

// Guards against running main() as a side effect of being imported — the
// scoreWallets.test.ts unit tests import this file's pure functions
// directly, and without this guard, doing so would trigger a real
// DB-writing production scan on every test run. Standard Node/ESM
// entry-point check: true when this file was executed directly (`tsx
// src/scoreWallets.ts`), false when it's imported as a module.
const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("scan:wallets failed:", err);
    process.exit(1);
  });
}
