// pnpm --filter @copybot/weather check:markets
//
// The EV Bridge (Joey, 2026-07-20) — wires the "brain" (the 82-member ensemble engine +
// climatology) to the "eyes" (Polymarket's live implied probability) for every actively-reviewed
// market (weather_market_mapping.is_active = true, per Rule 8's human-review gate).
//
// SCOPE, EXPLICIT: this LOGS expected value — it does not decide or execute a trade. Position
// sizing (Rule 11's 5% cap) and the temperature-buffer margin of safety (Rule 12's 1.5°F) are
// deliberately NOT applied here (Joey: "We will implement the actual Order Builder... in the
// phase after this"). A market crossing NOTABLE_EDGE_THRESHOLD below is flagged in the log as a
// signal worth a human look, nothing more — it does not gate anything.
//
// FOR EVERY ACTIVE MARKET, THIS SCRIPT:
//   1. Reads current Polymarket odds (one bullpen discover call, matched against active slugs —
//      reuses the exact same data shape discoverMarkets.ts already proven, no new API pattern).
//   2. Logs an odds-history snapshot (weather_market_odds_snapshot) — the "eyes" half of the
//      buy-low/sell-high early-exit strategy: you can't detect a probability SHIFT without a
//      history of what it used to be.
//   3. Computes climatology_prob (calculateClimatology.ts, 2yr historical base rate) and
//      forecast_prob (calculateProbability.ts, the 82-member ensemble) for the market's own
//      parsed metric/threshold/date.
//   4. Blends them into blendedProb — a SIMPLE, DELIBERATELY UN-SOPHISTICATED v1 formula (equal
//      weight, see BLEND_CLIMATOLOGY_WEIGHT below), flagged clearly as a first-pass heuristic to
//      be refined once there's real outcome data to validate against, not a rigorously-derived
//      optimal weighting.
//   5. edge = blendedProb - marketImpliedProb, written to weather_probability_estimate.

import { and, eq } from "drizzle-orm";
import { runBullpenJson } from "@copybot/bullpen-client";
import { db, weatherMarketMapping, weatherStation } from "@copybot/db";
import { calculateClimatology } from "./calculateClimatology";
import { calculateProbability, type ThresholdRange } from "./calculateProbability";
import { insertProbabilityEstimate, logOddsSnapshot } from "./db/writers";

// v1 heuristic, deliberately simple — see module comment. Weighted toward the ensemble forecast
// (0.65) over pure historical climatology (0.35): a specific multi-model forecast for THIS date
// is more informative than a ~30-point historical base rate, but climatology still anchors the
// blend against a forecast that's wildly out of line with what's normal for the place/season.
const BLEND_CLIMATOLOGY_WEIGHT = 0.35;
const MODEL_VERSION = "checkMarkets-v1-2026-07-20";

// Minimum |edge| to flag a row as a notable signal in the log — does not gate or filter anything
// written to the DB, every active market's EV is logged regardless. 5%, matching the same order
// of magnitude as Rule 11's 5% position-sizing cap — a starting point, not an empirically-derived
// optimal threshold; easy to tune once real outcome data exists to validate against.
const NOTABLE_EDGE_THRESHOLD = 0.05;

interface ActiveMapping {
  marketSlug: string;
  stationExternalId: string;
  metric: "max" | "min" | null;
  forecastFor: string | null;
  targetTempMinF: number | null;
  targetTempMaxF: number | null;
}

async function fetchActiveMappings(): Promise<ActiveMapping[]> {
  const rows = await db
    .select({
      marketSlug: weatherMarketMapping.marketSlug,
      stationExternalId: weatherStation.externalId,
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

/** Re-fetches live Polymarket weather odds (same shape/limit as discoverMarkets.ts) and returns
 * a flat marketSlug -> current "Yes" probability lookup — avoids one bullpen call per market. */
async function fetchCurrentOdds(): Promise<Map<string, number>> {
  const response = await runBullpenJson(
    ["polymarket", "discover", "--category", "weather", "--limit", "100"],
    { retries: 2, retryDelayMs: 500 }
  );
  const lookup = new Map<string, number>();
  for (const event of response?.events ?? []) {
    for (const market of event.markets ?? []) {
      const yes = market.outcomes?.find((o: { name: string }) => o.name === "Yes");
      if (yes) lookup.set(market.slug, yes.probability);
    }
  }
  return lookup;
}

async function main() {
  const mappings = await fetchActiveMappings();
  console.log(`checkMarkets: ${mappings.length} active market mapping(s) to evaluate.\n`);
  if (mappings.length === 0) {
    console.log("No active mappings — nothing to do. (Rule 8: mappings need human review before is_active=true.)");
    return;
  }

  const oddsLookup = await fetchCurrentOdds();

  let evaluated = 0;
  let skippedNoThreshold = 0;
  let skippedNoOdds = 0;
  let skippedNoData = 0;
  let notable = 0;

  for (const mapping of mappings) {
    if (!mapping.metric || !mapping.forecastFor || (mapping.targetTempMinF === null && mapping.targetTempMaxF === null)) {
      console.log(`SKIP ${mapping.marketSlug}: no parsed threshold on this mapping (re-run discoverMarkets.ts to backfill).`);
      skippedNoThreshold++;
      continue;
    }

    const marketImpliedProb = oddsLookup.get(mapping.marketSlug);
    if (marketImpliedProb === undefined) {
      console.log(`SKIP ${mapping.marketSlug}: not found in the current live discover response (expired/resolved?).`);
      skippedNoOdds++;
      continue;
    }
    await logOddsSnapshot(mapping.marketSlug, marketImpliedProb);

    const range: ThresholdRange = { min: mapping.targetTempMinF, max: mapping.targetTempMaxF };
    const [climatology, forecast] = await Promise.all([
      calculateClimatology(mapping.stationExternalId, mapping.forecastFor, mapping.metric, range),
      calculateProbability(mapping.stationExternalId, mapping.forecastFor, mapping.metric, range),
    ]);

    if (!climatology || !forecast) {
      console.log(
        `SKIP ${mapping.marketSlug}: missing ${!climatology ? "climatology" : ""}${!climatology && !forecast ? "/" : ""}` +
          `${!forecast ? "ensemble forecast" : ""} data for ${mapping.stationExternalId}/${mapping.forecastFor}.`
      );
      skippedNoData++;
      continue;
    }

    const climatologyProb = climatology.result.probability;
    const forecastProb = forecast.combined.probability;
    const blendedProb = BLEND_CLIMATOLOGY_WEIGHT * climatologyProb + (1 - BLEND_CLIMATOLOGY_WEIGHT) * forecastProb;
    const edge = blendedProb - marketImpliedProb;

    await insertProbabilityEstimate({
      marketSlug: mapping.marketSlug,
      outcome: "Yes",
      climatologyProb,
      forecastProb,
      blendedProb,
      marketImpliedProb,
      edge,
      modelVersion: MODEL_VERSION,
      inputsJson: {
        metric: mapping.metric,
        range,
        climatology: { probability: climatologyProb, sampleSize: climatology.result.totalCount, yearsUsed: climatology.yearsUsed, windowDays: climatology.windowDays },
        forecast: { probability: forecastProb, byModel: forecast.byModel },
        blendClimatologyWeight: BLEND_CLIMATOLOGY_WEIGHT,
      },
    });

    evaluated++;
    const flag = Math.abs(edge) >= NOTABLE_EDGE_THRESHOLD;
    if (flag) notable++;
    console.log(
      `${flag ? "**NOTABLE**" : "logged    "} ${mapping.marketSlug}: climatology=${(climatologyProb * 100).toFixed(1)}% ` +
        `forecast=${(forecastProb * 100).toFixed(1)}% blended=${(blendedProb * 100).toFixed(1)}% ` +
        `market=${(marketImpliedProb * 100).toFixed(1)}% edge=${(edge * 100).toFixed(1)}pp`
    );
  }

  console.log("\n=== Summary ===");
  console.log(`Active mappings:              ${mappings.length}`);
  console.log(`Evaluated (EV logged):        ${evaluated}`);
  console.log(`Skipped (no parsed threshold):${skippedNoThreshold}`);
  console.log(`Skipped (no current odds):    ${skippedNoOdds}`);
  console.log(`Skipped (no ensemble/climate data): ${skippedNoData}`);
  console.log(`Notable (|edge| >= ${(NOTABLE_EDGE_THRESHOLD * 100).toFixed(0)}%):     ${notable}`);
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("check:markets failed:", err);
    process.exit(1);
  });
}
