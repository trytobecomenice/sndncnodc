// Manual deep-dive diagnostic for discovery candidates — NOT part of the
// discovery pipeline itself, NOT wired into package.json. Reuses the exact
// same reconstruction functions discoverCategorySpecialists.ts/
// scoreWalletCategories.ts already use (no new fetch logic, no duplicated
// PnL math), then computes two things the pipeline's aggregate stats don't
// surface, because a WALLET-level average can hide exactly what this script
// is meant to catch:
//
// 1. Per-CATEGORY entry-price uniformity — the pipeline reports one
//    cost-basis-weighted avg_entry_price per category, which is what caught
//    0xc21ea96b's $0.001-on-every-trade pattern (Rule 27). But an average
//    alone can't distinguish "always exactly $0.001" from "sometimes $0.001,
//    sometimes $0.30, averaging out to something in between" — this computes
//    the actual entry-price distribution (min/max/mean/stddev/coefficient of
//    variation) per category, using each close's OWN implied entry price
//    (costBasisUsd/sharesClosed), not just the aggregate.
// 2. Per-MARKET volume concentration, across the WHOLE wallet (not
//    per-category) — computed directly from raw trades (grouped by
//    trade.slug, summed by dollar volume), since RealizedClose doesn't carry
//    market_slug and was never extended to (a deliberate scope boundary from
//    Rule 21's build — this script computes it separately rather than
//    reaching into the core pipeline for a one-off diagnostic).
//
// Usage: npx tsx src/_wallet_deep_dive.ts 0xWALLET1 0xWALLET2 ...

import { fetchWalletTrades, type RawActivityRecord } from "./polymarketDataApi";
import { resolveMarketCategory } from "./polymarketCategories";
import { aggregateCategoryScores, reconstructRealizedCloses } from "./scoreWalletCategories";

const GAMMA_API_HOST = "https://gamma-api.polymarket.com";
const REQUEST_HEADERS = { "User-Agent": "polymarket-copybot/1.0 (+personal research bot)", Accept: "application/json" };
const ROLLING_WINDOW_DAYS = 90;

async function fetchMarketResolution(marketSlug: string) {
  try {
    const response = await fetch(`${GAMMA_API_HOST}/markets?slug=${encodeURIComponent(marketSlug)}`, { headers: REQUEST_HEADERS });
    if (response.status !== 200) return null;
    const data = (await response.json()) as Array<{ closed?: boolean; outcomes?: string; outcomePrices?: string }>;
    if (!data || data.length === 0) return null;
    const market = data[0];
    return {
      closed: Boolean(market.closed),
      outcomes: JSON.parse(market.outcomes ?? "[]") as string[],
      outcomePrices: (JSON.parse(market.outcomePrices ?? "[]") as string[]).map(Number),
    };
  } catch { return null; }
}

async function fetchMarketFeeRate(marketSlug: string): Promise<number> {
  try {
    const response = await fetch(`${GAMMA_API_HOST}/markets?slug=${encodeURIComponent(marketSlug)}`, { headers: REQUEST_HEADERS });
    if (response.status !== 200) return 0;
    const data = (await response.json()) as Array<{ feesEnabled?: boolean; feeSchedule?: { rate?: number } }>;
    if (!data || data.length === 0) return 0;
    const market = data[0];
    if (!market.feesEnabled) return 0;
    return Number(market.feeSchedule?.rate ?? 0);
  } catch { return 0; }
}

function stats(values: number[]): { min: number; max: number; mean: number; stddev: number; cv: number } {
  const n = values.length;
  const mean = values.reduce((s, x) => s + x, 0) / n;
  const variance = values.reduce((s, x) => s + (x - mean) ** 2, 0) / (n - 1 || 1);
  const stddev = Math.sqrt(variance);
  return { min: Math.min(...values), max: Math.max(...values), mean, stddev, cv: mean > 0 ? stddev / mean : 0 };
}

// =============================================================================
// STRATEGY-TIER CLASSIFICATION (2026-07-24)
// =============================================================================
// Six requested copy-trading strategy profiles, using ONLY data this pipeline already computes.
// Non-copy-trading strategies are outside this repository's scope. NO hold-time/
// execution-speed data exists anywhere in this codebase (RealizedClose
// carries no timestamps) — "Whale"/"Scalper" below are approximated via
// dollar conviction + trade frequency, stated as approximations, not
// pretending to measure holding duration directly. "Momentum" is honestly
// NOT verifiable at all with current data — that tier is relabeled
// "Crypto Specialist" rather than claiming to confirm something it can't.
//
// TAIL-END YIELD FARMER'S CV FLOOR IS A DELIBERATE SAFEGUARD, not part of
// the original request: buying a near-certain outcome at high price right
// before resolution is the MIRROR IMAGE of the 0xc21ea96b floor-sniping
// pattern Rule 27 exists for — Rule 27's ceiling ($0.98) alone would not
// catch a candidate sitting at, say, $0.90 on every single trade. Requiring
// real entry-price variance (CV > 20%) applies the same scrutiny Rule 27
// applies at the low end, at the high end, for this specific tier.
export interface StrategyTierInput {
  category: string;
  winRate: number;
  tradeCount: number;
  avgPnlUsd: number;
  avgEntryPrice: number | null;
  entryPriceCv: number | null; // null = insufficient data (fewer than 2 closes) — never guessed
  categoryCount: number; // how many categories this WALLET clears overall (generalist detection)
  top3ConcentrationPct: number; // whole-wallet, not per-category (see marketConcentration())
}

const POLITICAL_WHALE_MIN_AVG_PNL_USD = 50;
const POLITICAL_WHALE_MAX_TRADE_COUNT = 100;
const SPORTS_SCALPER_MIN_TRADE_COUNT = 100;
const SPORTS_SCALPER_MAX_AVG_PNL_USD = 20;
const GENERALIST_MIN_CATEGORY_COUNT = 3;
const GENERALIST_MAX_TOP3_CONCENTRATION_PCT = 0.3;
const YIELD_FARMER_MIN_WIN_RATE = 0.85;
const YIELD_FARMER_MIN_ENTRY_PRICE = 0.85;
const YIELD_FARMER_MIN_ENTRY_PRICE_CV = 0.2;

/**
 * Pure classification — a candidate can match multiple tiers (e.g. a
 * Generalist that's also individually strong in one category); all matches
 * are returned, never forced into a single label. Directly unit-testable.
 */
export function classifyStrategyTiers(input: StrategyTierInput): string[] {
  const tiers: string[] = [];

  if (
    input.category === "politics" &&
    input.avgPnlUsd > POLITICAL_WHALE_MIN_AVG_PNL_USD &&
    input.tradeCount < POLITICAL_WHALE_MAX_TRADE_COUNT
  ) {
    tiers.push("Political Macro Whale");
  }

  if (
    input.category === "sports" &&
    input.tradeCount > SPORTS_SCALPER_MIN_TRADE_COUNT &&
    input.avgPnlUsd < SPORTS_SCALPER_MAX_AVG_PNL_USD
  ) {
    tiers.push("Sports High-Frequency Scalper");
  }

  if (input.category === "crypto") {
    tiers.push("Crypto Specialist (momentum/execution-speed NOT verified — no data)");
  }

  if (
    input.categoryCount >= GENERALIST_MIN_CATEGORY_COUNT &&
    input.top3ConcentrationPct < GENERALIST_MAX_TOP3_CONCENTRATION_PCT
  ) {
    tiers.push("Cross-Category Quant Generalist");
  }

  if (
    input.winRate > YIELD_FARMER_MIN_WIN_RATE &&
    input.avgEntryPrice !== null &&
    input.avgEntryPrice > YIELD_FARMER_MIN_ENTRY_PRICE &&
    input.entryPriceCv !== null &&
    input.entryPriceCv > YIELD_FARMER_MIN_ENTRY_PRICE_CV
  ) {
    tiers.push("Tail-End Yield Farmer");
  }

  if (input.category === "pop-culture") {
    tiers.push("Pop-Culture Specialist");
  }

  return tiers;
}

function marketConcentration(trades: RawActivityRecord[]): { topMarketSlug: string; topMarketPct: number; top3Pct: number; totalVolumeUsd: number } {
  const volumeBySlug = new Map<string, number>();
  let totalVolumeUsd = 0;
  for (const t of trades) {
    const v = Math.abs(t.usdcSize);
    volumeBySlug.set(t.slug, (volumeBySlug.get(t.slug) ?? 0) + v);
    totalVolumeUsd += v;
  }
  const sorted = [...volumeBySlug.entries()].sort((a, b) => b[1] - a[1]);
  const topMarketSlug = sorted[0]?.[0] ?? "(none)";
  const topMarketPct = totalVolumeUsd > 0 ? (sorted[0]?.[1] ?? 0) / totalVolumeUsd : 0;
  const top3Pct = totalVolumeUsd > 0 ? sorted.slice(0, 3).reduce((s, [, v]) => s + v, 0) / totalVolumeUsd : 0;
  return { topMarketSlug, topMarketPct, top3Pct, totalVolumeUsd };
}

interface DeepDiveRow {
  wallet: string;
  category: string;
  winRate: number;
  tradeCount: number;
  avgPnlUsd: number;
  avgEntryPrice: number | null;
  entryPriceCv: number | null;
  tiers: string[];
}

async function deepDive(wallet: string): Promise<DeepDiveRow[]> {
  console.log(`\n${"=".repeat(80)}\nDEEP DIVE: ${wallet}\n${"=".repeat(80)}`);

  const startEpochSeconds = Date.now() / 1000 - ROLLING_WINDOW_DAYS * 86400;
  const trades = await fetchWalletTrades(wallet, { startEpochSeconds });
  console.log(`raw trades fetched: ${trades.length}`);

  const conc = marketConcentration(trades);
  console.log(
    `\n[Single-market concentration, whole wallet] total volume=$${conc.totalVolumeUsd.toFixed(2)} | ` +
      `top market "${conc.topMarketSlug}" = ${(conc.topMarketPct * 100).toFixed(1)}% of volume | ` +
      `top-3 markets = ${(conc.top3Pct * 100).toFixed(1)}% of volume`
  );

  const closes = await reconstructRealizedCloses(trades, resolveMarketCategory, fetchMarketResolution, fetchMarketFeeRate);
  const scores = aggregateCategoryScores(closes);
  const categoryCount = Object.keys(scores).length;
  const rows: DeepDiveRow[] = [];

  for (const [category, detail] of Object.entries(scores)) {
    const categoryCloses = closes.filter((c) => c.category === category);
    const entryPrices = categoryCloses
      .filter((c) => c.sharesClosed > 0)
      .map((c) => c.costBasisUsd / c.sharesClosed);
    const priceStats = entryPrices.length > 1 ? stats(entryPrices) : null;

    const tiers = classifyStrategyTiers({
      category,
      winRate: detail.win_rate,
      tradeCount: detail.trade_count,
      avgPnlUsd: detail.avg_pnl_usd,
      avgEntryPrice: detail.avg_entry_price,
      entryPriceCv: priceStats?.cv ?? null,
      categoryCount,
      top3ConcentrationPct: conc.top3Pct,
    });

    console.log(`\n--- ${category} ---`);
    console.log(
      `  win_rate=${(detail.win_rate * 100).toFixed(1)}% | trades=${detail.trade_count} | roi=${(detail.roi * 100).toFixed(2)}% | ` +
        `avg_pnl=$${detail.avg_pnl_usd.toFixed(4)} | avg_entry_price=$${(detail.avg_entry_price ?? NaN).toFixed(4)} | t_stat=${detail.pnl_t_stat?.toFixed(2)}`
    );
    if (priceStats) {
      console.log(
        `  [Entry-price distribution] min=$${priceStats.min.toFixed(4)} | max=$${priceStats.max.toFixed(4)} | ` +
          `mean=$${priceStats.mean.toFixed(4)} | stddev=$${priceStats.stddev.toFixed(4)} | ` +
          `CV=${(priceStats.cv * 100).toFixed(1)}%${priceStats.cv < 0.15 ? "  <-- LOW: suspiciously uniform entry prices" : ""}`
      );
    }
    if (detail.win_rate >= 0.95) {
      console.log(`  [Win-rate flag] ${(detail.win_rate * 100).toFixed(1)}% win rate — cross-check ROI and avg_entry_price above for the wash-trading/sniping signature (Rule 23/27).`);
    }
    if (tiers.length > 0) {
      console.log(`  [Strategy tier(s)] ${tiers.join(" | ")}`);
    }

    rows.push({
      wallet, category, winRate: detail.win_rate, tradeCount: detail.trade_count,
      avgPnlUsd: detail.avg_pnl_usd, avgEntryPrice: detail.avg_entry_price,
      entryPriceCv: priceStats?.cv ?? null, tiers,
    });
  }

  return rows;
}

async function main() {
  const wallets = process.argv.slice(2);
  if (wallets.length === 0) {
    console.error("usage: npx tsx src/_wallet_deep_dive.ts 0xWALLET1 0xWALLET2 ...");
    process.exit(1);
  }

  const allRows: DeepDiveRow[] = [];
  for (const wallet of wallets) {
    allRows.push(...(await deepDive(wallet)));
  }

  console.log(`\n${"=".repeat(80)}\nSTRATEGY-TIER SUMMARY (${wallets.length} wallet(s))\n${"=".repeat(80)}`);
  const byTier = new Map<string, DeepDiveRow[]>();
  for (const row of allRows) {
    for (const tier of row.tiers) {
      const list = byTier.get(tier) ?? [];
      list.push(row);
      byTier.set(tier, list);
    }
  }
  if (byTier.size === 0) {
    console.log("No candidate matched any strategy tier.");
  } else {
    for (const [tier, rows] of byTier.entries()) {
      console.log(`\n${tier}:`);
      for (const r of rows) {
        console.log(
          `  ${r.wallet} | ${r.category} | win_rate=${(r.winRate * 100).toFixed(1)}% | trades=${r.tradeCount} | ` +
            `avg_pnl=$${r.avgPnlUsd.toFixed(2)}${r.entryPriceCv !== null ? ` | CV=${(r.entryPriceCv * 100).toFixed(1)}%` : ""}`
        );
      }
    }
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}
