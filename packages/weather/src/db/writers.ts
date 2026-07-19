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
  weatherPosition,
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

/** True if `marketSlug` already has an open (status='open') paper position — the duplicate-
 * exposure guard orderBuilder.ts uses so re-running it never stacks a second position on a market
 * it's already holding (mirrors the Copy Bot's own duplicate-exposure guard, `bot.py`). */
export async function hasOpenPosition(marketSlug: string): Promise<boolean> {
  const rows = await db
    .select({ id: weatherPosition.id })
    .from(weatherPosition)
    .where(and(eq(weatherPosition.marketSlug, marketSlug), eq(weatherPosition.status, "open")))
    .limit(1);
  return rows.length > 0;
}

export interface OpenPositionRow {
  id: string;
  marketSlug: string;
  outcome: "Yes" | "No";
  openedAt: Date;
  entryPrice: number;
  ourShares: number;
}

/** All currently-open paper positions — earlyExit.ts's entry point (mirrors orderBuilder.ts's
 * fetchActiveMappings as the analogous "what do I need to evaluate this pass" query). */
export async function fetchOpenPositions(): Promise<OpenPositionRow[]> {
  const rows = await db
    .select({
      id: weatherPosition.id,
      marketSlug: weatherPosition.marketSlug,
      outcome: weatherPosition.outcome,
      openedAt: weatherPosition.openedAt,
      entryPrice: weatherPosition.entryPrice,
      ourShares: weatherPosition.ourShares,
    })
    .from(weatherPosition)
    .where(eq(weatherPosition.status, "open"));
  return rows as OpenPositionRow[];
}

/** Closes a paper position (Early-Exit Engine, Joey 2026-07-20) — writes closedAt/realizedPnlUsd/
 * closeReason and flips status to 'closed'. Paper-only (Rule 1): closing here never touches a real
 * order. Once closed, the position no longer blocks orderBuilder.ts's duplicate-exposure guard
 * from re-entering the same market on a later, independent signal. */
export async function closePaperTradeOrder(
  positionId: string,
  exitPrice: number,
  realizedPnlUsd: number,
  closeReason: string
): Promise<void> {
  await db
    .update(weatherPosition)
    .set({ status: "closed", closedAt: new Date(), realizedPnlUsd, closeReason })
    .where(eq(weatherPosition.id, positionId));
}

export interface InsertPaperTradeOrderParams {
  marketSlug: string;
  outcome: "Yes" | "No";
  entryProb: number;
  entryPrice: number;
  sizeUsd: number;
  ensembleProb: number;
  polymarketProb: number;
  probabilityDifference: number;
  isSameDay: boolean;
  stationLocalTime: string;
  tempBufferF: number;
  fullKellyFraction: number;
  appliedFraction: number;
}

/** Writes one paper trade order as an open weather_position row. Reuses the pre-existing
 * weather_position table (Rule 2's own pipeline design: weather_probability_estimate ->
 * weather_position -> weather_pnl_snapshot) rather than a new table — it was already shaped
 * exactly for this (marketSlug/outcome/entryPrice/ourSizeUsd/status), just unused until now. */
export async function insertPaperTradeOrder(params: InsertPaperTradeOrderParams): Promise<void> {
  await db.insert(weatherPosition).values({
    marketSlug: params.marketSlug,
    outcome: params.outcome,
    status: "open",
    entryProb: params.entryProb,
    entryPrice: params.entryPrice,
    ourSizeUsd: params.sizeUsd,
    ourShares: params.sizeUsd / params.entryPrice,
    avgEntryPrice: params.entryPrice,
    isDemoData: false,
    ensembleProb: params.ensembleProb,
    polymarketProb: params.polymarketProb,
    probabilityDifference: params.probabilityDifference,
    isSameDay: params.isSameDay,
    stationLocalTime: params.stationLocalTime,
    tempBufferF: params.tempBufferF,
    fullKellyFraction: params.fullKellyFraction,
    appliedFraction: params.appliedFraction,
  });
}
