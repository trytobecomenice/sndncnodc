// Auto-onboarding for weather stations — built 2026-07-19, replacing manual per-station
// hardcoding in BOTH ingestMetar.ts (which originally had a hand-maintained
// KNOWN_STATION_TIMEZONES map, added when this file only needed to support RKSI) and
// discoverMarkets.ts (which originally skipped any market whose station wasn't already known).
//
// WHY THIS WAS NECESSARY, NOT JUST NICE-TO-HAVE: Polymarket creates new city weather markets
// continuously — hardcoding a station whenever one shows up doesn't scale and breaks the moment
// an unfamiliar city appears (Joey, 2026-07-19: "Tomorrow it might be London or Tokyo, and the
// bot will get stuck again"). This file resolves a station's real metadata automatically from
// two free, ToS-clean, already-integrated sources: aviationweather.gov (lat/lon/name — the same
// source ingestMetar.ts already fetches) and `geo-tz` (an offline lat/lon -> IANA timezone
// lookup, no network call, no manual map to maintain). NEVER fabricates data: if a station's
// ICAO code isn't in the METAR network at all, resolution returns null and the caller must skip
// that station/market rather than guess.

const AVIATION_WEATHER_BASE = "https://aviationweather.gov/api/data/metar";

export interface StationSample {
  name: string;
  lat: number;
  lon: number;
}

/**
 * Fetches ONE recent METAR report for a station, just to read its name/lat/lon — a lighter
 * cousin of ingestMetar.ts's fetchMetarReports (which pulls 48h of history for daily-max
 * computation; this only needs one data point for geographic metadata).
 *
 * OUTPUT: null if the station doesn't exist in the METAR network or the request fails — this is
 * an EXPECTED, valid outcome in an auto-discovery context (not every Wunderground-cited station
 * is a METAR-reporting airport), so this fails soft, unlike ingestMetar.ts's fail-loud design
 * (which is a manual one-off proof run where a silent skip would hide a real problem).
 */
export async function fetchMetarStationSample(icaoId: string): Promise<StationSample | null> {
  try {
    const url = `${AVIATION_WEATHER_BASE}?ids=${icaoId}&format=json&hours=3`;
    const response = await fetch(url, {
      headers: { "User-Agent": "polymarket-copybot-weather (research/paper-trading, non-commercial)" },
    });
    if (!response.ok) return null;
    const data = (await response.json()) as Array<{ name: string; lat: number; lon: number }>;
    if (data.length === 0) return null;
    const sample = data[data.length - 1];
    return { name: sample.name, lat: sample.lat, lon: sample.lon };
  } catch {
    return null;
  }
}

/**
 * Resolves an IANA timezone from coordinates — offline, no network call, no per-station manual
 * map. `geo-tz` can return multiple candidate zones for points near a timezone boundary; the
 * first is used (matches the library's own documented ordering — most specific/likely match
 * first). Good enough for this system's purposes (nowcast/forecast timing, not legal timekeeping).
 */
export async function resolveTimezone(lat: number, lon: number): Promise<string> {
  const { find } = await import("geo-tz");
  const zones = find(lat, lon);
  return zones[0] ?? "UTC";
}

export interface ResolvedStationMetadata {
  name: string;
  lat: number;
  lon: number;
  timezone: string;
}

/**
 * The single entry point both ingestMetar.ts and discoverMarkets.ts use to onboard a station by
 * ICAO code with zero manual per-station configuration.
 * OUTPUT: null if the station couldn't be resolved (not a real/active METAR station) — callers
 * must skip, never fabricate a fallback.
 */
export async function resolveStationMetadata(icaoId: string): Promise<ResolvedStationMetadata | null> {
  const sample = await fetchMetarStationSample(icaoId);
  if (!sample) return null;
  const timezone = await resolveTimezone(sample.lat, sample.lon);
  return { name: sample.name, lat: sample.lat, lon: sample.lon, timezone };
}
