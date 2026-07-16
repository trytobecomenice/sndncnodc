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
// scoring formula (ROI + consistency + a "one-hit-wonder" penalty +
// copyability), and writes a `status` of "track" / "watch" / "ignore" onto
// each wallet in the `wallet_profile` table. `bot.py` will eventually read
// that `status` column to decide who to actually copy-trade (once
// config.TRACKED_TRADERS_SOURCE is flipped from "static" to "db" — it isn't
// yet, so running this script today only writes data, it doesn't change
// live trading behavior).
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
//   entirely — we never pay for the expensive call on a wallet that was
//   never going to pass anyway.
//
//   PASS 2 (expensive, ~15s/wallet, only for pass-1 survivors): fetch the
//   wallet's full `pnl-series` history and use it to measure how CONSISTENT
//   their gains are, and how much of their profit came from one lucky
//   spike (the "one-hit-wonder" check).
//
// Both passes use a small "concurrency pool" (see @copybot/shared) so we run
// several wallets at once instead of one at a time, without overwhelming the
// API.
//
// =============================================================================
// DATABASE SAFETY BOUNDARY (see docs/SAFETY.md)
// =============================================================================
// READS FROM:  leaderboard_scan (who are the candidates?), rule_set (what
//              scoring weights/thresholds are currently active?)
// WRITES TO:   wallet_profile — but ONLY the scoring-related columns and
//              `status`/`statusReason`/`statusChangedAt`.
// NEVER TOUCHES: wallet_profile.circuitBreakerMuted, muteReason, mutedAt,
//              consecutiveLosses, recentResultsJson — those five columns
//              belong exclusively to bot.py's circuit breaker (see db.py).
//              The one function that writes to wallet_profile
//              (upsertWalletProfile, near the bottom of this file) simply
//              never mentions those column names, so Drizzle can't touch
//              them even by accident.

import { eq, sql } from "drizzle-orm";
import { runBullpenJson } from "@copybot/bullpen-client";
import { db, leaderboardScan, ruleSet, walletProfile } from "@copybot/db";
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
// and explained. Today we only ever create version 1 with our best initial
// guesses — nothing here has been "trained," it's a reasonable starting
// point per the plan we discussed and agreed on.

interface ScoringRules {
  version: number;

  // How much each of the three sub-scores (0..1 each) contributes to the
  // final compositeScore. These three numbers should add up to 1.0.
  weights: {
    roi: number;
    consistency: number;
    copyability: number;
  };

  // roi_30d (a fraction, e.g. 0.5 = 50% monthly return) at or above this
  // value maps to a perfect roiScore of 1.0. Below 0 maps to 0.
  roiSaturation: number;

  // Our simplified Sharpe ratio (mean gain per unit of volatility) at or
  // above this value maps to a perfect consistencyScore of 1.0.
  sharpeSaturation: number;

  // How much damage the one-hit-wonder penalty can do to the final score.
  // 0.8 means: even a MAXIMALLY concentrated wallet (all gains from one
  // spike) only loses 80% of its score, never 100% — a single red flag
  // shouldn't be treated identically to "definitely worthless."
  oneHitWonderPenaltyStrength: number;

  // Lifetime trade count at or above this maps to full (1.0) confidence in
  // this wallet's other scores. Fewer trades linearly discounts everything.
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
}

const DEFAULT_RULES: ScoringRules = {
  version: 1,
  weights: { roi: 0.4, consistency: 0.35, copyability: 0.25 },
  roiSaturation: 0.5,
  sharpeSaturation: 0.15,
  oneHitWonderPenaltyStrength: 0.8,
  sampleConfidenceTradesFloor: 50,
  hardMinTrades: 5,
  pass1CutoffScore: 0.15,
  statusThresholds: { track: 0.55, watch: 0.3 },
  copyability: { floor: 0.2, minGood: 2, maxGood: 10, ceiling: 50, volumePresenceSaturation: 50000 },
};

/**
 * Reads the currently-active scoring rules from the database. If none exist
 * yet (very first time this script has ever run), it creates version 1
 * using DEFAULT_RULES above and saves it — every future run will then reuse
 * that same saved version, so results stay comparable across runs, until
 * `updateRules.ts` deliberately creates a new version.
 *
 * INPUT:  none (reads the `rule_set` table)
 * OUTPUT: the active ScoringRules object to use for this run
 */
async function getActiveRuleSet(): Promise<ScoringRules> {
  const rows = await db.select().from(ruleSet).where(eq(ruleSet.isActive, true)).limit(1);
  if (rows.length > 0) {
    return JSON.parse(rows[0].thresholdsJson) as ScoringRules;
  }

  console.log("No active rule_set found — bootstrapping version 1 with default weights/thresholds.");
  await db.insert(ruleSet).values({
    version: DEFAULT_RULES.version,
    isActive: true,
    thresholdsJson: JSON.stringify(DEFAULT_RULES),
    description:
      "Initial wallet-scoring weights: 40% ROI, 35% consistency (Sharpe-proxy), 25% copyability, " +
      "with a one-hit-wonder penalty and a trade-count confidence multiplier. Seeded by scoreWallets.ts.",
  });
  return DEFAULT_RULES;
}

// =============================================================================
// SECTION 2: SMALL MATH HELPERS
// =============================================================================

/** Clamps `value` into the [min, max] range. Used everywhere to keep every
 * sub-score in the 0..1 range we promised the database schema. */
function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function mean(values: number[]): number {
  if (values.length === 0) return 0;
  return values.reduce((sum, v) => sum + v, 0) / values.length;
}

/** Sample standard deviation (uses n-1, the standard choice when the values
 * we have are treated as a SAMPLE of the wallet's true behavior, not the
 * entire universe of it). Returns 0 if there aren't at least 2 values. */
function sampleStdev(values: number[]): number {
  if (values.length < 2) return 0;
  const m = mean(values);
  const variance = values.reduce((sum, v) => sum + (v - m) ** 2, 0) / (values.length - 1);
  return Math.sqrt(variance);
}

// =============================================================================
// SECTION 3: FETCHING DATA FROM THE `bullpen` CLI
// =============================================================================
// These three functions are the ONLY places this script talks to the
// outside world. Each one is defensive: if the call fails or times out
// (which we verified DOES happen regularly for some bullpen endpoints —
// see the plan discussion), we log a warning and return null/empty rather
// than crashing the whole script over one bad wallet.

/** Shape of the data bullpen returns for `wallet-stats --section summary`. */
interface WalletStatsData {
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
interface TradeFlowData {
  wallet_address: string;
  volume_24h: number;
  volume_30d: number;
  volume_7d: number;
  net_flow_24h: number;
  net_flow_30d: number;
  net_flow_7d: number;
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
 * Fetches the wallet's full portfolio-value history — a list of
 * {p: value in USD, t: unix timestamp} points, roughly one per hour, going
 * back as far as bullpen has data. THIS is the expensive pass-2-only call.
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
function computeRoiScore(roi30d: number, rules: ScoringRules): number {
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
 */
function computeSampleConfidence(tradesCount: number, rules: ScoringRules): number {
  return clamp(tradesCount / rules.sampleConfidenceTradesFloor, 0, 1);
}

interface SeriesAnalysis {
  consistencyScore: number;
  oneHitWonderPenalty: number;
}

/**
 * The most important function in this file — analyzes a wallet's FULL
 * portfolio-value history to answer two questions: (1) does this wallet
 * gain steadily, or wildly swing up and down? and (2) did most of its
 * profit come from one lucky spike, or is it spread across many periods?
 *
 * INPUT:  series — the raw {p: value, t: timestamp} points from pnl-series
 * OUTPUT: { consistencyScore, oneHitWonderPenalty }, both 0..1
 *
 * HOW IT WORKS:
 *   Step 1: turn the raw VALUES into period-over-period CHANGES ("deltas").
 *     e.g. if the wallet's portfolio went $100 -> $110 -> $105, the deltas
 *     are [+10, -5]. We analyze the deltas, not the raw values, because
 *     what we care about is "how did they perform in each period," not
 *     "how big is their account."
 *
 *   Step 2 (consistency): compute a simplified SHARPE RATIO — a standard
 *     quant-finance metric — as (average delta) / (volatility of deltas).
 *     A wallet that gains a little bit steadily has a HIGH Sharpe ratio
 *     even if its total profit is modest. A wallet that swings wildly
 *     (huge up days, huge down days) has a LOW Sharpe ratio even if it
 *     ends up profitable overall, because the pattern is too noisy to
 *     trust going forward.
 *
 *   Step 3 (one-hit-wonder): look ONLY at the positive deltas (the
 *     gaining periods) and ask: what fraction of ALL the gains came from
 *     the single best period? If one spike is 90% of everything this
 *     wallet ever made, that's a huge red flag — it looks like one lucky
 *     event, not a repeatable skill.
 *
 *   HONEST LIMITATION: pnl-series gives us roughly hourly snapshots, not
 *   one row per individual trade. So this technically measures "how much
 *   did one concentrated PERIOD dominate the gains," which is a good proxy
 *   for "one lucky trade" but isn't a perfectly literal measurement of it —
 *   a wallet could technically make several trades within the same
 *   dominant hour. Worth knowing, not worth over-engineering around today.
 */
function analyzePnlSeries(series: Array<{ p: number; t: number }>, rules: ScoringRules): SeriesAnalysis {
  if (series.length < 3) {
    // Not enough history to say anything meaningful about consistency or
    // concentration — score neutral-low rather than guessing from noise.
    return { consistencyScore: 0, oneHitWonderPenalty: 0 };
  }

  // Defensive sort: the API should already return points in time order, but
  // we don't want one out-of-order point silently corrupting every delta
  // computed after it.
  const sorted = [...series].sort((a, b) => a.t - b.t);

  const deltas: number[] = [];
  for (let i = 1; i < sorted.length; i++) {
    deltas.push(sorted[i].p - sorted[i - 1].p);
  }

  // --- Consistency score (simplified Sharpe ratio) ---
  const meanDelta = mean(deltas);
  const stdevDelta = sampleStdev(deltas);
  const sharpeProxy = stdevDelta > 0 ? meanDelta / stdevDelta : 0;
  const consistencyScore = clamp(sharpeProxy / rules.sharpeSaturation, 0, 1);

  // --- One-hit-wonder penalty (gain concentration) ---
  const positiveDeltas = deltas.filter((d) => d > 0);
  const totalGain = positiveDeltas.reduce((sum, d) => sum + d, 0);
  const maxSingleGain = positiveDeltas.length > 0 ? Math.max(...positiveDeltas) : 0;
  const oneHitWonderPenalty = totalGain > 0 ? maxSingleGain / totalGain : 0;

  return { consistencyScore, oneHitWonderPenalty };
}

/**
 * Copyability score: could our bot actually, practically follow this
 * wallet's trades? (v1 — deliberately simple; see the plan discussion for
 * why the richer bullpen-provided tiers aren't reliably available today.)
 *
 * INPUT:  tradesCount   — lifetime trade count
 *         joinDateIso   — when bullpen first has data for this wallet (a
 *                         stand-in for "first trade," chosen because it
 *                         comes free with the summary call we already made
 *                         — no extra API call needed)
 *         volume30d     — dollar volume traded in the last 30 days
 * OUTPUT: a 0..1 score, blending two things:
 *   (a) "frequency fit" — is this wallet trading at a pace we could
 *       realistically keep up with? Too RARE (a trade every few weeks)
 *       means too little signal to build a read on them. Too FRANTIC
 *       (dozens of trades a day, probably a bot) means every individual
 *       trade is noise we can't meaningfully copy one at a time on our
 *       30-second poll loop. The best copyability is a comfortable middle
 *       ground.
 *   (b) "volume presence" — is this wallet still actually active with real
 *       money recently? A wallet that made a killing 8 months ago and has
 *       gone quiet since is far less useful than one still trading size
 *       right now.
 */
function computeCopyabilityScore(
  tradesCount: number,
  joinDateIso: string,
  volume30d: number,
  rules: ScoringRules
): number {
  const { floor, minGood, maxGood, ceiling, volumePresenceSaturation } = rules.copyability;

  const joinDateMs = new Date(joinDateIso).getTime();
  const daysActive = Math.max(1, (Date.now() - joinDateMs) / (1000 * 60 * 60 * 24));
  const tradesPerDay = tradesCount / daysActive;

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
 * INPUT:  the four sub-scores (roi, consistency, copyability,
 *         oneHitWonderPenalty) plus sampleConfidence
 * OUTPUT: a single 0..1 compositeScore
 *
 * ORDER OF OPERATIONS (each step matters):
 *   1. Blend the three positive sub-scores using the weights from rule_set.
 *   2. Apply the one-hit-wonder penalty as a PERCENTAGE REDUCTION (not a
 *      flat subtraction) — a wallet that was already scoring low doesn't
 *      get pushed into negative territory, it just loses a share of
 *      whatever score it had.
 *   3. Multiply by sampleConfidence LAST, so a low-trade-count wallet has
 *      its entire final score pulled toward zero, no matter how good its
 *      individual numbers looked.
 */
function computeCompositeScore(
  roiScore: number,
  consistencyScore: number,
  copyabilityScore: number,
  oneHitWonderPenalty: number,
  sampleConfidence: number,
  rules: ScoringRules
): number {
  const { roi, consistency, copyability } = rules.weights;
  const blended = roi * roiScore + consistency * consistencyScore + copyability * copyabilityScore;
  const afterPenalty = blended * (1 - rules.oneHitWonderPenaltyStrength * oneHitWonderPenalty);
  return afterPenalty * sampleConfidence;
}

/**
 * Turns a compositeScore into the final track/watch/ignore decision,
 * along with a human-readable reason (this reason gets saved to the
 * database so you can always see WHY a wallet ended up where it did).
 */
function decideStatus(
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
// SECTION 6: THE TWO PASSES
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
 * doesn't clear the bar — without ever paying for the expensive pnl-series
 * call on wallets that were never going to pass anyway.
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

/**
 * PASS 2 — the expensive deep-dive. Only runs on wallets that survived pass
 * 1. Fetches each survivor's full pnl-series history, computes consistency
 * and the one-hit-wonder penalty, finalizes the compositeScore, and writes
 * the final track/watch/ignore verdict.
 *
 * INPUT:  survivors — the list from runPass1(); rules — active ScoringRules
 * OUTPUT: none (writes one wallet_profile row per survivor)
 */
async function runPass2(survivors: Pass1Survivor[], rules: ScoringRules): Promise<void> {
  console.log(
    `Pass 2 (deep dive): fetching pnl-series for ${survivors.length} wallets that passed pass 1, ` +
      `${PASS2_CONCURRENCY} at a time...`
  );

  await mapWithConcurrency(survivors, PASS2_CONCURRENCY, async (candidate) => {
    const series = await fetchPnlSeries(candidate.address);
    const { consistencyScore, oneHitWonderPenalty } = analyzePnlSeries(series, rules);
    const copyabilityScore = computeCopyabilityScore(
      candidate.walletStats.trades_count,
      candidate.walletStats.join_date,
      candidate.tradeFlow?.volume_30d ?? 0,
      rules
    );
    const compositeScore = computeCompositeScore(
      candidate.roiScore,
      consistencyScore,
      copyabilityScore,
      oneHitWonderPenalty,
      candidate.sampleConfidence,
      rules
    );
    const { status, reason } = decideStatus(compositeScore, candidate.walletStats.trades_count, rules);

    await upsertWalletProfile({
      walletAddress: candidate.address,
      displayName: candidate.displayName,
      walletStats: candidate.walletStats,
      tradeFlow: candidate.tradeFlow,
      roiScore: candidate.roiScore,
      consistencyScore,
      copyabilityScore,
      oneHitWonderPenalty,
      compositeScore,
      status,
      statusReason: reason,
      scoreBreakdown: {
        pass: 2,
        roiScore: candidate.roiScore,
        consistencyScore,
        copyabilityScore,
        oneHitWonderPenalty,
        sampleConfidence: candidate.sampleConfidence,
        compositeScore,
        pnlSeriesPoints: series.length,
      },
    });
  });
}

// =============================================================================
// SECTION 7: ENTRY POINT
// =============================================================================

async function main() {
  console.log("scan:wallets starting — scoring candidate wallets from leaderboard_scan...");

  const rules = await getActiveRuleSet();
  console.log(
    `Using rule_set v${rules.version} (weights: roi=${rules.weights.roi}, ` +
      `consistency=${rules.weights.consistency}, copyability=${rules.weights.copyability})`
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
    await runPass2(survivors, rules);
  }

  console.log(
    `Done. Scored ${candidates.size} wallet(s) total (${survivors.length} got the full pass-2 deep-dive analysis).`
  );
}

main().catch((err) => {
  console.error("scan:wallets failed:", err);
  process.exit(1);
});
