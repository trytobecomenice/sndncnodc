// pnpm --filter @copybot/weather check:exits
//
// The Early-Exit Engine (Joey, 2026-07-20) — the "brain for swing trading." Scans every open
// paper position (weather_position, opened by orderBuilder.ts / Rule 15) and decides HOLD to
// settlement or EXIT early, using whatever fresher-than-entry signal checkMarkets.ts has since
// logged. Two exit reasons, both from exitSignals.ts's evaluateExit():
//
//   profit_target             — alpha decay: our edge has thinned below the same 5pp floor
//                                (Rule 7) a new trade would need to clear. The market has
//                                converged toward our view; most of the edge is already captured.
//   stop_loss_model_inversion — a REAL opposing edge has emerged (not noise) — the probability
//                                model has flipped against our held side.
//   stop_loss_temp_buffer     — a fresh ensemble run has moved the point-estimate forecast back
//                                into Rule 12's buffer zone, even before the probability edge
//                                itself has fully flipped. Checked first (see exitSignals.ts).
//
// PAPER ONLY (Rule 1): exiting here writes to weather_position, never a real order. Auto-close was
// an explicit choice (Joey, 2026-07-20) specifically because it's paper — zero real-capital risk,
// and the only way to actually complete the position lifecycle so a closed market can be
// re-entered later by orderBuilder.ts's duplicate-exposure guard.
//
// FRESHNESS: a position is only evaluated against an estimate strictly newer than its own
// openedAt. If checkMarkets.ts hasn't produced a fresher read since entry (e.g. the market has
// gone stale/near-resolution and checkMarkets.ts has stopped logging for it, per the Staleness
// Guard), this correctly falls through to HOLD — there's nothing new to act on, and a
// near-resolution market should ride to real settlement, not be force-exited on stale logic.

import { and, desc, eq } from "drizzle-orm";
import { db, weatherMarketMapping, weatherMarketOddsSnapshot, weatherProbabilityEstimate } from "@copybot/db";
import { type ThresholdRange } from "./calculateProbability";
import { closePaperTradeOrder, fetchOpenPositions } from "./db/writers";
import { computeRealizedPnl, evaluateExit } from "./exitSignals";
import { checkTempBuffer } from "./orderSizing";

const MIN_EDGE_FLOOR = 0.05; // Rule 7's floor, reused as the alpha-decay/exit trigger — see
// exitSignals.ts's module comment for why this isn't a separate, newly-invented constant.
const TEMP_BUFFER_F = 1.5; // Rule 12, same value orderBuilder.ts uses.

interface MappingInfo {
  forecastFor: string;
  targetTempMinF: number | null;
  targetTempMaxF: number | null;
}

async function fetchMapping(marketSlug: string): Promise<MappingInfo | null> {
  const rows = await db
    .select({
      forecastFor: weatherMarketMapping.forecastFor,
      targetTempMinF: weatherMarketMapping.targetTempMinF,
      targetTempMaxF: weatherMarketMapping.targetTempMaxF,
    })
    .from(weatherMarketMapping)
    .where(eq(weatherMarketMapping.marketSlug, marketSlug))
    .limit(1);
  const row = rows[0];
  if (!row || !row.forecastFor) return null;
  return { forecastFor: row.forecastFor, targetTempMinF: row.targetTempMinF, targetTempMaxF: row.targetTempMaxF };
}

interface FreshEstimate {
  estimatedAt: Date;
  edge: number;
  meanForecastF: number;
}

async function fetchFreshestEstimate(marketSlug: string): Promise<FreshEstimate | null> {
  const rows = await db
    .select({
      estimatedAt: weatherProbabilityEstimate.estimatedAt,
      edge: weatherProbabilityEstimate.edge,
      inputsJson: weatherProbabilityEstimate.inputsJson,
    })
    .from(weatherProbabilityEstimate)
    .where(and(eq(weatherProbabilityEstimate.marketSlug, marketSlug), eq(weatherProbabilityEstimate.outcome, "Yes")))
    .orderBy(desc(weatherProbabilityEstimate.estimatedAt))
    .limit(1);

  const row = rows[0];
  if (!row || row.edge === null) return null;

  let meanForecastF: number | null = null;
  try {
    const inputs = JSON.parse(row.inputsJson);
    meanForecastF = typeof inputs?.forecast?.meanForecastF === "number" ? inputs.forecast.meanForecastF : null;
  } catch {
    meanForecastF = null;
  }
  if (meanForecastF === null) return null;

  return { estimatedAt: row.estimatedAt, edge: row.edge, meanForecastF };
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
  const positions = await fetchOpenPositions();
  console.log(`earlyExit: ${positions.length} open position(s) to evaluate.\n`);
  if (positions.length === 0) {
    console.log("No open positions — nothing to do.");
    return;
  }

  let held = 0;
  let noFreshSignal = 0;
  let exitedProfitTarget = 0;
  let exitedStopLoss = 0;
  let skippedNoMapping = 0;
  let skippedNoOdds = 0;

  for (const position of positions) {
    const mapping = await fetchMapping(position.marketSlug);
    if (!mapping) {
      console.log(`SKIP ${position.marketSlug}: no market mapping found (shouldn't happen).`);
      skippedNoMapping++;
      continue;
    }

    const estimate = await fetchFreshestEstimate(position.marketSlug);
    if (!estimate || estimate.estimatedAt <= position.openedAt) {
      console.log(`HOLD ${position.marketSlug}: no fresher signal since entry — riding to settlement.`);
      noFreshSignal++;
      continue;
    }

    const range: ThresholdRange = { min: mapping.targetTempMinF, max: mapping.targetTempMaxF };
    const tempBuffer = checkTempBuffer(estimate.meanForecastF, range, TEMP_BUFFER_F);
    const decision = evaluateExit(position.outcome, estimate.edge, MIN_EDGE_FLOOR, tempBuffer);

    if (!decision.shouldExit) {
      console.log(
        `HOLD ${position.marketSlug}: side edge ${(decision.sideEdge * 100).toFixed(1)}pp still clears the floor, buffer intact.`
      );
      held++;
      continue;
    }

    const currentYesProb = await fetchLatestImpliedProb(position.marketSlug);
    if (currentYesProb === null) {
      console.log(`SKIP ${position.marketSlug}: exit signal fired (${decision.reason}) but no odds snapshot exists yet to price the exit.`);
      skippedNoOdds++;
      continue;
    }

    const exitPrice = position.outcome === "Yes" ? currentYesProb : 1 - currentYesProb;
    const realizedPnlUsd = computeRealizedPnl(position.entryPrice, exitPrice, position.ourShares);

    await closePaperTradeOrder(position.id, exitPrice, realizedPnlUsd, decision.reason!);

    if (decision.reason === "profit_target") exitedProfitTarget++;
    else exitedStopLoss++;

    console.log(
      `EXIT (${decision.reason}) ${position.marketSlug}: entry=${position.entryPrice.toFixed(3)} exit=${exitPrice.toFixed(3)} ` +
        `sideEdge=${(decision.sideEdge * 100).toFixed(1)}pp realizedPnl=$${realizedPnlUsd.toFixed(2)}.`
    );
  }

  console.log("\n=== Summary ===");
  console.log(`Open positions evaluated:     ${positions.length}`);
  console.log(`Held (edge/buffer intact):    ${held}`);
  console.log(`Held (no fresher signal):     ${noFreshSignal}`);
  console.log(`Exited (profit target):       ${exitedProfitTarget}`);
  console.log(`Exited (stop loss):           ${exitedStopLoss}`);
  console.log(`Skipped (no mapping):         ${skippedNoMapping}`);
  console.log(`Skipped (no odds to price exit): ${skippedNoOdds}`);
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("check:exits failed:", err);
    process.exit(1);
  });
}
