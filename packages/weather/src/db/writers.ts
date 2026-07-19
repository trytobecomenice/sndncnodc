// Shared DB-write helpers for packages/weather. Introduced 2026-07-19, at the exact point
// deferred in ingestMetar.ts's original module comment: "worth building once a SECOND script
// also writes weather_station rows" — discoverMarkets.ts is that second script. Mirrors
// scoreWallets.ts's single-writer-funnel pattern (one function per table is the only place that
// writes it) and serves as the application-level existence-check this domain uses instead of
// database-level foreign keys (docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 9).

import { and, eq } from "drizzle-orm";
import {
  db,
  weatherMarketMapping,
  weatherMarketOddsSnapshot,
  weatherProbabilityEstimate,
  weatherStation,
} from "@copybot/db";

export interface StationRow {
  id: string;
  externalId: string;
  name: string;
  source: string;
  lat: number;
  lon: number;
  timezone: string;
}

/** Looks up a station by external_id, ANY source — used when one script wants to borrow known
 * coordinates for a real-world location that a DIFFERENT source already onboarded (e.g.
 * discoverMarkets.ts reusing ingestMetar.ts's METAR-sourced lat/lon for a Wunderground station
 * row referring to the same physical airport). Returns the first match; callers that care about
 * a specific source should filter the result or use a more specific query. */
export async function findStationByExternalId(externalId: string): Promise<StationRow | null> {
  const rows = await db
    .select()
    .from(weatherStation)
    .where(eq(weatherStation.externalId, externalId))
    .limit(1);
  return rows[0] ?? null;
}

export interface UpsertStationParams {
  externalId: string;
  name: string;
  source: string;
  lat: number;
  lon: number;
  timezone: string;
  notes?: string;
}

/** Idempotent — matches on (external_id, source), so the same real-world location can have one
 * row per source (per the source-scoped station identity design in
 * docs/weather/WEATHER_ARCHITECTURE.md §1) without colliding, and re-running any ingester never
 * creates duplicates. */
export async function upsertWeatherStation(params: UpsertStationParams): Promise<string> {
  const existing = await db
    .select({ id: weatherStation.id })
    .from(weatherStation)
    .where(and(eq(weatherStation.externalId, params.externalId), eq(weatherStation.source, params.source)))
    .limit(1);

  if (existing.length > 0) return existing[0].id;

  const [inserted] = await db
    .insert(weatherStation)
    .values({
      externalId: params.externalId,
      name: params.name,
      source: params.source,
      lat: params.lat,
      lon: params.lon,
      timezone: params.timezone,
      notes: params.notes ?? null,
    })
    .returning({ id: weatherStation.id });
  return inserted.id;
}

export interface UpsertMarketMappingParams {
  marketSlug: string;
  stationId: string;
  settlementSource: string;
  settlementRule: string;
  /** Defaults to false — Rule 8 (docs/weather/WEATHER_RISK_MANAGEMENT.md) requires human review
   * before a mapping is trusted. No caller should pass true without that review having happened. */
  isActive?: boolean;
  /** All four added 2026-07-20 for the EV bridge (parseMarketThreshold.ts). Null when a market's
   * slug doesn't match a recognized temperature-bucket shape (e.g. the non-station "Weather"
   * markets — air quality, earthquakes — confirmed live to exist). */
  metric?: "max" | "min" | null;
  forecastFor?: string | null;
  targetTempMinF?: number | null;
  targetTempMaxF?: number | null;
}

/** Idempotent — matches on market_slug's unique constraint. Re-running discoverMarkets.ts
 * updates the mapping (e.g. a corrected settlement_rule, or backfilling the threshold columns
 * onto a row written before parseMarketThreshold.ts existed) rather than duplicating it, but
 * deliberately does NOT overwrite is_active on conflict — a human's prior approval must never be
 * silently reset by a routine re-scan. */
export async function upsertMarketMapping(params: UpsertMarketMappingParams): Promise<void> {
  const values = {
    marketSlug: params.marketSlug,
    stationId: params.stationId,
    settlementSource: params.settlementSource,
    settlementRule: params.settlementRule,
    isActive: params.isActive ?? false,
    metric: params.metric ?? null,
    forecastFor: params.forecastFor ?? null,
    targetTempMinF: params.targetTempMinF ?? null,
    targetTempMaxF: params.targetTempMaxF ?? null,
  };

  await db
    .insert(weatherMarketMapping)
    .values(values)
    .onConflictDoUpdate({
      target: weatherMarketMapping.marketSlug,
      set: {
        stationId: values.stationId,
        settlementSource: values.settlementSource,
        settlementRule: values.settlementRule,
        metric: values.metric,
        forecastFor: values.forecastFor,
        targetTempMinF: values.targetTempMinF,
        targetTempMaxF: values.targetTempMaxF,
        // isActive deliberately omitted from the update set — see doc comment above.
      },
    });
}

/** Append-only — logs one point-in-time reading of a market's implied "Yes" probability. Never
 * upserted/deduplicated: the whole point is a time series, per-observation, for the early-exit
 * strategy's odds-shift detection (Joey, 2026-07-20). */
export async function logOddsSnapshot(marketSlug: string, impliedProbability: number): Promise<void> {
  await db.insert(weatherMarketOddsSnapshot).values({ marketSlug, impliedProbability });
}

export interface UpsertProbabilityEstimateParams {
  marketSlug: string;
  outcome: string;
  climatologyProb: number;
  forecastProb: number | null;
  blendedProb: number;
  marketImpliedProb: number | null;
  edge: number | null;
  modelVersion: string;
  inputsJson: Record<string, unknown>;
}

/** Append-only, matching the existing append-only research-log pattern already used for
 * leaderboard_scan and weather_probability_estimate's own original design — every checkMarkets.ts
 * pass writes a new row rather than overwriting the last one, so the EV history over time is
 * preserved (needed for the early-exit strategy just as much as the raw odds history is). */
export async function insertProbabilityEstimate(params: UpsertProbabilityEstimateParams): Promise<void> {
  await db.insert(weatherProbabilityEstimate).values({
    marketSlug: params.marketSlug,
    outcome: params.outcome,
    climatologyProb: params.climatologyProb,
    forecastProb: params.forecastProb,
    blendedProb: params.blendedProb,
    marketImpliedProb: params.marketImpliedProb,
    edge: params.edge,
    modelVersion: params.modelVersion,
    inputsJson: JSON.stringify(params.inputsJson),
  });
}
