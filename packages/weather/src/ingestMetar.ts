// pnpm --filter @copybot/weather ingest:metar
//
// FIRST IMPLEMENTATION SLICE of the Weather Bot (see docs/weather/WEATHER_ARCHITECTURE.md /
// docs/weather/WEATHER_RISK_MANAGEMENT.md for the full design this implements). Purpose: prove
// one real end-to-end DB write on the cheapest, already-verified-working source before building
// anything else — NOAA/Open-Meteo ingestion, market discovery, the Wunderground settlement
// oracle, or any scoring logic all come after this is proven solid.
//
// METAR (aviationweather.gov) is a PREDICTIVE/NOWCAST input only, never the settlement source —
// see docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 5. A same-session reconciliation check found
// METAR disagrees with Wunderground (the real settlement authority) by enough to flip a
// whole-degree-Celsius bucket, so this script's output must never be used to validate a market
// resolution, only to feed climatology_prob/forecast_prob.
//
// SINGLE-FILE FOR NOW, DELIBERATELY: the blueprint's proposed db/writers.ts (a shared
// assertStationExists-style guard, mirroring scoreWallets.ts's upsertWalletProfile pattern) is
// worth building once a SECOND script also writes weather_station/weather_historical_observation
// rows (ingestNoaa.ts, ingestOpenMeteo.ts). Introducing that abstraction for one caller today
// would be structure with no reuse yet — deferred until it has a second caller.

import { and, eq } from "drizzle-orm";
import { db, weatherHistoricalObservation, weatherStation } from "@copybot/db";

const AVIATION_WEATHER_BASE = "https://aviationweather.gov/api/data/metar";

// Aviation Weather Center's METAR API returns no timezone field — ICAO airport identity alone
// doesn't imply one. A real multi-station ingester needs a proper station/timezone database;
// for this single-station proof, a small explicit map is honest about what's actually known
// versus guessed, and fails loudly (not silently to UTC) for any station not yet added here.
const KNOWN_STATION_TIMEZONES: Record<string, string> = {
  RKSI: "Asia/Seoul", // Incheon Intl Airport — the station this slice proves against
};

interface MetarReport {
  icaoId: string;
  obsTime: number; // unix seconds, UTC
  temp: number; // whole degrees Celsius, METAR's native precision
  lat: number;
  lon: number;
  name: string;
  rawOb: string;
}

/**
 * Fetches the last `hours` of METAR reports for one ICAO station.
 * INPUT:  icaoId — a 4-letter ICAO airport code, e.g. "RKSI"
 *         hours  — how far back to look (default 48h, enough to guarantee at least one
 *                  fully-complete UTC calendar day even right after this script's own deploy)
 * OUTPUT: an array of MetarReport, oldest first. Throws on a non-2xx response or unparseable
 *         JSON — this script is a manual one-off proof run, not a scheduled job yet, so failing
 *         loudly here is more useful than failing soft (contrast bot.py's bullpen wrappers,
 *         which fail soft because they're embedded in an always-on loop that must keep going).
 */
async function fetchMetarReports(icaoId: string, hours = 48): Promise<MetarReport[]> {
  const url = `${AVIATION_WEATHER_BASE}?ids=${icaoId}&format=json&hours=${hours}`;
  const response = await fetch(url, {
    headers: {
      // NOAA's own usage guidance asks for an identifying User-Agent so automated traffic
      // isn't mistaken for abuse — a real identity, not spoofing (see
      // docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 5 on why that distinction matters here).
      "User-Agent": "polymarket-copybot-weather (research/paper-trading, non-commercial)",
    },
  });
  if (!response.ok) {
    throw new Error(`aviationweather.gov METAR fetch failed: HTTP ${response.status} for ${icaoId}`);
  }
  const data = (await response.json()) as MetarReport[];
  return data.slice().sort((a, b) => a.obsTime - b.obsTime);
}

/**
 * Picks the most recent FULLY-COMPLETE UTC calendar day present in `reports`, and computes its
 * max/min temperature. "Complete" here means yesterday's UTC date relative to the fetch time —
 * today's date is always partial (the day isn't over), so it's never a fair day-max candidate.
 *
 * NOTE on day-boundary choice: this uses UTC calendar days, not the station's local calendar
 * day. That's a real simplification (Rule 5 in docs/weather/WEATHER_RISK_MANAGEMENT.md already
 * establishes METAR is never used for settlement, so a UTC-vs-local day-boundary mismatch here
 * doesn't carry the same stakes it would for a settlement read) — acceptable for a nowcast/
 * forecast input, flagged explicitly rather than silently assumed correct.
 */
function computeCompleteDayMinMax(
  reports: MetarReport[]
): { obsDate: string; maxC: number; minC: number; dayReports: MetarReport[] } | null {
  if (reports.length === 0) return null;

  const nowUtcDate = new Date().toISOString().slice(0, 10);
  const targetDate = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
  if (targetDate === nowUtcDate) return null; // shouldn't happen, defensive only

  const dayReports = reports.filter((r) => new Date(r.obsTime * 1000).toISOString().slice(0, 10) === targetDate);
  if (dayReports.length === 0) return null;

  const temps = dayReports.map((r) => r.temp);
  return {
    obsDate: targetDate,
    maxC: Math.max(...temps),
    minC: Math.min(...temps),
    dayReports,
  };
}

const cToF = (c: number) => (c * 9) / 5 + 32;

/** Idempotent — matches on weather_station.external_id's unique constraint, so re-running this
 * script never creates duplicate station rows. */
async function upsertMetarStation(icaoId: string, sample: MetarReport): Promise<string> {
  const timezone = KNOWN_STATION_TIMEZONES[icaoId];
  if (!timezone) {
    throw new Error(
      `No known timezone for station ${icaoId} — add it to KNOWN_STATION_TIMEZONES before ` +
        `onboarding a new station (see this file's module comment for why this fails loudly ` +
        `instead of defaulting to UTC).`
    );
  }

  const existing = await db
    .select({ id: weatherStation.id })
    .from(weatherStation)
    .where(and(eq(weatherStation.externalId, icaoId), eq(weatherStation.source, "metar")))
    .limit(1);

  if (existing.length > 0) return existing[0].id;

  const [inserted] = await db
    .insert(weatherStation)
    .values({
      externalId: icaoId,
      name: sample.name,
      source: "metar",
      lat: sample.lat,
      lon: sample.lon,
      timezone,
      notes: "Onboarded by ingestMetar.ts — nowcast/forecast input only, never a settlement source.",
    })
    .returning({ id: weatherStation.id });
  return inserted.id;
}

/** Idempotent — matches on weather_historical_observation's (station_id, obs_date, source)
 * unique index, so re-running this script for a day already written updates it in place rather
 * than duplicating a row. */
async function upsertHistoricalObservation(
  stationId: string,
  obsDate: string,
  maxC: number,
  minC: number,
  dayReports: MetarReport[]
): Promise<void> {
  const values = {
    stationId,
    obsDate,
    tMaxF: cToF(maxC),
    tMinF: cToF(minC),
    precipIn: null, // this METAR feed carries no reliable precip field — future work, not guessed
    conditionCode: null, // ditto — needs real METAR remarks parsing, not attempted in this slice
    source: "metar",
    rawJson: JSON.stringify(dayReports),
  };

  await db
    .insert(weatherHistoricalObservation)
    .values(values)
    .onConflictDoUpdate({
      target: [weatherHistoricalObservation.stationId, weatherHistoricalObservation.obsDate, weatherHistoricalObservation.source],
      set: values,
    });
}

async function main() {
  const icaoId = process.argv[2] ?? "RKSI";
  console.log(`Fetching METAR reports for ${icaoId} (last 48h, via aviationweather.gov)...`);

  const reports = await fetchMetarReports(icaoId, 48);
  console.log(`  ${reports.length} reports fetched, range ${new Date(reports[0].obsTime * 1000).toISOString()} -> ` +
    `${new Date(reports[reports.length - 1].obsTime * 1000).toISOString()}`);

  const dayStats = computeCompleteDayMinMax(reports);
  if (!dayStats) {
    console.error(`No complete UTC day found in the fetched window for ${icaoId} — nothing written.`);
    process.exit(1);
  }
  console.log(
    `  Complete day found: ${dayStats.obsDate} — max ${dayStats.maxC}C (${cToF(dayStats.maxC).toFixed(1)}F), ` +
      `min ${dayStats.minC}C (${cToF(dayStats.minC).toFixed(1)}F), from ${dayStats.dayReports.length} reports`
  );

  const stationId = await upsertMetarStation(icaoId, reports[reports.length - 1]);
  console.log(`  weather_station row: ${stationId} (external_id=${icaoId}, source=metar)`);

  await upsertHistoricalObservation(stationId, dayStats.obsDate, dayStats.maxC, dayStats.minC, dayStats.dayReports);
  console.log(`  weather_historical_observation row written for ${icaoId} / ${dayStats.obsDate}.`);
  console.log("Done.");
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("ingest:metar failed:", err);
    process.exit(1);
  });
}
