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
import { runBullpenJson } from "@copybot/bullpen-client";
import { botEventLog, db, leaderboardScan, ruleSet, walletProfile } from "@copybot/db";
import { mapWithConcurrency } from "@copybot/shared";

const READ_RETRIES = 3;
const READ_RETRY_DELAY_MS = 500;
const PASS1_CONCURRENCY = 5; // how many wallets' cheap calls run at once
const PASS2_CONCURRENCY = 5; // how many wallets' expensive calls run at once

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
}

export const DEFAULT_RULES: ScoringRules = {
  version: 3,
  weights: { roi: 0.3, consistency: 0.2, winRate: 0.3, copyability: 0.2 },
  roiSaturation: 0.5,
  sharpeSaturation: 0.15,
  winRateSaturation: 0.65,
  oneHitWonderPenaltyStrength: 0.8,
  sampleConfidenceTradesFloor: 50,
  hardMinTrades: 5,
  pass1CutoffScore: 0.15,
  statusThresholds: { track: 0.55, watch: 0.3 },
  copyability: { floor: 0.2, minGood: 2, maxGood: 10, ceiling: 50, volumePresenceSaturation: 50000 },
  rollingWindowDays: 90,
  maxDaysSinceLastTrade: 30,
  topNPoolSize: 15,
  topNPoolExpansion: 5,
  minMonthlyTradesForFullPool: 10,
  minConsistencyScore: 0.2,
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
        `(rolling-window scoring + win rate + top-N pool + toxic-flow consistency gate) — deactivating ` +
        `v${active.version} and activating v${DEFAULT_RULES.version}.`
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
      "passing it). Only the top 15 (or 20 if recent bot trade volume is thin) qualifying wallets become " +
      "'track'; the rest are demoted to 'watch'. Supersedes v1's lifetime-history scoring (under-rated " +
      "dormant-then-hot wallets) and v2 (had no defense against high-win-rate/low-consistency volume-farmer " +
      "flow). Seeded by scoreWallets.ts.",
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
  try {
    const response = await runBullpenJson(["polymarket", "wallet-stats", address, "--section", "summary"], {
      retries: READ_RETRIES,
      retryDelayMs: READ_RETRY_DELAY_MS,
    });
    return response?.wallet_stats?.data ?? null;
  } catch (err) {
    console.warn(`  wallet-stats summary failed for ${address}: ${(err as Error).message}`);
    return null;
  }
}

/**
 * Fetches recent trading volume / net cash flow for one wallet.
 * INPUT:  a wallet address
 * OUTPUT: the TradeFlowData object, or null if the call failed
 */
async function fetchTradeFlow(address: string): Promise<TradeFlowData | null> {
  try {
    const response = await runBullpenJson(["polymarket", "wallet-stats", address, "--section", "flow"], {
      retries: READ_RETRIES,
      retryDelayMs: READ_RETRY_DELAY_MS,
    });
    return response?.trade_flow?.data ?? null;
  } catch (err) {
    console.warn(`  wallet-stats flow failed for ${address}: ${(err as Error).message}`);
    return null;
  }
}

/**
 * Fetches recent (1d/7d/30d) trading behavior — the rolling-window trade
 * frequency and win-rate fields this rewrite depends on.
 * INPUT:  a wallet address
 * OUTPUT: the BehaviorStatsData object, or null if the call failed
 */
async function fetchBehaviorStats(address: string): Promise<BehaviorStatsData | null> {
  try {
    const response = await runBullpenJson(["polymarket", "wallet-stats", address, "--section", "behavior"], {
      retries: READ_RETRIES,
      retryDelayMs: READ_RETRY_DELAY_MS,
    });
    return response?.behavior_stats?.data ?? null;
  } catch (err) {
    console.warn(`  wallet-stats behavior failed for ${address}: ${(err as Error).message}`);
    return null;
  }
}

/**
 * Fetches first/last trade timestamps for one wallet — used only for the
 * "has this wallet gone cold?" recency gate.
 * INPUT:  a wallet address
 * OUTPUT: the ActivityBoundsData object, or null if the call failed
 */
async function fetchActivityBounds(address: string): Promise<ActivityBoundsData | null> {
  try {
    const response = await runBullpenJson(["polymarket", "wallet-stats", address, "--section", "activity"], {
      retries: READ_RETRIES,
      retryDelayMs: READ_RETRY_DELAY_MS,
    });
    return response?.activity_bounds?.data ?? null;
  } catch (err) {
    console.warn(`  wallet-stats activity failed for ${address}: ${(err as Error).message}`);
    return null;
  }
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
  try {
    const response = await runBullpenJson(["polymarket", "pnl-series", "--address", address], {
      retries: READ_RETRIES,
      retryDelayMs: READ_RETRY_DELAY_MS,
    });
    return response?.pnl_series ?? [];
  } catch (err) {
    console.warn(`  pnl-series failed for ${address}: ${(err as Error).message}`);
    return [];
  }
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
    return { consistencyScore: 0, oneHitWonderPenalty: 0, recentWinRate: 0.5, insufficientData: true };
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

  return { consistencyScore, oneHitWonderPenalty, recentWinRate, insufficientData: false };
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
): { status: string; reason: string } {
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

  const scoredValues = {
    // Normalized again here, defensively, right at the write boundary — see
    // the normalizeAddress comment near the top of this file for why.
    walletAddress: normalizeAddress(args.walletAddress),
    status: args.status,
    statusReason: args.statusReason,
    statusChangedAt: now,
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
    scoreBreakdownJson: JSON.stringify(args.scoreBreakdown),
    lastScoredAt: now,
    updatedAt: now,
  };

  await db
    .insert(walletProfile)
    .values({
      ...scoredValues,
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
  rules: ScoringRules
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
    `Pass 2 (deep dive): fetching pnl-series + behavior + activity for ${survivors.length} wallets that ` +
      `passed pass 1, ${PASS2_CONCURRENCY} at a time...`
  );

  return mapWithConcurrency(survivors, PASS2_CONCURRENCY, async (candidate) => {
    const [series, behaviorStats, activityBounds] = await Promise.all([
      fetchPnlSeries(candidate.address),
      fetchBehaviorStats(candidate.address),
      fetchActivityBounds(candidate.address),
    ]);

    const { consistencyScore, oneHitWonderPenalty, recentWinRate, insufficientData } = analyzePnlSeries(
      series,
      rules
    );
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
 *   3. Every remaining wallet gets its RAW status via decideStatus (the
 *      same absolute-threshold logic as before — unchanged).
 *   4. Among wallets that independently earned "track", only the top
 *      `poolSize` (by compositeScore) keep "track"; the rest are demoted to
 *      "watch" — still a good wallet, just not one of this month's picks.
 *
 * INPUT:  results — every Pass2Result from runPass2(); rules — active ScoringRules
 * OUTPUT: none (writes one wallet_profile row per result)
 */
async function finalizeAndWrite(results: Pass2Result[], rules: ScoringRules): Promise<void> {
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
      });
      recencyDropped++;
      continue;
    }
    eligible.push(r);
  }

  const decided = eligible.map((r) => ({ ...r, ...decideStatus(r.compositeScore, r.walletStats.trades_count, rules) }));

  const demotedAddresses = computeDemotedAddresses(decided, poolSize);

  let tracked = 0;
  let watched = 0;
  let ignored = 0;

  for (const r of decided) {
    const isDemoted = demotedAddresses.has(r.address);
    const finalStatus = isDemoted ? "watch" : r.status;
    const finalReason = isDemoted
      ? `${r.reason}; demoted to watch — outside this month's top-${poolSize} pool by compositeScore`
      : r.reason;

    if (finalStatus === "track") tracked++;
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
    });
  }

  const totalIgnored = ignored + recencyDropped + toxicFlowDropped;
  console.log(
    `Pass 2 + ranking complete: ${tracked} track, ${watched} watch, ${totalIgnored} ignore ` +
      `(${toxicFlowDropped} force-ignored as toxic flow / volume farmers, ${recencyDropped} force-ignored ` +
      `for going cold, ${demotedAddresses.size} demoted from track by the pool cap).`
  );
}

// =============================================================================
// SECTION 7: ENTRY POINT
// =============================================================================

async function main() {
  console.log("scan:wallets starting — scoring candidate wallets from leaderboard_scan (rolling-window mode)...");

  const rules = await getActiveRuleSet();
  console.log(
    `Using rule_set v${rules.version} (weights: roi=${rules.weights.roi}, consistency=${rules.weights.consistency}, ` +
      `winRate=${rules.weights.winRate}, copyability=${rules.weights.copyability}; ` +
      `rolling window=${rules.rollingWindowDays}d; recency limit=${rules.maxDaysSinceLastTrade}d; ` +
      `min consistency=${rules.minConsistencyScore} (toxic-flow gate); ` +
      `top-N pool=${rules.topNPoolSize} (+${rules.topNPoolExpansion} if signal is thin))`
  );

  const candidates = await getCandidateWallets();
  console.log(`Found ${candidates.size} distinct candidate wallet(s) from leaderboard_scan.`);
  if (candidates.size === 0) {
    console.log("No candidates found — run `pnpm scan:leaderboard` first.");
    return;
  }

  const { survivors, rejected } = await runPass1(candidates, rules);
  console.log(`Pass 1 complete: ${survivors.length} survived, ${rejected} rejected (never paid for pass 2).`);

  if (survivors.length > 0) {
    const results = await runPass2(survivors, rules);
    await finalizeAndWrite(results, rules);
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
