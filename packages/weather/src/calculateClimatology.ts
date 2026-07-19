// The climatology half of the EV bridge (Joey, 2026-07-20) — computes a historical base-rate
// probability from weather_historical_observation's 2-year backfill, to satisfy
// weather_probability_estimate.climatology_prob (a required column with no other producer yet)
// and give checkMarkets.ts's blended probability an actual second, independent input, not a
// duplicated/faked number in a column whose name promises something specific.
//
// METHOD: "same time of year, across every backfilled year" — for a target date, look at every
// historical observation within ±WINDOW_DAYS of that same month/day in EVERY year present in the
// backfill (currently 2024 and 2025, plus a partial 2026), and compute what fraction of those
// days' actual max/min landed inside the target range. This is a genuinely small sample (roughly
// (2*WINDOW_DAYS+1) x 2 years ≈ 30 data points with the current 2-year backfill) — a real,
// disclosed limitation, not hidden: this is a starting climatology, not a mature one, and gets
// more trustworthy as more years accumulate.

import { and, eq, inArray } from "drizzle-orm";
import { db, weatherHistoricalObservation, weatherStation } from "@copybot/db";
import { computeHitRate, type ProbabilityResult, type ThresholdRange } from "./calculateProbability";

const WINDOW_DAYS = 7;

/** Generates the set of YYYY-MM-DD date strings within ±windowDays of `forecastFor`'s month/day,
 * across every distinct year already present in `availableYears` — real Date arithmetic (not
 * string month/day matching) so month/year boundaries and leap years are handled correctly. */
export function buildClimatologyWindowDates(forecastFor: string, availableYears: number[], windowDays = WINDOW_DAYS): string[] {
  const [, monthStr, dayStr] = forecastFor.split("-");
  const month = Number(monthStr);
  const day = Number(dayStr);

  const dates = new Set<string>();
  for (const year of availableYears) {
    const center = new Date(Date.UTC(year, month - 1, day));
    for (let offset = -windowDays; offset <= windowDays; offset++) {
      const d = new Date(center);
      d.setUTCDate(d.getUTCDate() + offset);
      dates.add(d.toISOString().slice(0, 10));
    }
  }
  return Array.from(dates);
}

export interface ClimatologyResult {
  stationExternalId: string;
  forecastFor: string;
  metric: "max" | "min";
  range: ThresholdRange;
  windowDays: number;
  yearsUsed: number[];
  result: ProbabilityResult;
}

/** Queries weather_historical_observation for the day-of-year window (see module comment) and
 * computes the historical hit rate against `range`. Returns null if the station is unknown or no
 * historical data exists in the window at all. */
export async function calculateClimatology(
  stationExternalId: string,
  forecastFor: string,
  metric: "max" | "min",
  range: ThresholdRange
): Promise<ClimatologyResult | null> {
  const stationRows = await db
    .select({ id: weatherStation.id })
    .from(weatherStation)
    .where(and(eq(weatherStation.externalId, stationExternalId), eq(weatherStation.source, "metar")))
    .limit(1);
  const station = stationRows[0];
  if (!station) return null;

  // Discover which years actually have data for this station, rather than hardcoding 2024/2025 —
  // stays correct automatically as the backfill window rolls forward (Rule 3's 2yr retention).
  const existingDates = await db
    .select({ obsDate: weatherHistoricalObservation.obsDate })
    .from(weatherHistoricalObservation)
    .where(eq(weatherHistoricalObservation.stationId, station.id));
  const yearsUsed = Array.from(new Set(existingDates.map((r) => Number(r.obsDate.slice(0, 4))))).sort();
  if (yearsUsed.length === 0) return null;

  const windowDates = buildClimatologyWindowDates(forecastFor, yearsUsed);

  const rows = await db
    .select({ tMaxF: weatherHistoricalObservation.tMaxF, tMinF: weatherHistoricalObservation.tMinF })
    .from(weatherHistoricalObservation)
    .where(and(eq(weatherHistoricalObservation.stationId, station.id), inArray(weatherHistoricalObservation.obsDate, windowDates)));

  const values = rows.map((r) => (metric === "max" ? r.tMaxF : r.tMinF)).filter((v): v is number => v !== null);
  if (values.length === 0) return null;

  return {
    stationExternalId,
    forecastFor,
    metric,
    range,
    windowDays: WINDOW_DAYS,
    yearsUsed,
    result: computeHitRate(values, range),
  };
}
