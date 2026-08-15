// npm run score:wallet-categories
//
// =============================================================================
// WHAT THIS SCRIPT DOES, IN ONE PARAGRAPH
// =============================================================================
// scoreWallets.ts computes one global composite_score per wallet from
// bullpen's whole-wallet aggregate stats. This script answers a narrower,
// harder question bullpen's API genuinely cannot: "is this wallet actually
// good at Politics specifically, separately from Sports or Crypto?"
// (bullpen's wallet-stats endpoints — summary/flow/behavior/pnl-series — take
// no market or category filter at all, confirmed by reading scoreWallets.ts
// before building this.) So instead of asking bullpen, this script fetches
// each tracked wallet's RAW trade history directly from Polymarket's own Data
// API (polymarketDataApi.ts), reconstructs their positions and realized PnL
// itself (the same BUY/SELL/cost-basis arithmetic bot.py's own process_trade()
// already uses, just applied to a third party's trades), tags each closed
// position with a category (polymarketCategories.ts), and aggregates
// per-category win-rate/PnL into a score using the SAME normalization
// functions (computeRoiScore/computeWinRateScore) the global scorer already
// uses — reused rather than reinvented, just blended more simply (50/50, not
// the global score's 4-factor formula) since a single category's sample size
// is usually much smaller than a wallet's lifetime trade count.
//
// Writes the result into wallet_profile.category_scores_json — read by
// bot.py's compute_trade_size_usd() (see db.get_wallet_composite_scores())
// to size a copy by the wallet's edge in THIS SPECIFIC market's category,
// falling back to the global composite_score when no category score exists
// yet for that (wallet, category) pair.
//
// Run on the SAME monthly cadence as scoreWallets.ts (no new schedule) —
// this is a separate script/file, not fused into scoreWallets.ts itself,
// matching this codebase's existing one-script-per-concern convention
// (scanLeaderboard.ts / scoreWallets.ts are already separate files).
//
// =============================================================================
// WHAT'S DELIBERATELY EXCLUDED FROM THE PNL RECONSTRUCTION (edge cases)
// =============================================================================
// 1. A SELL against a position we never saw a BUY for (it was opened before
//    our lookback window started) — excluded entirely, cost basis unknowable.
//    Same accepted limitation as bot.py's own paper-position tracking (see
//    that file's module docstring: "pre-existing holdings... not visible").
// 2. A position still open (never sold) when the lookback window ends AND
//    the market hasn't resolved yet — excluded entirely. We don't guess at
//    a mark-to-market value; only REALIZED outcomes count.
// 3. A wallet whose raw trade history exceeds polymarketDataApi.ts's
//    MAX_PAGES — logged there, and this script's own reconstruction simply
//    works with whatever was returned (a known-partial history, not a bug).
// 4. A category with fewer than MIN_CATEGORY_SAMPLE resolved closes never
//    gets a score written at all (absent from category_scores_json, not a
//    noisy guess) — bot.py's fallback chain already treats "no category
//    entry" as "use the global score instead," so this needs no special
//    handling on the Python side.

import { eq, inArray } from "drizzle-orm";
import { db, walletProfile } from "@copybot/db";
import { fetchWalletTrades, type RawActivityRecord } from "./polymarketDataApi";
import { resolveMarketCategory } from "./polymarketCategories";
import { computeRoiScore, computeWinRateScore, DEFAULT_RULES } from "./scoreWallets";

const ROLLING_WINDOW_DAYS = 90; // same window scoreWallets.ts uses for the global score
const MIN_CATEGORY_SAMPLE = DEFAULT_RULES.hardMinTrades; // reuses the existing minimum rather than inventing a new one
const DUST_SHARES_THRESHOLD = 0.0001; // floating-point residue after closing a position, not a real remaining stake

const GAMMA_API_HOST = "https://gamma-api.polymarket.com";
const REQUEST_HEADERS = {
  "User-Agent": "polymarket-copybot/1.0 (+personal research bot)",
  Accept: "application/json",
};

interface Position {
  shares: number;
  costBasisUsd: number;
}

interface RealizedClose {
  category: string;
  pnlUsd: number;
  costBasisUsd: number;
  // Shares closed in this fill/resolution (added for TCA — see
  // aggregateCategoryScores' avg_entry_price: costBasisUsd/sharesClosed
  // gives the entry price actually paid, which is what a market order
  // would have crossed the spread AT — not the exit/resolution price,
  // since resolution pays out at a fixed 1.0/0.0 with no spread involved).
  sharesClosed: number;
  // This market's taker feeSchedule.rate at lookup time (0 when
  // feesEnabled=false — verified live, confirmed varies per market: 0.04,
  // 0.05 seen). Added for TCA (see CategoryScoreDetail.avg_fee_rate).
  feeRate: number;
}

interface MarketResolution {
  closed: boolean;
  outcomes: string[];
  outcomePrices: number[];
}

export interface CategoryScoreDetail {
  score: number;
  trade_count: number;
  win_rate: number;
  avg_pnl_usd: number;
  // One-sample t-statistic testing whether this category's mean realized
  // PnL is significantly less than zero (see aggregateCategoryScores'
  // docstring) — null when the sample has zero variance and zero mean (no
  // signal either way) or a degenerate n<2 case. bot.py's
  // should_skip_category() compares this against config.CATEGORY_SKIP_Z_CRITICAL
  // for a hard skip, separate from (stricter than) the score-based sizing.
  pnl_t_stat: number | null;
  // Return as a fraction of capital deployed (totalPnl / totalCostBasis) —
  // the SAME quantity computeRoiScore() already consumed internally, now
  // also surfaced directly (added for discoverCategorySpecialists.ts's TCA
  // filter, which compares this against estimated execution cost).
  roi: number;
  // Cost-basis-weighted average ENTRY price across this category's closes
  // (totalCostBasis / totalSharesClosed) — the price a market order would
  // have crossed the spread AT, not the exit/resolution price (added for
  // the same TCA filter — see RealizedClose.sharesClosed's comment).
  avg_entry_price: number | null;
  // Cost-basis-weighted average taker feeSchedule.rate across this
  // category's closes (added for TCA — combined with avg_entry_price via
  // fee = feeRate * price * (1-price), the SAME verified formula
  // polymarket_simulator.py's taker-fee check uses, this gives the expected
  // fee as a fraction of order USD: avg_fee_rate * (1 - avg_entry_price)).
  avg_fee_rate: number;
}

async function fetchMarketResolution(marketSlug: string): Promise<MarketResolution | null> {
  try {
    const response = await fetch(`${GAMMA_API_HOST}/markets?slug=${encodeURIComponent(marketSlug)}`, {
      headers: REQUEST_HEADERS,
    });
    if (response.status !== 200) return null;
    const data = (await response.json()) as Array<{ closed?: boolean; outcomes?: string; outcomePrices?: string }>;
    if (!data || data.length === 0) return null;
    const market = data[0];
    return {
      closed: Boolean(market.closed),
      outcomes: JSON.parse(market.outcomes ?? "[]") as string[],
      outcomePrices: (JSON.parse(market.outcomePrices ?? "[]") as string[]).map(Number),
    };
  } catch {
    return null; // a resolution-check failure just means this position gets excluded, not a crash
  }
}

/**
 * Fetches this market's taker fee rate — same Gamma `/markets?slug=`
 * endpoint and exact field shape polymarket_simulator.py's
 * get_market_by_slug() already verified live (feesEnabled bool +
 * feeSchedule.rate, 0 when fees are disabled). A separate fetch from
 * fetchMarketResolution's (not merged) because most closes never call
 * fetchMarketResolution at all — only still-open positions being marked to
 * a resolution payout do — while every close needs a fee rate for TCA.
 * Failure degrades to 0 (excludes this market from the fee estimate rather
 * than blocking the whole reconstruction on a transient fetch error).
 */
async function fetchMarketFeeRate(marketSlug: string): Promise<number> {
  try {
    const response = await fetch(`${GAMMA_API_HOST}/markets?slug=${encodeURIComponent(marketSlug)}`, {
      headers: REQUEST_HEADERS,
    });
    if (response.status !== 200) return 0;
    const data = (await response.json()) as Array<{ feesEnabled?: boolean; feeSchedule?: { rate?: number } }>;
    if (!data || data.length === 0) return 0;
    const market = data[0];
    if (!market.feesEnabled) return 0;
    return Number(market.feeSchedule?.rate ?? 0);
  } catch {
    return 0;
  }
}

/**
 * Reconstructs one wallet's positions from its raw trade history and
 * returns every REALIZED close — see module docstring for exactly what's
 * excluded (pre-window sells, still-open unresolved positions) and why.
 * `categoryOf`/`resolutionOf` are injected (not called directly on the
 * module-level functions) so this pure-ish reconstruction logic is testable
 * with fakes instead of mocking global fetch three layers deep.
 */
export async function reconstructRealizedCloses(
  trades: RawActivityRecord[],
  categoryOf: (marketSlug: string) => Promise<string | null>,
  resolutionOf: (marketSlug: string) => Promise<MarketResolution | null>,
  feeRateOf: (marketSlug: string) => Promise<number>
): Promise<RealizedClose[]> {
  const sorted = [...trades].sort((a, b) => a.timestamp - b.timestamp);
  const positions = new Map<string, Position>();
  const closes: RealizedClose[] = [];
  const categoryCache = new Map<string, string | null>();
  const feeRateCache = new Map<string, number>();

  const getCategory = async (marketSlug: string): Promise<string | null> => {
    if (!categoryCache.has(marketSlug)) {
      categoryCache.set(marketSlug, await categoryOf(marketSlug));
    }
    return categoryCache.get(marketSlug) ?? null;
  };

  const getFeeRate = async (marketSlug: string): Promise<number> => {
    if (!feeRateCache.has(marketSlug)) {
      feeRateCache.set(marketSlug, await feeRateOf(marketSlug));
    }
    return feeRateCache.get(marketSlug) ?? 0;
  };

  for (const trade of sorted) {
    const key = `${trade.slug}|${trade.outcome}`;
    const existing = positions.get(key) ?? { shares: 0, costBasisUsd: 0 };

    if (trade.side === "BUY") {
      positions.set(key, {
        shares: existing.shares + trade.size,
        costBasisUsd: existing.costBasisUsd + trade.usdcSize,
      });
      continue;
    }

    // SELL
    if (existing.shares <= 0) {
      // Selling from a position we never saw opened — pre-window, cost
      // basis unknowable. Excluded, not guessed at (see module docstring
      // edge case #1).
      continue;
    }
    const fractionSold = Math.min(1, trade.size / existing.shares);
    const costBasisClosed = existing.costBasisUsd * fractionSold;
    const sharesClosed = Math.min(trade.size, existing.shares);
    const pnlUsd = trade.usdcSize - costBasisClosed;

    const category = await getCategory(trade.slug);
    const feeRate = await getFeeRate(trade.slug);
    closes.push({ category: category ?? "other", pnlUsd, costBasisUsd: costBasisClosed, sharesClosed, feeRate });

    positions.set(key, {
      shares: Math.max(0, existing.shares - trade.size),
      costBasisUsd: Math.max(0, existing.costBasisUsd - costBasisClosed),
    });
  }

  // Still-open positions at the end of the window: only count them if the
  // market has actually resolved (edge case #2 — never mark-to-market a
  // live position as if it were a realized result).
  for (const [key, position] of positions.entries()) {
    if (position.shares <= DUST_SHARES_THRESHOLD) continue;
    const [marketSlug, outcome] = key.split("|");
    const resolution = await resolutionOf(marketSlug);
    if (!resolution || !resolution.closed) continue;

    const outcomeIndex = resolution.outcomes.findIndex((o) => o.toLowerCase() === outcome.toLowerCase());
    if (outcomeIndex === -1 || resolution.outcomePrices.length <= outcomeIndex) continue;

    const payoutPerShare = resolution.outcomePrices[outcomeIndex]; // 1.0 or 0.0 for a resolved binary market
    const payoutUsd = position.shares * payoutPerShare;
    const pnlUsd = payoutUsd - position.costBasisUsd;

    const category = await getCategory(marketSlug);
    const feeRate = await getFeeRate(marketSlug);
    closes.push({
      category: category ?? "other", pnlUsd, costBasisUsd: position.costBasisUsd,
      sharesClosed: position.shares, feeRate,
    });
  }

  return closes;
}

/**
 * Aggregates realized closes into a per-category score, reusing
 * computeRoiScore/computeWinRateScore (the SAME normalization functions and
 * thresholds scoreWallets.ts's global score already uses) rather than
 * inventing new ones — blended 50/50, simpler than the global score's
 * 4-factor formula, since a single category's sample is usually much
 * smaller than a wallet's lifetime trade count and a Sharpe-style
 * consistency measure would be shakier here than useful.
 *
 * A category with fewer than MIN_CATEGORY_SAMPLE closes is OMITTED from the
 * result entirely — not given a noisy score. bot.py's fallback chain
 * already treats "no category entry" as "use the global composite_score."
 *
 * Also computes pnl_t_stat: the one-sample t-statistic testing whether this
 * category's mean realized PnL is significantly LESS than zero (Student's
 * one-sample t-test — standard since Gosset 1908, the textbook method for
 * "is this sample mean significantly different from a hypothesized value,"
 * here zero expected profit). One-tailed, not two-tailed: we only care
 * about detecting harm (an unusually GOOD category is already rewarded via
 * `score`'s sizing, not something to gate on). bot.py's
 * should_skip_category() compares this against a critical value for a hard
 * skip — a separate, stricter decision than the score-based floor/ceiling
 * sizing computed here.
 */
export function aggregateCategoryScores(closes: RealizedClose[]): Record<string, CategoryScoreDetail> {
  const byCategory = new Map<string, RealizedClose[]>();
  for (const close of closes) {
    const list = byCategory.get(close.category) ?? [];
    list.push(close);
    byCategory.set(close.category, list);
  }

  const result: Record<string, CategoryScoreDetail> = {};
  for (const [category, categoryCloses] of byCategory.entries()) {
    if (categoryCloses.length < MIN_CATEGORY_SAMPLE) continue;

    const tradeCount = categoryCloses.length;
    const totalPnl = categoryCloses.reduce((sum, c) => sum + c.pnlUsd, 0);
    const totalCostBasis = categoryCloses.reduce((sum, c) => sum + c.costBasisUsd, 0);
    const wins = categoryCloses.filter((c) => c.pnlUsd > 0).length;

    const winRate = wins / tradeCount;
    const roi = totalCostBasis > 0 ? totalPnl / totalCostBasis : 0;
    const avgPnl = totalPnl / tradeCount;

    // Cost-basis-weighted, not a plain per-close average — a close that
    // deployed more capital should count for more, consistent with how
    // roi itself weights by totalCostBasis rather than by trade count.
    const totalSharesClosed = categoryCloses.reduce((sum, c) => sum + c.sharesClosed, 0);
    const avgEntryPrice = totalSharesClosed > 0 ? totalCostBasis / totalSharesClosed : null;
    const avgFeeRate =
      totalCostBasis > 0
        ? categoryCloses.reduce((sum, c) => sum + c.feeRate * c.costBasisUsd, 0) / totalCostBasis
        : 0;

    const roiScore = computeRoiScore(roi, DEFAULT_RULES);
    const winRateScoreValue = computeWinRateScore(winRate, DEFAULT_RULES);
    const score = 0.5 * roiScore + 0.5 * winRateScoreValue;

    // Sample standard deviation (Bessel's correction, n-1 denominator — the
    // standard unbiased estimator for a SAMPLE's variance, not population
    // variance) of realized PnL, then t = mean / (stddev / sqrt(n)).
    const variance =
      categoryCloses.reduce((sum, c) => sum + (c.pnlUsd - avgPnl) ** 2, 0) / (tradeCount - 1 || 1);
    const stdDev = Math.sqrt(variance);
    // Finite sentinel, not Infinity: JSON.stringify(Infinity) silently
    // serializes to `null` (confirmed live, not assumed) — writing a literal
    // Infinity here would have flipped "maximally strong evidence of harm"
    // into "no evidence at all" the moment it round-tripped through
    // category_scores_json. 1e6 is far more extreme than any realistic
    // critical value (~1.6-3), so it has the identical practical effect
    // without the serialization bug.
    const EXTREME_T_STAT_SENTINEL = 1e6;
    let pnlTStat: number | null;
    if (tradeCount < 2) {
      pnlTStat = null; // undefined for n<2, can't estimate variance at all
    } else if (stdDev === 0) {
      // Zero variance: every close had the identical PnL — if negative,
      // this is maximally strong (not weak) evidence of harm.
      pnlTStat = avgPnl < 0 ? -EXTREME_T_STAT_SENTINEL : avgPnl > 0 ? EXTREME_T_STAT_SENTINEL : null;
    } else {
      pnlTStat = (avgPnl / stdDev) * Math.sqrt(tradeCount);
    }

    result[category] = {
      score,
      trade_count: tradeCount,
      win_rate: winRate,
      avg_pnl_usd: avgPnl,
      pnl_t_stat: pnlTStat,
      roi,
      avg_entry_price: avgEntryPrice,
      avg_fee_rate: avgFeeRate,
    };
  }
  return result;
}

async function scoreOneWallet(walletAddress: string): Promise<Record<string, CategoryScoreDetail>> {
  const startEpochSeconds = Date.now() / 1000 - ROLLING_WINDOW_DAYS * 86400;
  const trades = await fetchWalletTrades(walletAddress, { startEpochSeconds });
  const closes = await reconstructRealizedCloses(trades, resolveMarketCategory, fetchMarketResolution, fetchMarketFeeRate);
  return aggregateCategoryScores(closes);
}

/**
 * Which wallets to score is deliberately NOT hardcoded to
 * wallet_profile.status='track' — that column is scoreWallets.ts's own
 * forward-looking recommendation, and is DISCONNECTED from what bot.py
 * actually trades whenever config.TRACKED_TRADERS_SOURCE="static" (the
 * current real setting, confirmed live: only 2 of the 20 statically-
 * configured wallets happen to carry status='track' today; most are
 * 'ignore'). There is no shared config module between Python and TS to
 * read the static list from directly, so this accepts explicit addresses
 * via argv, and only falls back to a DB-driven default (status IN
 * ('track','watch') — a bounded ~30 wallets, not all 470+ candidate rows)
 * when none are given.
 */
async function getWalletsToScore(): Promise<string[]> {
  const argvAddresses = process.argv.slice(2).filter((arg) => arg.startsWith("0x"));
  if (argvAddresses.length > 0) {
    return argvAddresses;
  }
  const rows = await db
    .select({ walletAddress: walletProfile.walletAddress })
    .from(walletProfile)
    .where(inArray(walletProfile.status, ["track", "watch"]));
  return rows.map((r) => r.walletAddress);
}

async function main() {
  const wallets = await getWalletsToScore();
  console.log(`Scoring category-specific performance for ${wallets.length} wallet(s)...`);

  for (const walletAddress of wallets) {
    try {
      const categoryScores = await scoreOneWallet(walletAddress);
      // wallet_profile.wallet_address is stored lowercase (see scoreWallets.ts's
      // normalizeAddress()/its own comment on why) and SQLite's default text
      // comparison is byte-for-byte, case-SENSITIVE — a real bug caught here
      // live: passing config.py's checksummed mixed-case addresses straight
      // into this WHERE clause silently matched zero rows on every write (no
      // error, just a no-op UPDATE) until this normalization was added.
      await db
        .update(walletProfile)
        .set({
          categoryScoresJson: JSON.stringify(categoryScores),
          derivedMetricsSource: "polymarket_official_raw_category",
          derivedMetricsVersion: "category-score-v1",
          derivedMetricsReady: true,
        })
        .where(eq(walletProfile.walletAddress, walletAddress.toLowerCase()));
      console.log(`  ${walletAddress}: ${JSON.stringify(categoryScores)}`);
    } catch (err) {
      console.warn(`  ${walletAddress}: failed — ${(err as Error).message}`);
    }
  }
}

// Same guard scoreWallets.ts uses — importing this file's pure functions for
// tests must not trigger a real DB-writing production run.
if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}
