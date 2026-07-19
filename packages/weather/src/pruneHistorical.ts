// pnpm --filter @copybot/weather prune:historical
//
// The Lean Historical Data rule (docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 3): no multi-year
// mass backfills, ever — weather_historical_observation keeps only a rolling window per station,
// and weather_forecast_snapshot (re-issued forecasts accumulate as a time series, per-station,
// every 3-6h) keeps only enough history to be useful, since a forecast for a date that has
// already passed has zero remaining value the moment that date resolves.
//
// Meant to run daily, paired with the daily historical-obs ingestion job (see
// docs/weather/WEATHER_ARCHITECTURE.md execution-cycle table) — not wired to a scheduler yet
// (no launchd job exists in this repo today), run manually until that's built.

import { lt } from "drizzle-orm";
import { db, weatherForecastSnapshot, weatherHistoricalObservation } from "@copybot/db";

// Rule 3's stated retention: "1 to 2 years." Two years, not one — the wider of the agreed range,
// since climatology (the whole reason this table exists) gets more reliable with more history,
// and this is still a small table at any station count this system will plausibly reach.
export const HISTORICAL_RETENTION_YEARS = 2;

// Forecast snapshots have no long-term analytical value the way historical observations do —
// once forecast_for's date has passed, that row was a prediction about a day that already
// happened and is now purely dead weight. 60 days is generous slack for any future "how did our
// forecasts trend into resolution" review without holding forecast noise indefinitely.
export const FORECAST_SNAPSHOT_RETENTION_DAYS = 60;

function isoDateDaysAgo(days: number): string {
  return new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
}

export function historicalObservationCutoff(now: Date = new Date()): string {
  const cutoff = new Date(now);
  cutoff.setUTCFullYear(cutoff.getUTCFullYear() - HISTORICAL_RETENTION_YEARS);
  return cutoff.toISOString().slice(0, 10);
}

export function forecastSnapshotCutoff(): string {
  return isoDateDaysAgo(FORECAST_SNAPSHOT_RETENTION_DAYS);
}

async function main() {
  const historicalCutoff = historicalObservationCutoff();
  const forecastCutoff = forecastSnapshotCutoff();

  console.log(
    `Pruning weather_historical_observation rows with obs_date < ${historicalCutoff} ` +
      `(${HISTORICAL_RETENTION_YEARS}yr retention)...`
  );
  const historicalResult = await db
    .delete(weatherHistoricalObservation)
    .where(lt(weatherHistoricalObservation.obsDate, historicalCutoff));
  console.log(`  deleted ${historicalResult.rowsAffected ?? "unknown count of"} row(s).`);

  console.log(
    `Pruning weather_forecast_snapshot rows with forecast_for < ${forecastCutoff} ` +
      `(${FORECAST_SNAPSHOT_RETENTION_DAYS}-day retention)...`
  );
  const forecastResult = await db
    .delete(weatherForecastSnapshot)
    .where(lt(weatherForecastSnapshot.forecastFor, forecastCutoff));
  console.log(`  deleted ${forecastResult.rowsAffected ?? "unknown count of"} row(s).`);

  console.log("Done.");
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("prune:historical failed:", err);
    process.exit(1);
  });
}
