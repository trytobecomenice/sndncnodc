// Shared DB-read helpers for packages/weather, parallel to db/writers.ts. Introduced 2026-07-20
// alongside calculateProbability.ts, which needs "what's the latest ensemble generation for this
// station+model" — the same concept pruneForecasts.ts's retention policy is built around, kept
// consistent in one place so the two can never define "latest" differently from each other.

import { and, desc, eq } from "drizzle-orm";
import { db, weatherEnsembleForecast } from "@copybot/db";

/** Returns the most recent issued_at timestamp for a (station, model) pair's ensemble forecast,
 * or null if no forecast has ever been ingested for that pair. */
export async function getLatestIssuedAt(stationId: string, model: string): Promise<Date | null> {
  const rows = await db
    .select({ issuedAt: weatherEnsembleForecast.issuedAt })
    .from(weatherEnsembleForecast)
    .where(and(eq(weatherEnsembleForecast.stationId, stationId), eq(weatherEnsembleForecast.model, model)))
    .orderBy(desc(weatherEnsembleForecast.issuedAt))
    .limit(1);
  return rows[0]?.issuedAt ?? null;
}
