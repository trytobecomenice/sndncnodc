// pnpm --filter @copybot/weather prune:forecasts
//
// Data Pruning Engine for weather_ensemble_forecast — closes the retention gap flagged when
// ingestOpenMeteo.ts was built (docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 13): every ingestion
// run is a new forecast generation, un-pruned, and two test runs already produced 70,520 rows
// with zero cleanup.
//
// RETENTION POLICY (Joey, 2026-07-20): keep ONLY the most recent `issued_at` generation per
// (station, model) pair — delete everything older, no rolling time window. Deliberate, not an
// oversight: calculateProbability.ts only ever needs the LATEST distribution to make a trading
// decision right now; older generations have no consumer yet. TRADE-OFF, named explicitly: this
// makes it impossible to later ask "how did our forecast change as the event approached" (a real
// forecast-skill backtest question, e.g. for a future reviewOutcomes.ts) — if that's wanted
// later, this policy needs revisiting toward a short rolling window instead of single-generation.
//
// SAFE AND ATOMIC: the delete is ONE SQL statement — a correlated subquery, not a
// read-latest-then-loop-delete sequence. SQLite makes a single statement atomic by construction,
// so there's no window where a concurrent ingestOpenMeteo.ts run (writing a brand-new generation)
// could race this script into deleting the wrong thing or leaving a half-pruned state — the
// database only ever sees "delete all rows whose issued_at is less than the max issued_at for
// that same station+model," evaluated as one consistent operation, never a partial one.

import { sql } from "drizzle-orm";
import { db, weatherEnsembleForecast, weatherStation } from "@copybot/db";

async function main() {
  const [before] = await db.select({ count: sql<number>`count(*)` }).from(weatherEnsembleForecast);
  console.log(`weather_ensemble_forecast: ${before.count} row(s) before pruning.`);

  const result = await db.delete(weatherEnsembleForecast).where(sql`
    ${weatherEnsembleForecast.issuedAt} < (
      SELECT MAX(t2.issued_at)
      FROM weather_ensemble_forecast t2
      WHERE t2.station_id = ${weatherEnsembleForecast.stationId}
        AND t2.model = ${weatherEnsembleForecast.model}
    )
  `);

  const [after] = await db.select({ count: sql<number>`count(*)` }).from(weatherEnsembleForecast);
  console.log(`Deleted ${result.rowsAffected ?? "?"} row(s) older than each station/model's latest generation.`);
  console.log(`weather_ensemble_forecast: ${after.count} row(s) after pruning.\n`);

  // Verification report — one line per (station, model) generation that survived, so it's
  // visible at a glance that every pair is down to exactly one generation, not silently wrong.
  const remaining = await db
    .select({
      stationId: weatherEnsembleForecast.stationId,
      model: weatherEnsembleForecast.model,
      count: sql<number>`count(*)`,
      issuedAt: sql<number>`max(${weatherEnsembleForecast.issuedAt})`,
    })
    .from(weatherEnsembleForecast)
    .groupBy(weatherEnsembleForecast.stationId, weatherEnsembleForecast.model);

  const stations = await db.select({ id: weatherStation.id, externalId: weatherStation.externalId }).from(weatherStation);
  const externalIdById = new Map(stations.map((s) => [s.id, s.externalId]));

  console.log(`=== Verification: ${remaining.length} (station, model) generation(s) remain ===`);
  for (const r of remaining.sort((a, b) => (externalIdById.get(a.stationId) ?? "").localeCompare(externalIdById.get(b.stationId) ?? ""))) {
    const label = externalIdById.get(r.stationId) ?? r.stationId;
    console.log(`  ${label.padEnd(6)} ${r.model.padEnd(14)} ${r.count} rows, generation issued ${new Date(r.issuedAt * 1000).toISOString()}`);
  }
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("prune:forecasts failed:", err);
    process.exit(1);
  });
}
