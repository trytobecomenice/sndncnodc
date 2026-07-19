// pnpm --filter @copybot/weather discover:markets
//
// Market discovery (docs/weather/WEATHER_ARCHITECTURE.md §1 "Market mapping population",
// docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 8). Finds live Polymarket weather events via
// `bullpen polymarket discover --category weather`, and for each individual binary strike
// market that passes the Extreme Odds Filter (Rule 10), writes a DRAFT weather_market_mapping
// row — is_active always false here, per Rule 8: a human must review and approve the settlement
// station once per city/event before anything downstream may trade off it.
//
// REAL FINDING FROM LIVE DATA (2026-07-19): not every weather event settles via Wunderground.
// Hong Kong's event cites the Hong Kong Observatory directly, not Wunderground — this script
// detects that per-event (by checking for "wunderground.com" in the description) and skips
// non-Wunderground events entirely rather than mislabeling their settlement_source. Handling
// non-Wunderground settlement sources is a real, separate future capability, not attempted here.
// The Polymarket "Weather" category also includes non-city-temperature markets (air quality
// index, earthquake counts, a hantavirus-pandemic market, "hottest year on record") that share
// the same binary-outcome shape but obviously aren't Wunderground-settled either — these fall
// through the same non-Wunderground skip path with no special-casing needed.
//
// DYNAMIC STATION AUTO-ONBOARDING (2026-07-19): Polymarket adds new city markets continuously —
// a station allowlist that only worked for cities seen so far would break the moment an
// unfamiliar one appeared (Joey: "Tomorrow it might be London or Tokyo, and the bot will get
// stuck again"). This script calls stationReconciliation.ts's resolveStationMetadata() to fetch
// real lat/lon/name from aviationweather.gov and derive a real timezone (geo-tz, offline)
// automatically — the exact same free, ToS-clean source ingestMetar.ts already uses, just
// triggered on demand instead of manually. STILL NEVER FABRICATES: if the parsed ICAO code isn't
// a real METAR-reporting station, resolution returns null and is skipped and logged.
//
// STATION KNOWLEDGE IS DECOUPLED FROM THE ODDS FILTER (2026-07-19 — corrected from this file's
// original design). Every Wunderground-sourced event's station gets resolved/onboarded
// regardless of whether any of its markets currently pass Rule 10's odds band — a real gap Joey
// caught: the first version only resolved a station when a market happened to be in-band AT SCAN
// TIME, so a city whose markets were only briefly in-band between two scans could go forever
// unknown to this system even though it's a real, actively-traded Polymarket city. Station
// metadata is cheap, harmless reference data (no capital committed, no market tracked) — Rule 10
// still gates which MARKETS get written to weather_market_mapping (that's the real
// risk/attention decision), it should never have gated which STATIONS this system simply knows
// about.

import { runBullpenJson } from "@copybot/bullpen-client";
import { findStationByExternalId, upsertMarketMapping, upsertWeatherStation } from "./db/writers";
import { checkOddsFilter } from "./oddsFilter";
import { parseMarketThreshold } from "./parseMarketThreshold";
import { resolveStationMetadata } from "./stationReconciliation";

// Default 100: verified live (2026-07-19) that `bullpen polymarket discover --category weather`
// caps at 100 events server-side regardless of a higher --limit requested (tried 200, got 100
// back) — so 100 is the actual full universe available per call, not an arbitrary choice. The
// original default of 10 was accidentally only seeing ~8 of the real ~48 distinct cities
// Polymarket currently lists (Joey: "polymarket isnt only this few country... should hv much
// more") — 100 captures the whole set in one call.
const DISCOVER_LIMIT = process.argv[2] ? Number(process.argv[2]) : 100;

interface DiscoverOutcome {
  name: string;
  probability: number;
  price: number;
}

interface DiscoverMarket {
  slug: string;
  question: string;
  outcomes: DiscoverOutcome[];
  endDateIso: string; // YYYY-MM-DD — the market's own target date, straight from bullpen, no parsing needed
}

interface DiscoverEvent {
  slug: string;
  title: string;
  description: string;
  category: string | null;
  markets: DiscoverMarket[];
}

// Matches the ICAO-style code at the end of a wunderground.com history URL, regardless of how
// many country/state/city path segments precede it (verified against live data: Asian cities use
// a 2-segment path like ".../kr/incheon/RKSI", NYC uses a 3-segment path like
// ".../us/ny/new-york-city/KLGA" — a fixed-depth regex would silently miss NYC).
const WUNDERGROUND_URL_RE = /wunderground\.com\/history\/daily\/(?:[a-z0-9-]+\/)+([A-Z0-9]{3,4})/;

interface ParsedSettlement {
  isWunderground: boolean;
  icaoId: string | null;
}

function parseSettlementSource(description: string): ParsedSettlement {
  const isWunderground = description.includes("wunderground.com");
  if (!isWunderground) return { isWunderground: false, icaoId: null };
  const match = WUNDERGROUND_URL_RE.exec(description);
  return { isWunderground: true, icaoId: match ? match[1] : null };
}

async function fetchWeatherEvents(limit: number): Promise<DiscoverEvent[]> {
  const response = await runBullpenJson(
    ["polymarket", "discover", "--category", "weather", "--limit", String(limit)],
    { retries: 2, retryDelayMs: 500 }
  );
  return (response?.events ?? []) as DiscoverEvent[];
}

/**
 * Finds (or, if unknown, auto-onboards) the Wunderground-sourced weather_station row for an
 * ICAO code, borrowing/writing coordinates as needed. Returns null only if the station genuinely
 * cannot be resolved (not a real METAR-reporting station) — never fabricated.
 *
 * Called for EVERY Wunderground-sourced event, independent of odds — see this file's module
 * comment on why station knowledge is deliberately decoupled from Rule 10's odds filter.
 */
async function resolveWundergroundStation(icaoId: string): Promise<{ id: string; wasAutoOnboarded: boolean } | null> {
  const known = await findStationByExternalId(icaoId);
  if (known) {
    const id = await upsertWeatherStation({
      externalId: icaoId,
      name: known.name,
      source: "wunderground",
      lat: known.lat,
      lon: known.lon,
      timezone: known.timezone,
      notes:
        `Coordinates inherited from an existing '${known.source}' row for the same external_id ` +
        `(${icaoId}) — not independently verified for Wunderground specifically. See ` +
        `docs/weather/WEATHER_ARCHITECTURE.md §1 on source-scoped station identity.`,
    });
    return { id, wasAutoOnboarded: false };
  }

  const resolved = await resolveStationMetadata(icaoId);
  if (!resolved) return null;

  // Auto-onboard BOTH a metar-sourced row (real ground-truth geodata, matches ingestMetar.ts's
  // own convention so a future ingestMetar.ts run for this station has a consistent source row
  // to upsert onto) and the wunderground-sourced row this event's mapping actually needs.
  await upsertWeatherStation({
    externalId: icaoId,
    name: resolved.name,
    source: "metar",
    lat: resolved.lat,
    lon: resolved.lon,
    timezone: resolved.timezone,
    notes: "Auto-onboarded by discoverMarkets.ts (stationReconciliation.ts) — not yet backfilled with historical observations.",
  });
  const id = await upsertWeatherStation({
    externalId: icaoId,
    name: resolved.name,
    source: "wunderground",
    lat: resolved.lat,
    lon: resolved.lon,
    timezone: resolved.timezone,
    notes: "Auto-onboarded by discoverMarkets.ts (stationReconciliation.ts) from aviationweather.gov + geo-tz.",
  });
  return { id, wasAutoOnboarded: true };
}

interface Tally {
  eventsScanned: number;
  strikeMarketsSeen: number;
  skippedOddsFilter: number;
  skippedNonWunderground: number;
  skippedUnresolvableStation: number;
  stationsAutoOnboarded: number;
  written: number;
}

async function main() {
  const tally: Tally = {
    eventsScanned: 0,
    strikeMarketsSeen: 0,
    skippedOddsFilter: 0,
    skippedNonWunderground: 0,
    skippedUnresolvableStation: 0,
    stationsAutoOnboarded: 0,
    written: 0,
  };

  console.log(`Discovering weather events (bullpen polymarket discover --category weather --limit ${DISCOVER_LIMIT})...`);
  const events = await fetchWeatherEvents(DISCOVER_LIMIT);
  console.log(`  ${events.length} event(s) found.\n`);

  for (const event of events) {
    tally.eventsScanned++;
    const settlement = parseSettlementSource(event.description);

    if (!settlement.isWunderground || !settlement.icaoId) {
      const reason = !settlement.isWunderground
        ? "settlement source is not Wunderground (not yet supported)"
        : "cites Wunderground but no ICAO code could be parsed from the URL";
      console.log(`SKIP event ${event.slug}: ${reason}.`);
      tally.skippedNonWunderground += event.markets.length;
      tally.strikeMarketsSeen += event.markets.length;
      continue;
    }

    // Resolve/auto-onboard the station FIRST, unconditionally — station knowledge is not gated
    // by odds (see module comment). Only market TRACKING (the mapping write below) is.
    const station = await resolveWundergroundStation(settlement.icaoId);
    if (station?.wasAutoOnboarded) {
      tally.stationsAutoOnboarded++;
      console.log(`  AUTO-ONBOARDED station ${settlement.icaoId} (real coordinates from aviationweather.gov + geo-tz).`);
    }

    const inBand: Array<{ market: DiscoverMarket; yesProb: number }> = [];
    for (const market of event.markets) {
      tally.strikeMarketsSeen++;
      const yesOutcome = market.outcomes.find((o) => o.name === "Yes");
      if (!yesOutcome) continue;
      const odds = checkOddsFilter(yesOutcome.probability);
      if (odds.ok) {
        inBand.push({ market, yesProb: yesOutcome.probability });
      } else {
        tally.skippedOddsFilter++;
      }
    }

    if (!station) {
      console.log(
        `EVENT ${event.slug}: station ${settlement.icaoId} could not be resolved (not a real ` +
          `METAR-reporting station) — no data to onboard, not guessing. ${inBand.length} market(s) ` +
          `would otherwise have been in-band.\n`
      );
      tally.skippedUnresolvableStation += inBand.length;
      continue;
    }

    if (inBand.length === 0) {
      console.log(`EVENT ${event.slug} (station ${settlement.icaoId} known${station.wasAutoOnboarded ? ", just onboarded" : ""}): no strike markets in the 10-90% band this scan — no mappings written.\n`);
      continue;
    }

    console.log(`EVENT ${event.slug} (station ${settlement.icaoId}, ${inBand.length}/${event.markets.length} strike markets in band):`);
    for (const { market, yesProb } of inBand) {
      const threshold = parseMarketThreshold(market.slug);
      if (!threshold) {
        // Shouldn't happen for a real temperature-bucket market (live-validated against all 100
        // real events, 1024/1052 markets parsed correctly, 28/28 non-matches were genuinely
        // non-temperature markets) — but this event passed the Wunderground/odds gates, so if the
        // slug still doesn't parse, that's worth a loud log, not a silent skip.
        console.log(`  SKIP ${market.slug}: passed all gates but parseMarketThreshold couldn't parse it — investigate.`);
        continue;
      }
      await upsertMarketMapping({
        marketSlug: market.slug,
        stationId: station.id,
        settlementSource: "wunderground",
        settlementRule: `Settles via Wunderground station ${settlement.icaoId} (see event description for the full rule text).`,
        metric: threshold.metric,
        forecastFor: market.endDateIso,
        targetTempMinF: threshold.targetTempMinF,
        targetTempMaxF: threshold.targetTempMaxF,
      });
      tally.written++;
      const rangeLabel = `[${threshold.targetTempMinF?.toFixed(1) ?? "-inf"}, ${threshold.targetTempMaxF?.toFixed(1) ?? "+inf"}]F`;
      console.log(`  WRITE ${market.slug}: Yes=${yesProb}, ${threshold.metric} target ${rangeLabel} — mapping row written (is_active=false, needs review).`);
    }
    console.log("");
  }

  console.log("=== Summary ===");
  console.log(`Events scanned:                ${tally.eventsScanned}`);
  console.log(`Strike markets seen:           ${tally.strikeMarketsSeen}`);
  console.log(`Skipped (odds filter):         ${tally.skippedOddsFilter}`);
  console.log(`Skipped (non-Wunderground):    ${tally.skippedNonWunderground}`);
  console.log(`Skipped (unresolvable station):${tally.skippedUnresolvableStation}`);
  console.log(`Stations auto-onboarded:       ${tally.stationsAutoOnboarded}`);
  console.log(`Written (draft mappings):      ${tally.written}`);
  if (tally.written > 0) {
    console.log(
      `\n${tally.written} mapping(s) need human review (Rule 8) before is_active can be set true — ` +
        `see docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 8.`
    );
  }
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("discover:markets failed:", err);
    process.exit(1);
  });
}
