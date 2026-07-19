// pnpm --filter @copybot/weather calculate:probability <ICAO> <YYYY-MM-DD> [thresholdMinF|none] [thresholdMaxF|none]
//
// The Probability Engine (Joey, 2026-07-20). Queries weather_ensemble_forecast's LATEST
// generation per model for a given station+day, and computes what fraction of the (up to 82)
// ensemble members forecast a daily max temperature inside a target range.
//
// EXTENDED 2026-07-20 to support both metrics: parseMarketThreshold.ts confirmed live that
// Polymarket's "Weather" category has real "lowest-temperature-in-X" events alongside
// "highest-temperature-in-X" ones (e.g. lowest-temperature-in-seoul, lowest-temperature-in-nyc)
// — a "lowest temperature >= 25F" market needs tMinF, not tMaxF; querying the wrong column would
// silently produce a confidently-wrong probability, not an error. `metric` is now a required
// parameter for exactly that reason — no default, so a caller can't forget to specify it.
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
  metric: "max" | "min";
  range: ThresholdRange;
  combined: ProbabilityResult;
  byModel: Record<string, ProbabilityResult>;
}

/** Queries the LATEST ensemble generation (per model, via getLatestIssuedAt so this always
 * matches whatever pruneForecasts.ts considers "current") for one station/day, and computes the
 * probability of the daily max OR min (per `metric`) landing inside `range`. Returns null if the
 * station is unknown or no ensemble data exists yet for this day (distinct from "0% probability"
 * — an absent forecast is not evidence of anything). */
export async function calculateProbability(
  stationExternalId: string,
  forecastFor: string,
  metric: "max" | "min",
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
      .select({ tMaxF: weatherEnsembleForecast.tMaxF, tMinF: weatherEnsembleForecast.tMinF })
      .from(weatherEnsembleForecast)
      .where(
        and(
          eq(weatherEnsembleForecast.stationId, station.id),
          eq(weatherEnsembleForecast.model, model),
          eq(weatherEnsembleForecast.issuedAt, latestIssuedAt),
          eq(weatherEnsembleForecast.forecastFor, forecastFor)
        )
      );

    const values = rows.map((r) => (metric === "max" ? r.tMaxF : r.tMinF));
    if (values.length === 0) continue;
    byModel[model] = computeHitRate(values, range);
    allValues.push(...values);
  }

  if (allValues.length === 0) return null;

  return {
    stationExternalId,
    forecastFor,
    metric,
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
  const [icao, forecastFor, metricArg, minArg, maxArg] = process.argv.slice(2);
  if (!icao || !forecastFor || (metricArg !== "max" && metricArg !== "min")) {
    console.error('Usage: tsx src/calculateProbability.ts <ICAO> <YYYY-MM-DD> <max|min> [thresholdMinF|none] [thresholdMaxF|none]');
    console.error('Example: tsx src/calculateProbability.ts RKSI 2026-07-21 max 80 none   # P(daily max >= 80F)');
    process.exit(1);
  }
  const metric = metricArg;
  const range: ThresholdRange = { min: parseThresholdArg(minArg), max: parseThresholdArg(maxArg) };

  const result = await calculateProbability(icao, forecastFor, metric, range);
  if (!result) {
    console.log(`No ensemble data found for ${icao} / ${forecastFor} — has ingestOpenMeteo.ts been run for this station/day?`);
    return;
  }

  const rangeLabel = `[${range.min ?? "-inf"}, ${range.max ?? "+inf"}]F`;
  console.log(`${result.stationExternalId} / ${result.forecastFor} (${metric}), target range ${rangeLabel}:`);
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
