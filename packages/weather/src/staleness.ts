// The Staleness Guard (Joey, 2026-07-20) — patches the exact gap found live during the EV Bridge
// smoke test: checkMarkets.ts computed a -63.6pp "edge" for a market whose forecast_for day had
// already fully elapsed, because the market's price had converged toward the real, now-largely-
// known outcome while the stored ensemble forecast had not. That was never real signal.
//
// RULE (Joey's explicit call, 2026-07-20): a market is stale — skip it, do not calculate an edge
// for it — if its forecast_for day is strictly in the past (station-local calendar day), OR it's
// today but the station-local clock has passed STALE_CUTOFF_HOUR (18:00 / 6pm). Same-day trading
// before that cutoff is explicitly allowed. All comparisons use the STATION's own local time
// (weather_station.timezone, already resolved via geo-tz — Rule 8), not server/UTC time, since
// "is today's high already effectively decided" is a station-local question.

const STALE_CUTOFF_HOUR = 18;

export interface StationLocalTime {
  /** YYYY-MM-DD, station-local calendar day. */
  date: string;
  /** 0-23, station-local hour. */
  hour: number;
  /** Station-local "YYYY-MM-DDTHH:mm:ss" — deliberately no "Z"/offset suffix, since this is a
   * local wall-clock reading, not a UTC instant. */
  isoLocal: string;
}

/** Pure function (aside from reading the system clock via `now`, injectable for tests) — converts
 * a UTC instant into that timezone's local calendar date/hour/timestamp string. Uses `Intl` (no
 * new dependency — the same standard-library approach already relied on elsewhere in this repo,
 * e.g. geo-tz's own use of IANA zone data) rather than a manual UTC-offset table, so DST
 * transitions are handled correctly by the runtime, not approximated. */
export function getStationLocalTime(timezone: string, now: Date = new Date()): StationLocalTime {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  const parts = formatter.formatToParts(now);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "00";

  const date = `${get("year")}-${get("month")}-${get("day")}`;
  let hour = Number(get("hour"));
  // Some locales/runtimes format midnight as "24" rather than "00" under hour12: false.
  if (hour === 24) hour = 0;
  const minute = get("minute");
  const second = get("second");

  return { date, hour, isoLocal: `${date}T${String(hour).padStart(2, "0")}:${minute}:${second}` };
}

export interface StalenessCheck {
  isStale: boolean;
  /** True when forecastFor equals the station's current local calendar day — distinct from
   * "stale," since same-day is allowed until the cutoff hour. */
  isSameDay: boolean;
  stationLocalDate: string;
  stationLocalTime: string;
}

/** The gate itself. `forecastFor` is the market's target day (YYYY-MM-DD, same format
 * weather_market_mapping.forecast_for already stores). A day strictly before the station's
 * current local calendar day is always stale (fully elapsed, outcome long since decided); the
 * current local day is stale only once the station-local clock reaches STALE_CUTOFF_HOUR; any
 * future day is never stale. */
export function checkStaleness(forecastFor: string, timezone: string, now: Date = new Date()): StalenessCheck {
  const { date, hour, isoLocal } = getStationLocalTime(timezone, now);
  const isSameDay = forecastFor === date;
  const isPastDay = forecastFor < date;
  const isStale = isPastDay || (isSameDay && hour >= STALE_CUTOFF_HOUR);
  return { isStale, isSameDay, stationLocalDate: date, stationLocalTime: isoLocal };
}

export { STALE_CUTOFF_HOUR };
