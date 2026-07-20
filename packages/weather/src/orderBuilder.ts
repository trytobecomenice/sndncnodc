// pnpm --filter @copybot/weather build:orders
//
// The Order Builder (Joey, 2026-07-20) — the "hands" of the bot. Translates a positive Expected
// Value from checkMarkets.ts's weather_probability_estimate log into a sized paper trade,
// enforcing every rule this codebase has already documented for exactly this step:
//
//   Rule 7  (residual basis-risk margin): edge must clear MIN_EDGE_FLOOR beyond zero, not just be
//           positive — added to this build at Joey's explicit direction, even though it wasn't in
//           her original 3-item scope, since it was already fully specified for this component.
//   Rule 12 (minimum forecast margin of safety): the ensemble's own point-estimate forecast must
//           sit at least TEMP_BUFFER (1.5F) from every strike boundary.
//   Rule 11 (5% capital cap): position size is Quarter-Kelly (KELLY_MULTIPLIER = 0.25 of the full
//           Kelly fraction — Joey's call, since full Kelly over-bets on a probability estimate
//           this codebase's own docs already flag as uncalibrated), hard-capped at
//           MAX_CAPITAL_PER_TRADE against a mock WEATHER_PAPER_BANKROLL_USD capital base. This is
//           the first concrete value for the capital-base constant Rule 11 was blocked on.
//
// PORTFOLIO EXPOSURE CAP (added 2026-07-20, ahead of the forward-test freeze): Rule 11's 5% cap
// bounds any ONE trade, but nothing previously bounded how many positions could be open AT ONCE —
// all exposed to the same underlying model-calibration risk (a systematic mispricing hits every
// open position together, not independently). MAX_PORTFOLIO_EXPOSURE = 25% of total capital,
// tracked as a running total across this run (not just the pre-run DB total) so multiple
// candidates approved in the SAME run can't collectively blow through the cap.
//
// SCOPE: paper trading only (Rule 1) — this writes a weather_position row, it never touches a
// real order or a private key. Re-running is safe: a market that already has an open position is
// skipped (duplicate-exposure guard), matching bot.py's own equivalent guard for the Copy Bot.
//
// STALENESS: re-checked fresh here, independent of whatever checkMarkets.ts recorded when it
// wrote the estimate being read — time may have passed between that estimate and this order-build
// run, and a market that was safely same-day-and-early when the estimate was made could have
// crossed the 18:00 station-local cutoff by the time an order would actually be placed.

import { and, desc, eq } from "drizzle-orm";
import { db, weatherMarketMapping, weatherProbabilityEstimate, weatherStation } from "@copybot/db";
import { type ThresholdRange } from "./calculateProbability";
import { WEATHER_PAPER_BANKROLL_USD } from "./constants";
import { fetchTotalOpenExposureUsd, hasOpenPosition, insertPaperTradeOrder } from "./db/writers";
import {
  applyPortfolioExposureCap,
  checkEdgeFloor,
  checkTempBuffer,
  computeKellyFraction,
  computePositionSize,
} from "./orderSizing";
import { checkStaleness } from "./staleness";

const MIN_EDGE_FLOOR = 0.05; // Rule 7 — 5 percentage points, same order of magnitude as the
// checkMarkets.ts notable-edge threshold and Rule 11's cap; a stated starting value, not yet
// empirically calibrated against real settlement outcomes (none exist yet).
const TEMP_BUFFER_F = 1.5; // Rule 12, Joey's stated default.
const KELLY_MULTIPLIER = 0.25; // Quarter-Kelly, Joey's call 2026-07-20.
const MAX_CAPITAL_PER_TRADE = 0.05; // Rule 11, Joey's stated default.
const MAX_PORTFOLIO_EXPOSURE = 0.25; // Portfolio-level cap, Joey's call 2026-07-20, ahead of the
// forward-test freeze — see module comment.

interface ActiveMapping {
  marketSlug: string;
  timezone: string;
  metric: "max" | "min" | null;
  forecastFor: string | null;
  targetTempMinF: number | null;
  targetTempMaxF: number | null;
}

async function fetchActiveMappings(): Promise<ActiveMapping[]> {
  const rows = await db
    .select({
      marketSlug: weatherMarketMapping.marketSlug,
      timezone: weatherStation.timezone,
      metric: weatherMarketMapping.metric,
      forecastFor: weatherMarketMapping.forecastFor,
      targetTempMinF: weatherMarketMapping.targetTempMinF,
      targetTempMaxF: weatherMarketMapping.targetTempMaxF,
    })
    .from(weatherMarketMapping)
    .innerJoin(weatherStation, eq(weatherMarketMapping.stationId, weatherStation.id))
    .where(and(eq(weatherMarketMapping.isActive, true), eq(weatherStation.source, "wunderground")));
  return rows as ActiveMapping[];
}

interface LatestEstimate {
  blendedProb: number;
  marketImpliedProb: number | null;
  edge: number | null;
  meanForecastF: number | null;
}

async function fetchLatestEstimate(marketSlug: string): Promise<LatestEstimate | null> {
  const rows = await db
    .select({
      blendedProb: weatherProbabilityEstimate.blendedProb,
      marketImpliedProb: weatherProbabilityEstimate.marketImpliedProb,
      edge: weatherProbabilityEstimate.edge,
      inputsJson: weatherProbabilityEstimate.inputsJson,
    })
    .from(weatherProbabilityEstimate)
    .where(and(eq(weatherProbabilityEstimate.marketSlug, marketSlug), eq(weatherProbabilityEstimate.outcome, "Yes")))
    .orderBy(desc(weatherProbabilityEstimate.estimatedAt))
    .limit(1);

  const row = rows[0];
  if (!row) return null;

  let meanForecastF: number | null = null;
  try {
    const inputs = JSON.parse(row.inputsJson);
    meanForecastF = typeof inputs?.forecast?.meanForecastF === "number" ? inputs.forecast.meanForecastF : null;
  } catch {
    meanForecastF = null;
  }

  return { blendedProb: row.blendedProb, marketImpliedProb: row.marketImpliedProb, edge: row.edge, meanForecastF };
}

async function main() {
  const mappings = await fetchActiveMappings();
  console.log(`orderBuilder: ${mappings.length} active market mapping(s) to evaluate.\n`);
  if (mappings.length === 0) {
    console.log("No active mappings — nothing to do.");
    return;
  }

  let ordersPlaced = 0;
  let skippedNoEstimate = 0;
  let skippedStale = 0;
  let skippedDuplicate = 0;
  let skippedEdgeFloor = 0;
  let skippedTempBuffer = 0;
  let skippedZeroSize = 0;
  let skippedPortfolioCap = 0;

  // Running total, not just the pre-run DB figure — updated after every order placed THIS run so
  // multiple candidates approved in the same pass can't collectively exceed the portfolio cap.
  let runningExposureUsd = await fetchTotalOpenExposureUsd();

  for (const mapping of mappings) {
    const estimate = await fetchLatestEstimate(mapping.marketSlug);
    if (!estimate || estimate.marketImpliedProb === null || estimate.edge === null || estimate.meanForecastF === null) {
      console.log(`SKIP ${mapping.marketSlug}: no usable weather_probability_estimate yet (run check:markets first).`);
      skippedNoEstimate++;
      continue;
    }

    const staleness = checkStaleness(mapping.forecastFor!, mapping.timezone);
    if (staleness.isStale) {
      console.log(`SKIP ${mapping.marketSlug}: stale as of order-build time (station-local ${staleness.stationLocalTime}).`);
      skippedStale++;
      continue;
    }

    if (await hasOpenPosition(mapping.marketSlug)) {
      console.log(`SKIP ${mapping.marketSlug}: already has an open paper position (duplicate-exposure guard).`);
      skippedDuplicate++;
      continue;
    }

    if (!checkEdgeFloor(estimate.edge, MIN_EDGE_FLOOR)) {
      console.log(
        `REJECT ${mapping.marketSlug}: edge ${(estimate.edge * 100).toFixed(1)}pp fails Rule 7's ` +
          `${(MIN_EDGE_FLOOR * 100).toFixed(0)}pp floor.`
      );
      skippedEdgeFloor++;
      continue;
    }

    const range: ThresholdRange = { min: mapping.targetTempMinF, max: mapping.targetTempMaxF };
    const tempBuffer = checkTempBuffer(estimate.meanForecastF, range, TEMP_BUFFER_F);
    if (!tempBuffer.passes) {
      console.log(
        `REJECT ${mapping.marketSlug}: forecast ${estimate.meanForecastF.toFixed(1)}F is only ` +
          `${tempBuffer.distanceF.toFixed(1)}F from the strike boundary, fails Rule 12's ${TEMP_BUFFER_F}F buffer.`
      );
      skippedTempBuffer++;
      continue;
    }

    const kelly = computeKellyFraction(estimate.blendedProb, estimate.marketImpliedProb);
    const size = computePositionSize(kelly.fullKellyFraction, KELLY_MULTIPLIER, MAX_CAPITAL_PER_TRADE, WEATHER_PAPER_BANKROLL_USD);
    if (size.sizeUsd <= 0) {
      console.log(`SKIP ${mapping.marketSlug}: computed position size is zero.`);
      skippedZeroSize++;
      continue;
    }

    const cappedSizeUsd = applyPortfolioExposureCap(size.sizeUsd, runningExposureUsd, MAX_PORTFOLIO_EXPOSURE, WEATHER_PAPER_BANKROLL_USD);
    if (cappedSizeUsd <= 0) {
      console.log(
        `SKIP ${mapping.marketSlug}: portfolio exposure cap reached (${(MAX_PORTFOLIO_EXPOSURE * 100).toFixed(0)}% of capital already ` +
          `committed to open positions) — no headroom for a new trade this run.`
      );
      skippedPortfolioCap++;
      continue;
    }
    const wasClamped = cappedSizeUsd < size.sizeUsd;
    const appliedFraction = cappedSizeUsd / WEATHER_PAPER_BANKROLL_USD;

    const isYes = kelly.side === "Yes";
    const entryProb = isYes ? estimate.blendedProb : 1 - estimate.blendedProb;
    const entryPrice = isYes ? estimate.marketImpliedProb : 1 - estimate.marketImpliedProb;

    await insertPaperTradeOrder({
      marketSlug: mapping.marketSlug,
      outcome: kelly.side,
      entryProb,
      entryPrice,
      sizeUsd: cappedSizeUsd,
      ensembleProb: estimate.blendedProb,
      polymarketProb: estimate.marketImpliedProb,
      probabilityDifference: estimate.edge,
      isSameDay: staleness.isSameDay,
      stationLocalTime: staleness.stationLocalTime,
      tempBufferF: tempBuffer.distanceF,
      fullKellyFraction: kelly.fullKellyFraction,
      appliedFraction,
    });

    runningExposureUsd += cappedSizeUsd;
    ordersPlaced++;
    console.log(
      `ORDER ${mapping.marketSlug}: BUY ${kelly.side.toUpperCase()} $${cappedSizeUsd.toFixed(2)} ` +
        `(${(appliedFraction * 100).toFixed(2)}% of capital${wasClamped ? ", clamped by portfolio cap" : ""}) — ` +
        `edge=${(estimate.edge * 100).toFixed(1)}pp, fullKelly=${(kelly.fullKellyFraction * 100).toFixed(1)}%, ` +
        `tempBuffer=${tempBuffer.distanceF.toFixed(1)}F, ${staleness.isSameDay ? "same-day" : "future-day"}.`
    );
  }

  console.log("\n=== Summary ===");
  console.log(`Active mappings:              ${mappings.length}`);
  console.log(`Orders placed:                ${ordersPlaced}`);
  console.log(`Skipped (no estimate yet):    ${skippedNoEstimate}`);
  console.log(`Skipped (stale):              ${skippedStale}`);
  console.log(`Skipped (duplicate position): ${skippedDuplicate}`);
  console.log(`Rejected (Rule 7 edge floor): ${skippedEdgeFloor}`);
  console.log(`Rejected (Rule 12 temp buffer): ${skippedTempBuffer}`);
  console.log(`Skipped (zero size):          ${skippedZeroSize}`);
  console.log(`Skipped (portfolio cap):      ${skippedPortfolioCap}`);
  console.log(`Total exposure after this run: $${runningExposureUsd.toFixed(2)} (${((runningExposureUsd / WEATHER_PAPER_BANKROLL_USD) * 100).toFixed(1)}% of capital)`);
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("build:orders failed:", err);
    process.exit(1);
  });
}
