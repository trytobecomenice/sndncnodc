// pnpm --filter @copybot/weather backfill:historical
//
// Global historical backfill across the full fleet of onboarded stations — building real
// climatology "memory" for the probability model that doesn't exist yet (Joey, 2026-07-19).
//
// WHY THIS IS A SEPARATE SCRIPT, NOT A REFACTOR OF ingestMetar.ts: the original ask was to
// refactor ingestMetar.ts itself, but this is a genuinely different job on a genuinely different
// data source, run in a genuinely different mode — ingestMetar.ts is a small, fast, DAILY
// incremental job against the LIVE aviationweather.gov feed for one station at a time (CLI arg);
// this is a one-time (or occasionally re-run), slow, MULTI-STATION bulk job against a historical
// ARCHIVE. Merging them would conflate two different operational shapes into one file, against
// this codebase's established one-script-one-job convention (scanLeaderboard.ts vs.
// scoreWallets.ts, ingestMetar.ts vs. pruneHistorical.ts, ...).
//
// WHY THE DATA SOURCE CHANGED: verified live (2026-07-19) that aviationweather.gov's
// `/api/data/metar` endpoint — what ingestMetar.ts uses — physically cannot serve more than
// ~8-9 days of history (hours=750 is the max the API accepts; even then it returns only the most
// recent ~400 records). It is a live "recent conditions" feed, not an archive — no amount of
// pacing or chunking gets around a hard server-side ceiling. The real free, public, ToS-intended
// source for multi-year historical METAR is the Iowa Environmental Mesonet's ASOS archive
// (mesonet.agron.iastate.edu, Iowa State University) — verified live: a SINGLE request for
// RKSI's full 2-year range returned 44,626 real readings, and the same station-code query works
// unchanged for both US (KLGA/KORD) and non-US (RKSI/EGLC/WSSS) stations — the "LGA"/"ORD" labels
// IEM returns in its response are cosmetic display names only, not something callers need to
// remap. Same underlying METAR data as ingestMetar.ts, just a different, archive-capable access
// point — so results here are written with the same `source: "metar"` tag, sharing the identical
// `(station_id, obs_date, source)` upsert target a future ingestMetar.ts run would also use.
//
// WINDOW: exactly 2 years, matching Rule 3 (docs/weather/WEATHER_RISK_MANAGEMENT.md) — NOT the
// 2.5 years originally requested. Joey's own call (2026-07-19): backfilling past the documented
// retention window would just have pruneHistorical.ts delete the oldest ~6 months right back out
// the next time it runs. Reuses pruneHistorical.ts's own cutoff function so the two can never
// drift out of sync with each other.
//
// PACING: a real risk, not a formality — this script got rate-limited by IEM ("Too many requests
// from your IP address, slow down.") after just TWO quick manual test requests. BASE_DELAY_MS is
// deliberately more conservative than the 2-3s first suggested, and the rate-limit response text
// is detected explicitly and backed off from (not just slept through) — see fetchStationCsv.

import { and, eq, gte, sql } from "drizzle-orm";
import { db, weatherHistoricalObservation, weatherStation } from "@copybot/db";
import { historicalObservationCutoff } from "./pruneHistorical";

const IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py";
const BASE_DELAY_MS = 5000;
const RATE_LIMIT_BACKOFF_MS = 60000;
const RATE_LIMIT_MAX_RETRIES = 3;
const SOURCE = "metar"; // same underlying data as ingestMetar.ts's live feed, see module comment

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

interface AsosReading {
  utcIso: string; // "YYYY-MM-DD HH:MM"
  tempF: number;
}

/**
 * Fetches the FULL date-range CSV for one station in a single request (IEM supports arbitrary
 * ranges natively — verified live, unlike aviationweather.gov's ~8-9-day ceiling).
 * Retries with a long backoff specifically on IEM's rate-limit response text, up to
 * RATE_LIMIT_MAX_RETRIES times, before giving up and letting the caller treat it as a failure.
 */
async function fetchStationCsv(icaoId: string, startDate: string, endDate: string): Promise<string> {
  const [y1, m1, d1] = startDate.split("-");
  const [y2, m2, d2] = endDate.split("-");
  const url =
    `${IEM_BASE}?station=${icaoId}&data=tmpf&year1=${y1}&month1=${m1}&day1=${d1}` +
    `&year2=${y2}&month2=${m2}&day2=${d2}&tz=Etc/UTC&format=onlycomma&latlon=no&elev=no&missing=null&trace=null`;

  for (let attempt = 1; attempt <= RATE_LIMIT_MAX_RETRIES; attempt++) {
    const response = await fetch(url, {
      headers: { "User-Agent": "polymarket-copybot-weather (research/paper-trading, non-commercial)" },
    });
    const text = await response.text();

    if (text.includes("Too many requests") || text.includes("slow down")) {
      console.warn(
        `    rate-limited by IEM (attempt ${attempt}/${RATE_LIMIT_MAX_RETRIES}) — backing off ${RATE_LIMIT_BACKOFF_MS / 1000}s...`
      );
      if (attempt === RATE_LIMIT_MAX_RETRIES) {
        throw new Error(`IEM rate-limited this request ${RATE_LIMIT_MAX_RETRIES} times in a row, giving up on ${icaoId}`);
      }
      await sleep(RATE_LIMIT_BACKOFF_MS);
      continue;
    }
    if (!response.ok) {
      throw new Error(`IEM ASOS fetch failed: HTTP ${response.status} for ${icaoId}`);
    }
    return text;
  }
  throw new Error(`unreachable`); // satisfies TS — the loop above always returns or throws
}

/** Parses IEM's onlycomma CSV format, skipping the header row and any "null" (missing) readings. */
function parseAsosCsv(csv: string): AsosReading[] {
  const lines = csv.trim().split("\n");
  const readings: AsosReading[] = [];
  for (const line of lines.slice(1)) {
    const parts = line.split(",");
    if (parts.length < 3) continue;
    const [, utcIso, tempFRaw] = parts;
    if (tempFRaw === "null" || tempFRaw === "" || tempFRaw === undefined) continue;
    const tempF = Number(tempFRaw);
    if (!Number.isFinite(tempF)) continue;
    readings.push({ utcIso: utcIso.trim(), tempF });
  }
  return readings;
}

/**
 * Groups readings by the STATION'S OWN LOCAL calendar day (not UTC — a genuine improvement over
 * ingestMetar.ts's UTC-day simplification, which was explicitly justified there only because
 * METAR is a nowcast-only input, never settlement; this backfill is meant to be real climatology
 * "memory," so it's worth doing the more correct local-day aggregation here since we already have
 * each station's real resolved timezone on hand).
 */
function aggregateByLocalDay(
  readings: AsosReading[],
  timezone: string
): Map<string, { maxF: number; minF: number; readings: AsosReading[] }> {
  const formatter = new Intl.DateTimeFormat("en-CA", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit" });
  const byDay = new Map<string, AsosReading[]>();

  for (const r of readings) {
    // IEM's "onlycomma" timestamps are UTC (tz=Etc/UTC was requested explicitly) — append Z so
    // Date parses them as UTC, not local-to-this-process.
    const utcDate = new Date(r.utcIso.replace(" ", "T") + "Z");
    const localDateStr = formatter.format(utcDate); // en-CA locale gives YYYY-MM-DD directly
    const bucket = byDay.get(localDateStr);
    if (bucket) bucket.push(r);
    else byDay.set(localDateStr, [r]);
  }

  const result = new Map<string, { maxF: number; minF: number; readings: AsosReading[] }>();
  for (const [day, dayReadings] of byDay) {
    const temps = dayReadings.map((r) => r.tempF);
    result.set(day, { maxF: Math.max(...temps), minF: Math.min(...temps), readings: dayReadings });
  }
  return result;
}

/** Counts how many days in [startDate, endDate] this station already has, for the resume
 * optimization below — re-running this script (e.g. after an interruption) shouldn't re-fetch
 * a station that's already fully backfilled, both for speed and to conserve IEM's rate-limit
 * budget on every re-run. */
async function countExistingDays(stationId: string, startDate: string): Promise<number> {
  const rows = await db
    .select({ count: sql<number>`count(*)` })
    .from(weatherHistoricalObservation)
    .where(
      and(
        eq(weatherHistoricalObservation.stationId, stationId),
        eq(weatherHistoricalObservation.source, SOURCE),
        gte(weatherHistoricalObservation.obsDate, startDate)
      )
    );
  return rows[0]?.count ?? 0;
}

/** Idempotent by construction: onConflictDoUpdate on the (station_id, obs_date, source) unique
 * index means a re-run of this script — interrupted, resumed, or simply repeated — can never
 * create a duplicate row; it just overwrites the same row with the same (or corrected) values. */
async function upsertDay(
  stationId: string,
  obsDate: string,
  maxF: number,
  minF: number,
  dayReadings: AsosReading[]
): Promise<void> {
  const values = {
    stationId,
    obsDate,
    tMaxF: maxF,
    tMinF: minF,
    precipIn: null,
    conditionCode: null,
    source: SOURCE,
    rawJson: JSON.stringify({ backfillSource: "iem-asos", readings: dayReadings }),
  };
  await db
    .insert(weatherHistoricalObservation)
    .values(values)
    .onConflictDoUpdate({
      target: [weatherHistoricalObservation.stationId, weatherHistoricalObservation.obsDate, weatherHistoricalObservation.source],
      set: values,
    });
}

interface StationResult {
  externalId: string;
  status: "backfilled" | "already-complete" | "failed";
  daysWritten: number;
  error?: string;
}

async function main() {
  const endDate = new Date().toISOString().slice(0, 10);
  const startDate = historicalObservationCutoff(); // exactly Rule 3's 2-year window, reused
  const expectedDays = Math.floor((new Date(endDate).getTime() - new Date(startDate).getTime()) / (24 * 60 * 60 * 1000));

  const stations = await db
    .select({ id: weatherStation.id, externalId: weatherStation.externalId, timezone: weatherStation.timezone })
    .from(weatherStation)
    .where(eq(weatherStation.source, "metar"));

  console.log(`Global historical backfill: ${stations.length} station(s), window ${startDate} -> ${endDate} (~${expectedDays} days each).`);
  console.log(`Source: Iowa Environmental Mesonet ASOS archive. Pacing: ${BASE_DELAY_MS / 1000}s between stations, rate-limit-aware backoff.\n`);

  const results: StationResult[] = [];

  for (let i = 0; i < stations.length; i++) {
    const station = stations[i];
    const progress = `[${i + 1}/${stations.length}]`;

    const existingDays = await countExistingDays(station.id, startDate);
    if (existingDays >= expectedDays - 2) {
      // -2 days of slack: the CSV window and local-day bucketing can legitimately land one or
      // two fewer/more rows than a naive day-count, this isn't a bug worth re-fetching over.
      console.log(`${progress} ${station.externalId}: already backfilled (${existingDays} days on file) — skipping, no request made.`);
      results.push({ externalId: station.externalId, status: "already-complete", daysWritten: existingDays });
      continue;
    }

    console.log(`${progress} Processing station ${station.externalId} (${station.timezone})...`);
    try {
      const csv = await fetchStationCsv(station.externalId, startDate, endDate);
      const readings = parseAsosCsv(csv);
      const byDay = aggregateByLocalDay(readings, station.timezone);

      let written = 0;
      for (const [obsDate, agg] of byDay) {
        await upsertDay(station.id, obsDate, agg.maxF, agg.minF, agg.readings);
        written++;
      }

      console.log(`${progress} ${station.externalId}: fetched ${readings.length} raw readings, saved ${written} daily rows.`);
      results.push({ externalId: station.externalId, status: "backfilled", daysWritten: written });
    } catch (err) {
      const message = (err as Error).message;
      console.error(`${progress} ${station.externalId}: FAILED — ${message}. Continuing to next station.`);
      results.push({ externalId: station.externalId, status: "failed", daysWritten: 0, error: message });
    }

    if (i < stations.length - 1) await sleep(BASE_DELAY_MS);
  }

  console.log("\n=== Verification: rows per station ===");
  for (const r of results) {
    const label = r.status === "failed" ? `FAILED (${r.error})` : `${r.daysWritten} days`;
    console.log(`  ${r.externalId.padEnd(6)} ${label}`);
  }

  const backfilled = results.filter((r) => r.status === "backfilled").length;
  const alreadyDone = results.filter((r) => r.status === "already-complete").length;
  const failed = results.filter((r) => r.status === "failed").length;
  console.log(
    `\n=== Summary: ${stations.length} stations total — ${backfilled} newly backfilled, ` +
      `${alreadyDone} already complete, ${failed} failed. ` +
      `${backfilled + alreadyDone}/${stations.length} stations have historical data on file. ===`
  );
  if (failed > 0) {
    console.log(`Re-run this script to retry failed stations only (already-complete ones are skipped automatically).`);
  }
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("backfill:historical failed:", err);
    process.exit(1);
  });
}
