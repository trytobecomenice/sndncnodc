// pnpm --filter @copybot/weather update:pnl
//
// The Portfolio Rollup (Joey, 2026-07-20) — takes one snapshot of total paper portfolio equity
// per run and writes it to weather_pnl_snapshot, so the equity curve can be tracked over time.
// Read-only against weather_position / weather_market_odds_snapshot; writes exactly one new
// append-only row.
//
// ACCOUNTING:
//   costBasisOpen        = sum(ourSizeUsd) over OPEN positions
//   markToMarketValue    = sum(currentPrice * ourShares) over OPEN positions, side-adjusted
//                           (a "No" position's current price is 1 - the latest implied Yes prob)
//   unrealizedPnlUsd     = markToMarketValue - costBasisOpen
//   cumulativeRealizedPnlUsd = sum(realizedPnlUsd) over every CLOSED position ever (not just this
//                           period — an equity-curve point represents total portfolio state at
//                           this instant, matching how an equity curve is normally read)
//   availableCashUsd     = WEATHER_PAPER_BANKROLL_USD - costBasisOpen + cumulativeRealizedPnlUsd
//                           (algebraically equivalent to replaying every debit-on-open/
//                           credit-on-close from the start, without needing the full history)
//   totalEquityUsd        = availableCashUsd + markToMarketValue

import { desc, eq } from "drizzle-orm";
import { db, weatherMarketOddsSnapshot, weatherPosition } from "@copybot/db";
import { WEATHER_PAPER_BANKROLL_USD } from "./constants";
import { insertPnlSnapshot } from "./db/writers";

interface OpenPositionForPnl {
  marketSlug: string;
  outcome: "Yes" | "No";
  entryPrice: number;
  ourSizeUsd: number;
  ourShares: number;
}

async function fetchOpenPositionsForPnl(): Promise<OpenPositionForPnl[]> {
  const rows = await db
    .select({
      marketSlug: weatherPosition.marketSlug,
      outcome: weatherPosition.outcome,
      entryPrice: weatherPosition.entryPrice,
      ourSizeUsd: weatherPosition.ourSizeUsd,
      ourShares: weatherPosition.ourShares,
    })
    .from(weatherPosition)
    .where(eq(weatherPosition.status, "open"));
  return rows as OpenPositionForPnl[];
}

interface ClosedPositionSummary {
  cumulativeRealizedPnlUsd: number;
  closedCount: number;
  winCount: number;
}

async function fetchClosedPositionSummary(): Promise<ClosedPositionSummary> {
  const rows = await db
    .select({ realizedPnlUsd: weatherPosition.realizedPnlUsd })
    .from(weatherPosition)
    .where(eq(weatherPosition.status, "closed"));

  let cumulativeRealizedPnlUsd = 0;
  let winCount = 0;
  for (const row of rows) {
    const pnl = row.realizedPnlUsd ?? 0;
    cumulativeRealizedPnlUsd += pnl;
    if (pnl > 0) winCount++;
  }
  return { cumulativeRealizedPnlUsd, closedCount: rows.length, winCount };
}

async function fetchLatestImpliedProb(marketSlug: string): Promise<number | null> {
  const rows = await db
    .select({ impliedProbability: weatherMarketOddsSnapshot.impliedProbability })
    .from(weatherMarketOddsSnapshot)
    .where(eq(weatherMarketOddsSnapshot.marketSlug, marketSlug))
    .orderBy(desc(weatherMarketOddsSnapshot.recordedAt))
    .limit(1);
  return rows[0]?.impliedProbability ?? null;
}

async function main() {
  const openPositions = await fetchOpenPositionsForPnl();
  const closedSummary = await fetchClosedPositionSummary();

  let markToMarketValue = 0;
  let costBasisOpen = 0;

  for (const position of openPositions) {
    const currentYesProb = await fetchLatestImpliedProb(position.marketSlug);
    const currentPrice =
      currentYesProb === null
        ? position.entryPrice // defensive: no odds snapshot yet for this market, assume unchanged
        : position.outcome === "Yes"
          ? currentYesProb
          : 1 - currentYesProb;

    markToMarketValue += currentPrice * position.ourShares;
    costBasisOpen += position.ourSizeUsd;
  }

  const unrealizedPnlUsd = markToMarketValue - costBasisOpen;
  const availableCashUsd = WEATHER_PAPER_BANKROLL_USD - costBasisOpen + closedSummary.cumulativeRealizedPnlUsd;
  const totalEquityUsd = availableCashUsd + markToMarketValue;
  const winRate = closedSummary.closedCount > 0 ? closedSummary.winCount / closedSummary.closedCount : null;

  await insertPnlSnapshot({
    realizedPnlUsd: closedSummary.cumulativeRealizedPnlUsd,
    unrealizedPnlUsd,
    openPositionsCount: openPositions.length,
    winRate,
    availableCashUsd,
    totalEquityUsd,
  });

  console.log("updatePnl: snapshot written.");
  console.log(`  Open positions:      ${openPositions.length}`);
  console.log(
    `  Closed positions:    ${closedSummary.closedCount}` +
      (closedSummary.closedCount > 0 ? ` (${closedSummary.winCount} winners, ${(winRate! * 100).toFixed(1)}% win rate)` : "")
  );
  console.log(`  Cumulative realized: $${closedSummary.cumulativeRealizedPnlUsd.toFixed(2)}`);
  console.log(`  Unrealized (open):   $${unrealizedPnlUsd.toFixed(2)}`);
  console.log(`  Available cash:      $${availableCashUsd.toFixed(2)}`);
  console.log(`  Total equity:        $${totalEquityUsd.toFixed(2)}  (bankroll $${WEATHER_PAPER_BANKROLL_USD})`);
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("update:pnl failed:", err);
    process.exit(1);
  });
}
