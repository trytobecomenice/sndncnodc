// pnpm --filter @copybot/weather ingest:openmeteo
//
// Ensemble forecast ingestion (docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 13,
// docs/weather/WEATHER_ARCHITECTURE.md §2). Fetches TWO independent ensemble products per
// station — ecmwf_ifs025 (51 members) and gfs_seamless (31 members), 82 combined — rather than
// a single deterministic point forecast, so the probability model (not yet built) can eventually
// answer "what fraction of simulated futures hit this bucket" instead of trusting one guess.
//
// BOTH MODEL IDENTIFIERS VERIFIED LIVE (2026-07-20), NOT ASSUMED FROM DOCS: the commonly-cited
// ECMWF identifier "ecmwf_ifs04" silently returns a single non-ensemble series with NO error —
// easy to ship a broken ingester never noticing it never got real ensemble data. The real
// identifier is "ecmwf_ifs025". If Open-Meteo ever renames these again, this script will need
// the same live-verification treatment, not just a docs lookup.
//
// LOCAL-DAY ALIGNMENT VIA THE API ITSELF: unlike backfillHistorical.ts (which had to convert IEM's
// UTC timestamps to each station's local calendar day manually), Open-Meteo's `timezone` query
// parameter returns `hourly.time[]` already expressed in that timezone — verified live. So this
// script passes each station's own resolved IANA timezone and buckets by the returned local date
// string directly, no conversion math needed.
//
// STORAGE: weather_ensemble_forecast, NOT weather_forecast_snapshot — a dedicated table exists
// specifically so a future probability calculation can run a direct SQL COUNT/AVG over member
// rows instead of parsing a JSON blob at read time (see the schema decision in
// docs/weather/WEATHER_ARCHITECTURE.md §2). Writes are done as CHUNKED BULK inserts (not one
// upsert per row) — with 82 members × ~10 days per station-model pair, row counts get large
// enough that bulk insert matters for real efficiency, not just style.

import { db, weatherEnsembleForecast, weatherStation } from "@copybot/db";
import { eq, sql } from "drizzle-orm";

const ENSEMBLE_API_BASE = "https://ensemble-api.open-meteo.com/v1/ensemble";
const FORECAST_DAYS = process.argv[2] ? Number(process.argv[2]) : 10;
const STATION_PACING_MS = 2000; // Open-Meteo's free tier is documented as generous (unlike IEM,
// which rate-limited us after 2 requests) — still paced out of consistency with the rest of this
// codebase's posture, not because a limit was hit in testing.
// SQLite/libsql has a practical bound on parameters per statement — chunk bulk inserts well under
// it (7 columns/row, so 300 rows/chunk = 2,100 params, safely inside any reasonable limit).
const INSERT_CHUNK_SIZE = 300;

interface EnsembleResponse {
  timezone: string;
  hourly: Record<string, (number | null)[]> & { time: string[] };
}

interface ModelSpec {
  id: string;
  expectedMinMembers: number; // sanity floor — if a model silently degrades (like ecmwf_ifs04 did),
  // catch it here rather than silently ingesting one-series-pretending-to-be-an-ensemble.
}

const MODELS: ModelSpec[] = [
  { id: "ecmwf_ifs025", expectedMinMembers: 50 },
  { id: "gfs_seamless", expectedMinMembers: 30 },
];

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchEnsemble(lat: number, lon: number, timezone: string, model: string): Promise<EnsembleResponse> {
  const url =
    `${ENSEMBLE_API_BASE}?latitude=${lat}&longitude=${lon}&hourly=temperature_2m&models=${model}` +
    `&forecast_days=${FORECAST_DAYS}&temperature_unit=fahrenheit&timezone=${encodeURIComponent(timezone)}`;
  const response = await fetch(url, {
    headers: { "User-Agent": "polymarket-copybot-weather (research/paper-trading, non-commercial)" },
  });
  if (!response.ok) {
    throw new Error(`Open-Meteo ensemble fetch failed: HTTP ${response.status} for model=${model}`);
  }
  const data = (await response.json()) as EnsembleResponse | { error: true; reason: string };
  if ("error" in data) {
    throw new Error(`Open-Meteo ensemble error for model=${model}: ${data.reason}`);
  }
  return data;
}

interface DailyMemberRow {
  forecastFor: string;
  model: string;
  memberIndex: number;
  tMaxF: number;
  tMinF: number;
}

/** Extracts every member series from the response ("temperature_2m" = member 0/control,
 * "temperature_2m_memberNN" = perturbed members), groups each member's hourly values by the
 * LOCAL calendar day the API already returned (see module comment), and computes that member's
 * daily max/min. Throws if fewer real members were found than `expectedMinMembers` — a silent
 * degraded response (like ecmwf_ifs04's single-series fallback) must fail loudly, not ingest
 * one series as if it were a real ensemble. */
function aggregateMembersByDay(response: EnsembleResponse, model: ModelSpec): DailyMemberRow[] {
  const memberKeys = Object.keys(response.hourly).filter(
    (k) => k === "temperature_2m" || /^temperature_2m_member\d+$/.test(k)
  );
  if (memberKeys.length < model.expectedMinMembers) {
    throw new Error(
      `model=${model.id} returned only ${memberKeys.length} member series, expected at least ` +
        `${model.expectedMinMembers} — likely a degraded/non-ensemble response, refusing to ingest it as real ensemble data.`
    );
  }

  const times = response.hourly.time;
  const rows: DailyMemberRow[] = [];

  for (const key of memberKeys) {
    const memberIndex = key === "temperature_2m" ? 0 : Number(key.match(/member(\d+)/)![1]);
    const values = response.hourly[key];

    const byDay = new Map<string, number[]>();
    for (let i = 0; i < times.length; i++) {
      const v = values[i];
      if (v === null || v === undefined) continue;
      const day = times[i].slice(0, 10); // already station-local, per the timezone query param
      const bucket = byDay.get(day);
      if (bucket) bucket.push(v);
      else byDay.set(day, [v]);
    }

    for (const [day, dayValues] of byDay) {
      // Skip a day with too few hourly points to trust a real max/min (e.g. the first/last
      // partial day of the requested window) — same "don't guess from noise" principle used
      // throughout this codebase (see scoreWallets.ts's insufficientData handling).
      if (dayValues.length < 12) continue;
      rows.push({
        forecastFor: day,
        model: model.id,
        memberIndex,
        tMaxF: Math.max(...dayValues),
        tMinF: Math.min(...dayValues),
      });
    }
  }

  return rows;
}

async function bulkUpsert(stationId: string, rows: DailyMemberRow[], issuedAt: Date): Promise<number> {
  if (rows.length === 0) return 0;
  const values = rows.map((r) => ({
    stationId,
    forecastFor: r.forecastFor,
    issuedAt,
    model: r.model,
    memberIndex: r.memberIndex,
    tMaxF: r.tMaxF,
    tMinF: r.tMinF,
  }));

  let written = 0;
  for (let i = 0; i < values.length; i += INSERT_CHUNK_SIZE) {
    const chunk = values.slice(i, i + INSERT_CHUNK_SIZE);
    await db
      .insert(weatherEnsembleForecast)
      .values(chunk)
      .onConflictDoUpdate({
        target: [
          weatherEnsembleForecast.stationId,
          weatherEnsembleForecast.forecastFor,
          weatherEnsembleForecast.issuedAt,
          weatherEnsembleForecast.model,
          weatherEnsembleForecast.memberIndex,
        ],
        // A bulk insert's conflict-update can't reuse one static `set` object the way this
        // codebase's single-row upserts do (e.g. ingestMetar.ts's upsertHistoricalObservation) —
        // each row in the chunk has different values. `sql`excluded.column`` references SQLite's
        // ON CONFLICT pseudo-table, so each conflicting row updates to ITS OWN incoming values.
        set: { tMaxF: sql`excluded.t_max_f`, tMinF: sql`excluded.t_min_f` },
      });
    written += chunk.length;
  }
  return written;
}

interface StationResult {
  externalId: string;
  status: "ok" | "failed";
  rowsWritten: number;
  error?: string;
}

async function main() {
  const stations = await db
    .select({ id: weatherStation.id, externalId: weatherStation.externalId, lat: weatherStation.lat, lon: weatherStation.lon, timezone: weatherStation.timezone })
    .from(weatherStation)
    .where(eq(weatherStation.source, "metar"));

  console.log(`Ensemble forecast ingestion: ${stations.length} station(s), ${FORECAST_DAYS} days ahead, models: ${MODELS.map((m) => m.id).join(", ")}.\n`);

  const issuedAt = new Date();
  const results: StationResult[] = [];

  for (let i = 0; i < stations.length; i++) {
    const station = stations[i];
    const progress = `[${i + 1}/${stations.length}]`;
    console.log(`${progress} Processing station ${station.externalId} (${station.timezone})...`);

    try {
      let stationRows: DailyMemberRow[] = [];
      for (const model of MODELS) {
        const response = await fetchEnsemble(station.lat, station.lon, station.timezone, model.id);
        const memberRows = aggregateMembersByDay(response, model);
        stationRows = stationRows.concat(memberRows);
      }

      const written = await bulkUpsert(station.id, stationRows, issuedAt);
      console.log(`${progress} ${station.externalId}: saved ${written} member-day rows across ${MODELS.length} models.`);
      results.push({ externalId: station.externalId, status: "ok", rowsWritten: written });
    } catch (err) {
      const message = (err as Error).message;
      console.error(`${progress} ${station.externalId}: FAILED — ${message}. Continuing to next station.`);
      results.push({ externalId: station.externalId, status: "failed", rowsWritten: 0, error: message });
    }

    if (i < stations.length - 1) await sleep(STATION_PACING_MS);
  }

  const ok = results.filter((r) => r.status === "ok");
  const failed = results.filter((r) => r.status === "failed");
  const totalRows = ok.reduce((sum, r) => sum + r.rowsWritten, 0);

  console.log("\n=== Summary ===");
  console.log(`Stations processed: ${stations.length} — ${ok.length} ok, ${failed.length} failed.`);
  console.log(`Total member-day rows written: ${totalRows}.`);
  if (failed.length > 0) {
    console.log("Failed stations:");
    for (const r of failed) console.log(`  ${r.externalId}: ${r.error}`);
  }
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("ingest:openmeteo failed:", err);
    process.exit(1);
  });
}
