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
//
// STATION COORDINATES: Wunderground's event descriptions name a station and an ICAO-style code
// (parsed from the cited wunderground.com URL) but never give lat/lon directly. Rather than
// fabricate coordinates, this script only creates a Wunderground-sourced weather_station row
// when an existing row for that SAME external_id (any source — typically METAR, from
// ingestMetar.ts) already has real, verified coordinates to borrow. A market whose station has
// no known coordinates yet is skipped and logged, not silently guessed.

import { runBullpenJson } from "@copybot/bullpen-client";
import { findStationByExternalId, upsertMarketMapping, upsertWeatherStation } from "./db/writers";
import { checkOddsFilter } from "./oddsFilter";

const DISCOVER_LIMIT = process.argv[2] ? Number(process.argv[2]) : 10;

interface DiscoverOutcome {
  name: string;
  probability: number;
  price: number;
}

interface DiscoverMarket {
  slug: string;
  question: string;
  outcomes: DiscoverOutcome[];
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

interface Tally {
  eventsScanned: number;
  strikeMarketsSeen: number;
  skippedOddsFilter: number;
  skippedNonWunderground: number;
  skippedNoStationCoords: number;
  written: number;
}

async function main() {
  const tally: Tally = {
    eventsScanned: 0,
    strikeMarketsSeen: 0,
    skippedOddsFilter: 0,
    skippedNonWunderground: 0,
    skippedNoStationCoords: 0,
    written: 0,
  };

  console.log(`Discovering weather events (bullpen polymarket discover --category weather --limit ${DISCOVER_LIMIT})...`);
  const events = await fetchWeatherEvents(DISCOVER_LIMIT);
  console.log(`  ${events.length} event(s) found.\n`);

  for (const event of events) {
    tally.eventsScanned++;
    const settlement = parseSettlementSource(event.description);

    if (!settlement.isWunderground) {
      console.log(`SKIP event ${event.slug}: settlement source is not Wunderground (not yet supported).`);
      tally.skippedNonWunderground += event.markets.length;
      tally.strikeMarketsSeen += event.markets.length;
      continue;
    }
    if (!settlement.icaoId) {
      console.log(`SKIP event ${event.slug}: cites Wunderground but no ICAO code could be parsed from the URL.`);
      tally.skippedNonWunderground += event.markets.length;
      tally.strikeMarketsSeen += event.markets.length;
      continue;
    }

    // Borrow coordinates from any existing station row for this ICAO code (typically a METAR
    // row from ingestMetar.ts) — never fabricated. Looked up once per event, reused for every
    // strike market under it.
    const knownStation = await findStationByExternalId(settlement.icaoId);
    let wundergroundStationId: string | null = null;
    if (knownStation) {
      wundergroundStationId = await upsertWeatherStation({
        externalId: settlement.icaoId,
        name: knownStation.name,
        source: "wunderground",
        lat: knownStation.lat,
        lon: knownStation.lon,
        timezone: knownStation.timezone,
        notes:
          `Coordinates inherited from an existing '${knownStation.source}' row for the same ` +
          `external_id (${settlement.icaoId}) — not independently verified for Wunderground ` +
          `specifically. See docs/weather/WEATHER_ARCHITECTURE.md §1 on source-scoped station identity.`,
      });
    }

    console.log(`EVENT ${event.slug} (station ${settlement.icaoId}, ${event.markets.length} strike markets):`);

    for (const market of event.markets) {
      tally.strikeMarketsSeen++;
      const yesOutcome = market.outcomes.find((o) => o.name === "Yes");
      if (!yesOutcome) {
        console.log(`  SKIP ${market.slug}: no "Yes" outcome found in response.`);
        continue;
      }

      const odds = checkOddsFilter(yesOutcome.probability);
      if (!odds.ok) {
        tally.skippedOddsFilter++;
        console.log(`  skip  ${market.slug}: ${odds.reason}`);
        continue;
      }

      if (!wundergroundStationId) {
        tally.skippedNoStationCoords++;
        console.log(
          `  skip  ${market.slug}: Yes=${yesOutcome.probability} passes the odds filter, but station ` +
            `${settlement.icaoId} has no known coordinates yet (run ingestMetar.ts for it first, or add manually).`
        );
        continue;
      }

      await upsertMarketMapping({
        marketSlug: market.slug,
        stationId: wundergroundStationId,
        settlementSource: "wunderground",
        settlementRule: `Settles via Wunderground station ${settlement.icaoId} (see event description for the full rule text).`,
      });
      tally.written++;
      console.log(`  WRITE ${market.slug}: Yes=${yesOutcome.probability} — mapping row written (is_active=false, needs review).`);
    }
    console.log("");
  }

  console.log("=== Summary ===");
  console.log(`Events scanned:              ${tally.eventsScanned}`);
  console.log(`Strike markets seen:         ${tally.strikeMarketsSeen}`);
  console.log(`Skipped (odds filter):       ${tally.skippedOddsFilter}`);
  console.log(`Skipped (non-Wunderground):  ${tally.skippedNonWunderground}`);
  console.log(`Skipped (no station coords): ${tally.skippedNoStationCoords}`);
  console.log(`Written (draft mappings):    ${tally.written}`);
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
