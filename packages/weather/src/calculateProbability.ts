// pnpm --filter @copybot/weather calculate:probability <ICAO> <YYYY-MM-DD> [thresholdMinF|none] [thresholdMaxF|none]
//
// The Probability Engine (Joey, 2026-07-20). Queries weather_ensemble_forecast's LATEST
// generation per model for a given station+day, and computes what fraction of the (up to 82)
// ensemble members forecast a daily max temperature inside a target range.
//
// SCOPE, DELIBERATE: this is the probability MATH, verified against real data — it is NOT YET
// wired to real Polymarket market thresholds. weather_market_mapping has no stored min/max
// bucket columns today (a market's threshold currently only exists implicitly in its slug text,
// e.g. "...-80-81f" or "...-22c"); parsing that into a real ThresholdRange is a distinct next
// step, most naturally checkMarkets.ts's job once it exists — not attempted here so this module
// stays a clean, independently-testable building block rather than getting tangled up with
// slug-parsing edge cases in the same file. Also NOT yet writing to weather_probability_estimate
// (that table is keyed by market_slug/outcome, which this module doesn't have yet for the same
// reason). Exported as a clean function specifically so wiring both of those in later is additive,
// not a rewrite.
//
// BOTH MODELS BROKEN OUT, NOT JUST COMBINED: Rule 13's entire justification for an ensemble
// (docs/weather/WEATHER_RISK_MANAGEMENT.md) is that models can genuinely disagree — RKSI's real
// next-day forecast already showed ecmwf_ifs025 at ~22% vs. gfs_seamless at ~3% for the same
// threshold. Collapsing that into one blended number by default would hide exactly the signal
// this architecture was built to surface, so both are always returned.

import { and, eq } from "drizzle-orm";
import { db, weatherEnsembleForecast, weatherStation } from "@copybot/db";
import { getLatestIssuedAt } from "./db/queries";

const MODELS = ["ecmwf_ifs025", "gfs_seamless"];

export interface ThresholdRange {
  /** Inclusive lower bound in Fahrenheit, or null for an open-ended "X or below" bucket. */
  min: number | null;
  /** Inclusive upper bound in Fahrenheit, or null for an open-ended "X or higher" bucket. */
  max: number | null;
}

export interface ProbabilityResult {
  hitCount: number;
  totalCount: number;
  /** 0..1. 0 when totalCount is 0 — "no data" and "definitely won't happen" are different
   * things; callers must check totalCount before trusting probability, not assume 0 means "no". */
  probability: number;
}

/** Pure function: what fraction of `values` fall inside `range` (inclusive at both bounds where
 * set). No I/O — fully unit-testable against synthetic ensemble output without touching the DB. */
export function computeHitRate(values: number[], range: ThresholdRange): ProbabilityResult {
  if (values.length === 0) return { hitCount: 0, totalCount: 0, probability: 0 };
  const hits = values.filter((v) => (range.min === null || v >= range.min) && (range.max === null || v <= range.max));
  return { hitCount: hits.length, totalCount: values.length, probability: hits.length / values.length };
}

export interface StationProbability {
  stationExternalId: string;
  forecastFor: string;
  range: ThresholdRange;
  combined: ProbabilityResult;
  byModel: Record<string, ProbabilityResult>;
}

/** Queries the LATEST ensemble generation (per model, via getLatestIssuedAt so this always
 * matches whatever pruneForecasts.ts considers "current") for one station/day, and computes the
 * probability of the daily max landing inside `range`. Returns null if the station is unknown or
 * no ensemble data exists yet for this day (distinct from "0% probability" — an absent forecast
 * is not evidence of anything). */
export async function calculateProbability(
  stationExternalId: string,
  forecastFor: string,
  range: ThresholdRange
): Promise<StationProbability | null> {
  const stationRows = await db
    .select({ id: weatherStation.id })
    .from(weatherStation)
    .where(and(eq(weatherStation.externalId, stationExternalId), eq(weatherStation.source, "metar")))
    .limit(1);
  const station = stationRows[0];
  if (!station) return null;

  const allValues: number[] = [];
  const byModel: Record<string, ProbabilityResult> = {};

  for (const model of MODELS) {
    const latestIssuedAt = await getLatestIssuedAt(station.id, model);
    if (!latestIssuedAt) continue;

    const rows = await db
      .select({ tMaxF: weatherEnsembleForecast.tMaxF })
      .from(weatherEnsembleForecast)
      .where(
        and(
          eq(weatherEnsembleForecast.stationId, station.id),
          eq(weatherEnsembleForecast.model, model),
          eq(weatherEnsembleForecast.issuedAt, latestIssuedAt),
          eq(weatherEnsembleForecast.forecastFor, forecastFor)
        )
      );

    const values = rows.map((r) => r.tMaxF);
    if (values.length === 0) continue;
    byModel[model] = computeHitRate(values, range);
    allValues.push(...values);
  }

  if (allValues.length === 0) return null;

  return {
    stationExternalId,
    forecastFor,
    range,
    combined: computeHitRate(allValues, range),
    byModel,
  };
}

function parseThresholdArg(v: string | undefined): number | null {
  if (!v || v.toLowerCase() === "none") return null;
  const n = Number(v);
  if (!Number.isFinite(n)) throw new Error(`invalid threshold value: "${v}"`);
  return n;
}

async function main() {
  const [icao, forecastFor, minArg, maxArg] = process.argv.slice(2);
  if (!icao || !forecastFor) {
    console.error('Usage: tsx src/calculateProbability.ts <ICAO> <YYYY-MM-DD> [thresholdMinF|none] [thresholdMaxF|none]');
    console.error('Example: tsx src/calculateProbability.ts RKSI 2026-07-21 80 none   # P(max >= 80F)');
    process.exit(1);
  }
  const range: ThresholdRange = { min: parseThresholdArg(minArg), max: parseThresholdArg(maxArg) };

  const result = await calculateProbability(icao, forecastFor, range);
  if (!result) {
    console.log(`No ensemble data found for ${icao} / ${forecastFor} — has ingestOpenMeteo.ts been run for this station/day?`);
    return;
  }

  const rangeLabel = `[${range.min ?? "-inf"}, ${range.max ?? "+inf"}]F`;
  console.log(`${result.stationExternalId} / ${result.forecastFor}, target range ${rangeLabel}:`);
  console.log(
    `  Combined: ${result.combined.hitCount}/${result.combined.totalCount} members ` +
      `(${(result.combined.probability * 100).toFixed(1)}%)`
  );
  for (const [model, r] of Object.entries(result.byModel)) {
    console.log(`  ${model}: ${r.hitCount}/${r.totalCount} members (${(r.probability * 100).toFixed(1)}%)`);
  }
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("calculate:probability failed:", err);
    process.exit(1);
  });
}
