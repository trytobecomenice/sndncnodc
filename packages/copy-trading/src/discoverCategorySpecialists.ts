// npm run discover:category-specialists
//
// =============================================================================
// WHAT THIS SCRIPT DOES, IN ONE PARAGRAPH
// =============================================================================
// The 20 currently-tracked wallets are all we've ever category-scored. This
// script runs the SAME reconstruction scoreWalletCategories.ts uses (raw
// trade history -> positions -> resolution-aware realized PnL -> per-category
// t-test) against a BROADER candidate pool, to surface wallets worth adding
// to tracking specifically because of a strong CATEGORY-SPECIFIC edge — not
// necessarily a strong overall record. This is a REPORTING tool only: it
// prints a ranked list, it does not write to wallet_profile.status or touch
// config.TRACKED_TRADERS. Deciding what to actually track stays a human
// decision, matching how the current 20 were hand-curated in the first place.
//
// =============================================================================
// WHY NOT JUST ASK BULLPEN FOR "TOP TRADERS BY CATEGORY"? (checked live, not assumed)
// =============================================================================
// Two bullpen paths exist and were both tried:
//   1. `bullpen data leaderboard` — no category filter at all.
//   2. `bullpen data smart-money --type top_traders --category <X>` — has a
//      real, documented --category flag (politics/sports/crypto/culture/
//      economics/tech/finance) — but tested live just now: crypto/politics/
//      sports all return genuinely EMPTY ("traders": []), one attempt timed
//      out server-side. scanLeaderboard.ts's own comments already flagged
//      this endpoint as never-confirmed-working; this just re-confirms it's
//      still not usable.
// So this reconstructs from raw trade history directly, same as
// scoreWalletCategories.ts — no bullpen discovery dependency.
//
// =============================================================================
// CANDIDATE POOL — a real trade-off, not hidden
// =============================================================================
// wallet_profile already has 470+ scored candidates (from scoreWallets.ts's
// existing monthly run, seeded from scanLeaderboard.ts's discovery). Running
// the expensive reconstruction against all of them would take hours and risk
// sustained rate-limit pressure. But filtering by the wallet's existing
// GLOBAL composite_score/status is exactly the signal this feature exists to
// second-guess — a wallet mediocre overall (or status='ignore') could still
// be a genuine specialist in one category. Checked live: status!='ignore'
// alone leaves only 26 candidates (too narrow, would likely hide real
// specialists); composite_score >= 0.2 leaves 107 (the default here);
// --all bypasses the filter entirely for an exhaustive (much slower) run.
const DEFAULT_MIN_COMPOSITE_SCORE = 0.2;

// =============================================================================
// TCA (TRANSACTION COST ANALYSIS) FILTER
// =============================================================================
// A wallet can be statistically significant (clears Z_CRITICAL) and still be
// worthless to copy if its historical edge is smaller than what OUR OWN
// market order would cost to execute. This is Implementation Shortfall
// (Perold, 1988): IS = explicit costs (fees) + implicit costs (spread/impact)
// + delay/opportunity cost. We model the first two (Ce, Ci) — not delay or
// opportunity cost (Cd, Co), a deliberate simplification: our copy trades are
// tiny ($3-$10) relative to the market, executed within seconds of detecting
// the source trade, so delay cost is not the dominant term the way it is in
// institutional execution research.
//
// FEES (Ce) are EXACT, not estimated: fee = feeRate * price * (1-price) is
// the verified live formula (polymarket_simulator.py, confirmed against real
// trades), and avg_fee_rate is now threaded through from the market's own
// feeSchedule.rate — no guessing needed here.
//
// SLIPPAGE (Ci) must be ESTIMATED: Polymarket's public API only exposes the
// CURRENT order book, not the book as it stood at each historical trade's
// timestamp. Grounded in Polymarket-specific microstructure research (median
// relative bid-ask spread by price bucket): ~4% (400bps) for markets priced
// near the center [0.4, 0.6], widening to ~13-18% (we use 15.5%, the
// midpoint) for longshot markets priced below 0.10 or above 0.90 — the
// "longshot spread premium." A market BUY order crosses from mid-price to
// the far touch, i.e. HALF the quoted spread — not the full spread, since
// mid is the reference point, not the near touch. This is sized to OUR
// order ($3-$10), not the candidate's historical trade size (often
// $1k-$50k+): a smaller order walks less of the book, so this is a
// favorable (not pessimistic) assumption for us, stated explicitly rather
// than hidden.
//
// SAFETY BUFFER is a configurable PERCENTAGE of price (not a flat USD
// amount) per explicit instruction — a flat $0.50 buffer would be
// meaningless on a $0.03 longshot (16x the price) and negligible on a $0.97
// near-certainty, so a percentage scales correctly across the whole
// $0.03-$0.97 price spectrum. Default 2%, overridable via --tca-safety-buffer.
//
// THE FILTER: a category only survives if
//   roi > estimatedRelativeSpread(avg_entry_price)/2
//         + avg_fee_rate * (1 - avg_entry_price)
//         + TCA_SAFETY_BUFFER_PCT
// roi is TOTAL realized PnL / TOTAL capital deployed across all of this
// category's closes (see aggregateCategoryScores) — the same "return on
// capital" quantity, compared directly against these three cost terms
// (also expressed as a fraction of capital deployed).
const DEFAULT_TCA_SAFETY_BUFFER_PCT = 0.02;

// The two anchor points are both empirically confirmed Polymarket-specific
// findings (not textbook equity-market numbers, which don't transfer to
// prediction markets' longshot-bias microstructure):
const SPREAD_AT_CENTER = 0.04; // ~400bps, price in [0.4, 0.6] (distance from 0.5 <= 0.1)
const SPREAD_AT_EXTREME = 0.155; // ~13-18%, midpoint used; price < 0.10 or > 0.90 (distance >= 0.4)
const CENTER_DISTANCE = 0.1;
const EXTREME_DISTANCE = 0.4;

/**
 * Piecewise-linear estimate of relative bid-ask spread as a function of
 * price, interpolating between the two research-confirmed anchor points
 * above. Symmetric around 0.5 (Polymarket binary markets have no inherent
 * skew toward Yes vs No pricing). Pure function, directly unit-testable.
 */
export function estimatedRelativeSpread(price: number): number {
  const distanceFromCenter = Math.abs(price - 0.5);
  if (distanceFromCenter <= CENTER_DISTANCE) return SPREAD_AT_CENTER;
  if (distanceFromCenter >= EXTREME_DISTANCE) return SPREAD_AT_EXTREME;
  const t = (distanceFromCenter - CENTER_DISTANCE) / (EXTREME_DISTANCE - CENTER_DISTANCE);
  return SPREAD_AT_CENTER + t * (SPREAD_AT_EXTREME - SPREAD_AT_CENTER);
}

/**
 * The strict TCA inequality itself — pure function, directly unit-testable.
 * Returns false (not viable) for a null avg_entry_price (can happen if
 * every close in the category came from a resolution mark rather than an
 * actual sell, which — see RealizedClose — can leave totalSharesClosed at
 * 0 only in a degenerate case; treated as "insufficient data," not "pass").
 */
export function passesTcaFilter(
  candidate: { roi: number; avgEntryPrice: number | null; avgFeeRate: number },
  safetyBufferPct: number
): boolean {
  if (candidate.avgEntryPrice === null) return false;
  const estimatedSlippagePct = estimatedRelativeSpread(candidate.avgEntryPrice) / 2;
  const estimatedFeePct = candidate.avgFeeRate * (1 - candidate.avgEntryPrice);
  return candidate.roi > estimatedSlippagePct + estimatedFeePct + safetyBufferPct;
}

// =============================================================================
// TCA ENTRY-PRICE FLOOR (2026-07-24, Rule 27)
// =============================================================================
// Live-verifying the discovery output against a real 3-category "specialist"
// (0xc21ea96b...) surfaced a failure mode passesTcaFilter alone doesn't
// catch: its avg_entry_price is EXACTLY $0.001 — Polymarket's minimum price
// tick — on every one of its 1,206+ trades, across every category, with a
// 100% win rate in all of them. That's not longshot-picking skill; it's the
// textbook signature of settlement/resolution sniping (buying the already-
// effectively-certain side for pennies right before formal on-chain
// resolution). The wallet's ROI (100-190%) comfortably clears the TCA cost
// bar anyway, but that bar is unsound at this price point for two
// compounding reasons:
//   1. estimatedRelativeSpread() was calibrated from research anchored at
//      price >= $0.10 — it flatlines at 15.5% for everything below that,
//      including $0.001, a price 100x more extreme than what the estimate
//      was actually validated at. No research grounding exists for trusting
//      that number this deep into the tail.
//   2. Even if the spread estimate were right, the tiny pool of floor-priced
//      liquidity this wallet captured is almost certainly gone by the time a
//      lagging copy-bot detects the trade and fires its own order — the
//      historical ROI describes a fill we structurally cannot replicate.
//
// DEFAULT_TCA_MIN_ENTRY_PRICE = 0.02 — explicitly a judgment call, stated as
// such rather than dressed up as more rigorous than it is: a full order of
// magnitude above the confirmed $0.001 platform tick, chosen to separate
// "genuinely thin-probability market" from "structurally at the platform
// floor" (estimatedRelativeSpread's own price<0.10 anchor doesn't further
// distinguish within that range) — not to model spread precisely.
// Configurable via --tca-min-entry-price, tunable with more evidence.
//
// A HARD REJECTION, not a warning (unlike Rule 23's wash-trading screen,
// deliberately a "look closer" annotation because the false-positive risk
// against genuinely disciplined small-edge traders was real): a 100% win
// rate at the LITERAL platform floor, on every trade, across every
// unrelated category, is about as close to definitive evidence of this
// specific failure mode as this kind of analysis gets.
//
// RUNS BEFORE the TCA filter, not alongside it — a real ordering
// requirement: estimatedRelativeSpread(avg_entry_price) is a direct input
// to the TCA formula itself, so if the entry price isn't trustworthy, the
// TCA verdict built on top of it isn't either.
const DEFAULT_TCA_MIN_ENTRY_PRICE = 0.02;

/**
 * Pure predicate — mirrors passesTcaFilter's shape. Returns false (not
 * trustworthy) for a null avgEntryPrice (same "insufficient data" convention
 * passesTcaFilter uses), and false when avgEntryPrice sits within
 * minEntryPrice of EITHER extreme — symmetric, matching
 * estimatedRelativeSpread()'s own existing symmetry around 0.5 (a $0.999
 * "already-won" floor-price buy is the same failure mode mirrored).
 */
export function passesEntryPriceFloor(
  avgEntryPrice: number | null,
  minEntryPrice: number = DEFAULT_TCA_MIN_ENTRY_PRICE
): boolean {
  if (avgEntryPrice === null) return false;
  // Strict inequality, matching passesTcaFilter's own convention (landing
  // exactly on a bar doesn't clear it) — the more conservative reading,
  // consistent with this gate's low-false-positive-tolerance reasoning.
  return avgEntryPrice > minEntryPrice && avgEntryPrice < 1 - minEntryPrice;
}

// =============================================================================
// WASH-TRADING SUSPICION SCREEN (2026-07-24)
// =============================================================================
// Motivated by a real research finding, not a hunch: ~15% of volume in some
// Polymarket markets matches self-trading/incentive-farming patterns (CoinDesk,
// Apr 2026), and under 1% of wallets capture ~50% of profit. This is a
// plausible mechanism for Rule 20's own "honest finding" — the 100%-win-rate,
// $0.03-avg-profit wallets that kept surfacing as top "specialists."
//
// Digging into WHY our own ranking is vulnerable to this (not just that it
// might be): rankSpecialistsByCategory() sorts by pnl_t_stat descending, and
// aggregateCategoryScores()'s t-stat calculation gives a wallet whose closes
// are all nearly-identical small positive amounts an EXTREME t-stat (the
// EXTREME_T_STAT_SENTINEL path — see that function's comment, documented
// there for the negative/harm case but symmetric on the positive side). Low-
// variance, tiny, repeated gains — exactly what "economically neutral" wash
// trades look like — is structurally REWARDED, not penalized, by pure t-stat
// ranking. That's the real gap this screen closes.
//
// DEFAULT THRESHOLDS, and why these specific numbers:
// - 0.90 win rate, not the 70% "often a red flag" bar a trader-persistence
//   source (Polyburg, 2026) cites — deliberately more conservative, since
//   this is a WARNING label, not an exclusion (see below), and the goal is
//   minimizing false flags against genuinely disciplined small-edge traders.
// - 20 trades, not MIN_CATEGORY_SAMPLE (5) — an exclusionary/suspicious
//   judgment needs more evidence than an inclusionary one; also well below
//   the same source's cited "200 trades to be bankable," which would almost
//   never fire against today's actual candidate pool sizes.
// - 10% ROI ceiling — roi here (cost-basis-weighted average return per
//   close, not compounded) is already close to "average ROI per trade." The
//   TCA-viable floor for a center-priced market computes to ~6.5% (live-
//   verified during the TCA filter's build) — setting this ceiling at 10%
//   means a candidate can clear TCA (edge survives execution cost) yet still
//   get flagged if that edge is suspiciously thin relative to what a
//   genuine, confident directional bet in a mispriced binary market should
//   net when right. TCA asks "is this big enough to survive costs"; this
//   asks "is this suspiciously small anyway."
//
// REPORT ONLY, same as every discovery/analysis tool in this codebase (Rule
// 20/21): a flagged wallet is annotated in the report, never silently
// dropped from tcaViable — a near-100%-win-rate small-edge wallet COULD be
// genuinely great, this is "look closer," not "auto-exclude."
//
// SCOPE, stated explicitly: this covers the wash-trading signal only (win
// rate + trade count + roi, all data already computed — no new API calls,
// no new RealizedClose fields). A SEPARATE research finding from the same
// pass — wallet age / single-market-concentration as an insider-trading
// signature — needs genuinely new data (first-seen timestamp, position
// concentration) this codebase doesn't fetch today, and is deliberately
// deferred as its own follow-up, not folded in here.
const DEFAULT_WASH_MIN_WIN_RATE = 0.9;
const DEFAULT_WASH_MIN_TRADE_COUNT = 20;
const DEFAULT_WASH_MAX_ROI = 0.1;

export interface WashTradingThresholds {
  minWinRate: number;
  minTradeCount: number;
  maxRoi: number;
}

export const DEFAULT_WASH_TRADING_THRESHOLDS: WashTradingThresholds = {
  minWinRate: DEFAULT_WASH_MIN_WIN_RATE,
  minTradeCount: DEFAULT_WASH_MIN_TRADE_COUNT,
  maxRoi: DEFAULT_WASH_MAX_ROI,
};

/**
 * Pure predicate — mirrors passesTcaFilter's shape exactly. Flags a
 * candidate as wash-trading-suspect when ALL THREE hold: win rate at or
 * above the threshold, trade count at or above the threshold (enough
 * evidence for a suspicious judgment, not just a fluke), AND roi below the
 * ceiling (a near-perfect win rate producing only a thin edge — the
 * "economically neutral position" signature). Directly unit-testable.
 */
export function flagsWashTradingSuspicion(
  candidate: { winRate: number; tradeCount: number; roi: number },
  thresholds: WashTradingThresholds = DEFAULT_WASH_TRADING_THRESHOLDS
): boolean {
  return (
    candidate.winRate >= thresholds.minWinRate &&
    candidate.tradeCount >= thresholds.minTradeCount &&
    candidate.roi < thresholds.maxRoi
  );
}

import { and, gte, notInArray } from "drizzle-orm";
import { db, walletProfile } from "@copybot/db";
import { mapWithConcurrency } from "@copybot/shared";
import { fetchWalletTrades } from "./polymarketDataApi";
import { CATEGORY_TAG_SLUGS, resolveMarketCategory } from "./polymarketCategories";
import { aggregateCategoryScores, reconstructRealizedCloses } from "./scoreWalletCategories";

const DISCOVERY_CONCURRENCY = 5; // matches scanLeaderboard.ts's PASS1_CONCURRENCY precedent
const ROLLING_WINDOW_DAYS = 90; // same window scoreWalletCategories.ts uses

// =============================================================================
// CATEGORY QUOTA SYSTEM (2026-07-24, Rule 24)
// =============================================================================
// Domain-diversification requirement, not just a global top-N: an edge is
// category-specific (a crypto specialist has no proven edge in sports), and
// tracking 20 wallets that all happen to be political analysts means one
// misread poll hits every one of them at once — a correlated bet, not 20
// independent ones, which breaks the independence assumption standard
// position-sizing formulas (including Kelly) implicitly rely on. So instead
// of a global top-20-by-t-stat, this fixes a slot count PER CATEGORY.
//
// DEFAULT_TARGET_CATEGORIES reuses CATEGORY_TAG_SLUGS directly (politics,
// sports, crypto, pop-culture — each independently verified live against a
// real event, see that constant's own comment) rather than inventing a
// parallel list — there is no "Science/Weather" category anywhere in this
// system; Weather is a wholly separate bot/product (packages/weather) with
// its own EV/Kelly pipeline, unrelated to Polymarket-tag-based domains.
const DEFAULT_QUOTA_PER_CATEGORY = 5;
const DEFAULT_TARGET_CATEGORIES = CATEGORY_TAG_SLUGS;

// Positive-evidence mirror of config.py's CATEGORY_SKIP_Z_CRITICAL (1.645) —
// the SAME one-tailed 95%-confidence critical value, just testing the
// opposite tail (is mean PnL significantly GREATER than zero, not less). A
// candidate needs to clear this to be reported as a genuine specialist, not
// just a lucky small sample — the natural symmetric counterpart to Rule 19's
// hard-skip on the harm side.
const Z_CRITICAL = 1.645;

export interface CategoryCandidateResult {
  walletAddress: string;
  category: string;
  score: number;
  winRate: number;
  tradeCount: number;
  avgPnlUsd: number;
  pnlTStat: number;
  roi: number;
  avgEntryPrice: number | null;
  avgFeeRate: number;
  washTradingSuspect: boolean;
}

/**
 * Filters to only STATISTICALLY SIGNIFICANT positive results (pnl_t_stat >=
 * zCritical — null/insignificant t-stats are excluded, not treated as
 * "good") and groups by category — UNCAPPED, deliberately (see this file's
 * module-level "CATEGORY QUOTA SYSTEM" comment): capping here, before TCA
 * gets to see the full pool, is the exact bug this function used to have
 * (rankSpecialistsByCategory, pre-2026-07-24) — a category's top-N-by-t-stat
 * could include TCA-rejected candidates while lower-ranked-but-TCA-viable
 * candidates #N+1... were discarded before TCA ever saw them.
 *
 * Only categories in `targetCategories` are included in the returned map —
 * everything else (i.e. "other") is returned separately via
 * `outsideTargetCategories`, for transparency, not silently dropped.
 *
 * Pure function — no network, no DB — directly unit-testable.
 */
export function filterSignificantByCategory(
  results: CategoryCandidateResult[],
  zCritical: number,
  targetCategories: string[]
): { byCategory: Record<string, CategoryCandidateResult[]>; outsideTargetCategories: CategoryCandidateResult[] } {
  const significant = results.filter((r) => r.pnlTStat >= zCritical);
  const targetSet = new Set(targetCategories);

  const byCategory: Record<string, CategoryCandidateResult[]> = {};
  const outsideTargetCategories: CategoryCandidateResult[] = [];
  for (const r of significant) {
    if (!targetSet.has(r.category)) {
      outsideTargetCategories.push(r);
      continue;
    }
    const list = byCategory[r.category] ?? [];
    list.push(r);
    byCategory[r.category] = list;
  }
  return { byCategory, outsideTargetCategories };
}

/**
 * Sorts one category's (already significance+TCA-filtered) candidates by
 * `(washTradingSuspect ascending, pnlTStat descending)` — a clean,
 * strong-evidence candidate wins a scarce slot over a flagged one at the
 * same or even somewhat higher t-stat, but a flagged candidate can still
 * fill a slot if the category doesn't have enough clean qualifiers (Rule
 * 23's "warning, not auto-exclude" principle still holds — this only
 * affects ORDER, never eligibility). Caps at topN. Pure, directly
 * unit-testable. Called PER CATEGORY, AFTER that category's TCA filtering —
 * never before (see filterSignificantByCategory's docstring).
 */
export function rankAndCapCategory(
  entries: CategoryCandidateResult[],
  topN: number
): CategoryCandidateResult[] {
  return [...entries]
    .sort((a, b) => Number(a.washTradingSuspect) - Number(b.washTradingSuspect) || b.pnlTStat - a.pnlTStat)
    .slice(0, topN);
}

async function scoreOneCandidate(
  walletAddress: string,
  washTradingThresholds: WashTradingThresholds
): Promise<CategoryCandidateResult[]> {
  const startEpochSeconds = Date.now() / 1000 - ROLLING_WINDOW_DAYS * 86400;
  const trades = await fetchWalletTrades(walletAddress, { startEpochSeconds });
  const closes = await reconstructRealizedCloses(
    trades,
    resolveMarketCategory,
    fetchMarketResolutionForDiscovery,
    fetchMarketFeeRateForDiscovery
  );
  const categoryScores = aggregateCategoryScores(closes);

  return Object.entries(categoryScores)
    .filter(([, detail]) => detail.pnl_t_stat !== null)
    .map(([category, detail]) => ({
      walletAddress,
      category,
      score: detail.score,
      winRate: detail.win_rate,
      tradeCount: detail.trade_count,
      avgPnlUsd: detail.avg_pnl_usd,
      pnlTStat: detail.pnl_t_stat as number,
      roi: detail.roi,
      avgEntryPrice: detail.avg_entry_price,
      avgFeeRate: detail.avg_fee_rate,
      washTradingSuspect: flagsWashTradingSuspicion(
        { winRate: detail.win_rate, tradeCount: detail.trade_count, roi: detail.roi },
        washTradingThresholds
      ),
    }));
}

const GAMMA_API_HOST = "https://gamma-api.polymarket.com";
const REQUEST_HEADERS = { "User-Agent": "polymarket-copybot/1.0 (+personal research bot)", Accept: "application/json" };

// Duplicated from scoreWalletCategories.ts's own (unexported) fetchMarketResolution
// rather than importing a private helper — same reasoning as
// polymarket_simulator.py's "small, separate copy on purpose" precedent: this
// is a few lines of a single Gamma call, not worth exporting/coupling two
// scripts' internals over.
async function fetchMarketResolutionForDiscovery(marketSlug: string) {
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
    return null;
  }
}

// Duplicated from scoreWalletCategories.ts's own (unexported)
// fetchMarketFeeRate — same "small, separate copy on purpose" precedent.
async function fetchMarketFeeRateForDiscovery(marketSlug: string): Promise<number> {
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

function parseArgs(): {
  minCompositeScore: number | null;
  excludeAddresses: Set<string>;
  tcaSafetyBufferPct: number;
  tcaMinEntryPrice: number;
  washTradingThresholds: WashTradingThresholds;
  quotaPerCategory: number;
  targetCategories: string[];
} {
  const args = process.argv.slice(2);
  let minCompositeScore: number | null = DEFAULT_MIN_COMPOSITE_SCORE;
  let tcaSafetyBufferPct = DEFAULT_TCA_SAFETY_BUFFER_PCT;
  let tcaMinEntryPrice = DEFAULT_TCA_MIN_ENTRY_PRICE;
  let quotaPerCategory = DEFAULT_QUOTA_PER_CATEGORY;
  let targetCategories = DEFAULT_TARGET_CATEGORIES;
  const washTradingThresholds: WashTradingThresholds = { ...DEFAULT_WASH_TRADING_THRESHOLDS };
  const excludeAddresses = new Set<string>();

  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--all") {
      minCompositeScore = null;
    } else if (args[i] === "--min-composite-score") {
      minCompositeScore = Number(args[i + 1]);
      i++;
    } else if (args[i] === "--exclude" && args[i + 1]) {
      for (const addr of args[i + 1].split(",")) excludeAddresses.add(addr.toLowerCase());
      i++;
    } else if (args[i] === "--tca-safety-buffer" && args[i + 1]) {
      tcaSafetyBufferPct = Number(args[i + 1]);
      i++;
    } else if (args[i] === "--tca-min-entry-price" && args[i + 1]) {
      tcaMinEntryPrice = Number(args[i + 1]);
      i++;
    } else if (args[i] === "--wash-min-win-rate" && args[i + 1]) {
      washTradingThresholds.minWinRate = Number(args[i + 1]);
      i++;
    } else if (args[i] === "--wash-min-trades" && args[i + 1]) {
      washTradingThresholds.minTradeCount = Number(args[i + 1]);
      i++;
    } else if (args[i] === "--wash-max-roi" && args[i + 1]) {
      washTradingThresholds.maxRoi = Number(args[i + 1]);
      i++;
    } else if (args[i] === "--quota-per-category" && args[i + 1]) {
      quotaPerCategory = Number(args[i + 1]);
      i++;
    } else if (args[i] === "--categories" && args[i + 1]) {
      targetCategories = args[i + 1].split(",").map((c) => c.trim());
      i++;
    }
  }
  return {
    minCompositeScore, excludeAddresses, tcaSafetyBufferPct, tcaMinEntryPrice,
    washTradingThresholds, quotaPerCategory, targetCategories,
  };
}

async function main() {
  const {
    minCompositeScore, excludeAddresses, tcaSafetyBufferPct, tcaMinEntryPrice, washTradingThresholds,
    quotaPerCategory, targetCategories,
  } = parseArgs();

  const whereClauses = [];
  if (minCompositeScore !== null) whereClauses.push(gte(walletProfile.compositeScore, minCompositeScore));
  if (excludeAddresses.size > 0) whereClauses.push(notInArray(walletProfile.walletAddress, [...excludeAddresses]));

  const candidates = await db
    .select({ walletAddress: walletProfile.walletAddress })
    .from(walletProfile)
    .where(whereClauses.length > 0 ? and(...whereClauses) : undefined);

  console.log(
    `Discovering category specialists among ${candidates.length} candidate(s) ` +
      `(min composite_score=${minCompositeScore ?? "none (--all)"}) — ` +
      `quota: ${quotaPerCategory} per category × [${targetCategories.join(", ")}] = ` +
      `${quotaPerCategory * targetCategories.length} target slots...`
  );

  const perWalletResults = await mapWithConcurrency(candidates, DISCOVERY_CONCURRENCY, async ({ walletAddress }) => {
    try {
      return await scoreOneCandidate(walletAddress, washTradingThresholds);
    } catch (err) {
      console.warn(`  ${walletAddress}: failed — ${(err as Error).message}`);
      return [];
    }
  });

  const allResults = perWalletResults.flat();
  const { byCategory: statisticallySignificant, outsideTargetCategories } = filterSignificantByCategory(
    allResults,
    Z_CRITICAL,
    targetCategories
  );

  // Entry-price floor runs BEFORE the TCA filter, not alongside it (Rule 27)
  // — estimatedRelativeSpread(avg_entry_price) is a direct input to the TCA
  // formula itself, so a candidate whose entry price isn't trustworthy never
  // gets its TCA verdict computed/trusted at all, rather than being
  // independently double-checked.
  const entryPriceRejected: CategoryCandidateResult[] = [];
  const entryPriceSurvivors: Record<string, CategoryCandidateResult[]> = {};
  for (const [category, entries] of Object.entries(statisticallySignificant)) {
    const survivors = entries.filter((e) => {
      const passes = passesEntryPriceFloor(e.avgEntryPrice, tcaMinEntryPrice);
      if (!passes) entryPriceRejected.push(e);
      return passes;
    });
    if (survivors.length > 0) entryPriceSurvivors[category] = survivors;
  }

  // TCA filter runs AFTER significance filtering but BEFORE ranking/capping
  // (see filterSignificantByCategory's docstring — this ordering is the
  // actual fix for the pre-2026-07-24 bug where capping happened before TCA
  // ever saw the full significant pool). rankAndCapCategory only runs on
  // each category's TCA-survivors, so a quota slot always goes to the best
  // QUALIFIED candidate, never one truncated away before TCA had a say.
  const tcaViable: Record<string, CategoryCandidateResult[]> = {};
  const tcaRejected: CategoryCandidateResult[] = [];
  for (const [category, entries] of Object.entries(entryPriceSurvivors)) {
    const survivors = entries.filter((e) => {
      const passes = passesTcaFilter({ roi: e.roi, avgEntryPrice: e.avgEntryPrice, avgFeeRate: e.avgFeeRate }, tcaSafetyBufferPct);
      if (!passes) tcaRejected.push(e);
      return passes;
    });
    if (survivors.length > 0) tcaViable[category] = rankAndCapCategory(survivors, quotaPerCategory);
  }

  console.log(
    `\n=== Category Quota Discovery (target: ${quotaPerCategory} per category × ` +
      `${targetCategories.length} categories = ${quotaPerCategory * targetCategories.length} slots, ` +
      `TCA safety buffer=${(tcaSafetyBufferPct * 100).toFixed(1)}%) ===`
  );
  let totalFilled = 0;
  for (const category of targetCategories) {
    const entries = tcaViable[category] ?? [];
    totalFilled += entries.length;
    console.log(`\n${category} (${entries.length}/${quotaPerCategory} filled)${entries.length === 0 ? " — no qualifying candidates found" : ":"}`);
    for (const e of entries) {
      const spreadPct = e.avgEntryPrice !== null ? (estimatedRelativeSpread(e.avgEntryPrice) / 2) * 100 : NaN;
      const feePct = e.avgEntryPrice !== null ? e.avgFeeRate * (1 - e.avgEntryPrice) * 100 : NaN;
      // Wash-trading suspicion is a WARNING annotation, not an exclusion —
      // e stays in tcaViable either way (see this file's module-level
      // comment on the wash-trading screen for why this is "look closer,"
      // not "auto-exclude").
      const washWarning = e.washTradingSuspect
        ? " | ⚠ WASH-TRADING SUSPECT (near-perfect win rate, thin edge, large sample — review before tracking)"
        : "";
      console.log(
        `  ${e.walletAddress} | score=${e.score.toFixed(3)} | t_stat=${e.pnlTStat.toFixed(2)} | ` +
          `win_rate=${(e.winRate * 100).toFixed(1)}% | trades=${e.tradeCount} | avg_pnl=$${e.avgPnlUsd.toFixed(2)} | ` +
          `roi=${(e.roi * 100).toFixed(1)}% | est_slippage=${spreadPct.toFixed(1)}% | est_fee=${feePct.toFixed(1)}%${washWarning}`
      );
    }
  }

  console.log(
    `\nTotal slots filled: ${totalFilled} / ${quotaPerCategory * targetCategories.length} across ` +
      `${targetCategories.length} target categories.`
  );

  if (entryPriceRejected.length > 0) {
    console.log(
      `\n--- Rejected: entry price too close to the platform floor (< $${tcaMinEntryPrice.toFixed(3)} or ` +
        `> $${(1 - tcaMinEntryPrice).toFixed(3)} from either extreme) — not a modeled price point, likely ` +
        `not replicable (settlement/resolution-sniping signature, not domain edge) ---`
    );
    for (const e of entryPriceRejected) {
      console.log(
        `  ${e.walletAddress} | ${e.category} | avg_entry_price=$${(e.avgEntryPrice ?? NaN).toFixed(4)} | ` +
          `roi=${(e.roi * 100).toFixed(2)}% | win_rate=${(e.winRate * 100).toFixed(1)}% | trades=${e.tradeCount}`
      );
    }
  }

  if (tcaRejected.length > 0) {
    console.log(`\n--- Rejected on TCA grounds (significant edge too small to survive execution cost) ---`);
    for (const e of tcaRejected) {
      console.log(`  ${e.walletAddress} | ${e.category} | roi=${(e.roi * 100).toFixed(2)}% | t_stat=${e.pnlTStat.toFixed(2)}`);
    }
  }

  if (outsideTargetCategories.length > 0) {
    console.log(
      `\n--- Outside the quota system (category not in [${targetCategories.join(", ")}] — ` +
        `informational only, not counted toward the ${quotaPerCategory * targetCategories.length} slots) ---`
    );
    for (const e of outsideTargetCategories) {
      console.log(`  ${e.walletAddress} | category=${e.category} | t_stat=${e.pnlTStat.toFixed(2)} | roi=${(e.roi * 100).toFixed(2)}%`);
    }
  }

  console.log(
    "\nThis is a REPORT only — nothing was written to wallet_profile.status or config.TRACKED_TRADERS. " +
      "Review and add manually if any of these look worth tracking."
  );
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}
