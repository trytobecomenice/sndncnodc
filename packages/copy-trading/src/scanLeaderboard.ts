// npm run scan:leaderboard — populates LeaderboardScan.
//
// IMPORTANT, empirically verified (2026-07-17, see conversation/plan notes):
// there is NO single bullpen call that returns a 500-row leaderboard.
// `bullpen polymarket discover traders --limit 500` caps at exactly 50 rows
// no matter what --limit is passed, AND its --category filter is a
// documented-but-inert no-op (--category politics and --category crypto
// return byte-identical wallet sets) — looping this call over categories
// would just waste API calls for zero additional coverage. `bullpen tracker
// groups curated` returns nothing in this environment.
//
// So this script fans out across genuinely different sources instead:
//   1. discover traders (global top-50 by volume/pnl, addresses direct)
//   2. per-event holders: top events by volume -> each event's highest-
//      volume market -> that market's position holders (display names,
//      resolved to addresses via `search --type user`)
// `event-top-holders` (which would have been the more direct version of
// source #2) was tried and timed out consistently (~10s, NETWORK_TIMEOUT)
// on both a $4B+ event and a small one — treated as unreliable, not used.
// `markets --sort volume` was also tried and rejected: its returned
// "volume" values were absurdly small and string-sorted, not real dollar
// volume (an event's own top-level `volume` field, and its embedded
// `markets[].volume`, are the trustworthy numbers instead — see
// scanEventHolders below).
//
// Coverage is accumulated as append-only LeaderboardScan history across
// repeated runs over time, not a single-shot 500-wallet pull.

import { runBullpenJson } from "@copybot/bullpen-client";
import { db, leaderboardScan } from "@copybot/db";

const TOP_EVENTS_TO_SCAN = 8;
const HOLDERS_PER_MARKET = 15;
const READ_RETRIES = 3;
const READ_RETRY_DELAY_MS = 500;

// Ethereum addresses are case-INsensitive (0xABC... and 0xabc... are the
// same wallet), but different bullpen commands return them in different
// casing — `discover traders` returns EIP-55 checksummed mixed-case,
// `search --type user` returns lowercase. Left unnormalized, this creates
// duplicate rows for the same real wallet wherever the address is used as a
// key (verified: 8 wallets ended up duplicated in wallet_profile from
// exactly this). Always normalize to lowercase at the point of writing.
function normalizeAddress(address: string): string {
  return address.toLowerCase();
}

interface LeaderboardRow {
  source: string;
  rank: number | null;
  walletAddress: string;
  displayName: string | null;
  pnlAllTime: number | null;
  volume30d: number | null;
  raw: unknown;
}

function parseDiscoverTradersTitle(title: string): { rank: number | null; pnl: number | null } {
  const rankMatch = /^#(\d+)/.exec(title);
  const pnlMatch = /P&L:\s*\$(-?[\d,]+)/.exec(title);
  return {
    rank: rankMatch ? Number(rankMatch[1]) : null,
    pnl: pnlMatch ? Number(pnlMatch[1].replace(/,/g, "")) : null,
  };
}

async function scanDiscoverTradersOverall(): Promise<LeaderboardRow[]> {
  const data = await runBullpenJson(["polymarket", "discover", "traders", "--limit", "500"], {
    retries: READ_RETRIES,
    retryDelayMs: READ_RETRY_DELAY_MS,
  });
  const rows: any[] = data?.events ?? [];
  return rows.map((row) => {
    const { rank, pnl } = parseDiscoverTradersTitle(String(row.title ?? ""));
    return {
      source: "discover_traders_overall",
      rank,
      walletAddress: normalizeAddress(row.id),
      displayName: null,
      pnlAllTime: pnl,
      volume30d: typeof row.volume === "number" ? row.volume : null,
      raw: row,
    };
  });
}

const FULL_ADDRESS_RE = /0x[a-fA-F0-9]{40}/;
const TRUNCATED_ADDRESS_RE = /^0x[a-fA-F0-9]{2,8}\.\.\.[a-fA-F0-9]{2,8}$/;

// Resolves a Polymarket display name to a wallet address via search, with an
// in-run cache so the same holder (common across events) is only resolved
// once per script invocation.
async function makeNameResolver() {
  const cache = new Map<string, string | null>();
  return async function resolveAddress(displayName: string): Promise<string | null> {
    if (cache.has(displayName)) return cache.get(displayName)!;

    // Fast path: some holders never set a nickname, so Polymarket's own
    // display_name IS (or embeds) their address — e.g.
    // "0x371afB83...b27CC13-1774063557496". Skip the search round-trip
    // entirely when we can read the address straight off the name.
    const embedded = FULL_ADDRESS_RE.exec(displayName);
    if (embedded) {
      const normalized = normalizeAddress(embedded[0]);
      cache.set(displayName, normalized);
      return normalized;
    }
    // The other shape some names take — "0x5533...51BA" — is a lossy
    // truncation with no recoverable address; a search call would just
    // fail on it (verified), so skip the network round-trip too.
    if (TRUNCATED_ADDRESS_RE.test(displayName)) {
      cache.set(displayName, null);
      return null;
    }

    try {
      const data = await runBullpenJson(["polymarket", "search", displayName, "--type", "user"], {
        retries: READ_RETRIES,
        retryDelayMs: READ_RETRY_DELAY_MS,
      });
      const profile = (data?.profiles ?? []).find((p: any) => p.name === displayName) ?? data?.profiles?.[0];
      const address = profile?.wallet ? normalizeAddress(profile.wallet) : null;
      cache.set(displayName, address);
      return address;
    } catch (err) {
      console.warn(`  search failed for "${displayName}": ${(err as Error).message}`);
      cache.set(displayName, null);
      return null;
    }
  };
}

async function scanEventHolders(): Promise<LeaderboardRow[]> {
  const eventsData = await runBullpenJson(
    ["polymarket", "events", "--sort", "volume", "--limit", String(TOP_EVENTS_TO_SCAN)],
    { retries: READ_RETRIES, retryDelayMs: READ_RETRY_DELAY_MS }
  );
  const events: any[] = eventsData?.events ?? [];
  const resolveAddress = await makeNameResolver();
  const rows: LeaderboardRow[] = [];

  for (const event of events) {
    const eventSlug = event.slug;
    try {
      const detail = await runBullpenJson(["polymarket", "event", eventSlug], {
        retries: READ_RETRIES,
        retryDelayMs: READ_RETRY_DELAY_MS,
      });
      const markets: any[] = detail?.markets ?? [];
      if (markets.length === 0) continue;
      const topMarket = markets.reduce((best, m) => ((m.volume ?? 0) > (best.volume ?? 0) ? m : best), markets[0]);

      const holdersData = await runBullpenJson(
        ["polymarket", "holders", topMarket.slug, "--limit", String(HOLDERS_PER_MARKET)],
        { retries: READ_RETRIES, retryDelayMs: READ_RETRY_DELAY_MS }
      );
      const holders: any[] = Array.isArray(holdersData) ? holdersData : (holdersData?.holders ?? []);

      for (const holder of holders) {
        const displayName = holder.display_name;
        if (!displayName) continue;
        const address = await resolveAddress(displayName);
        if (!address) continue;
        rows.push({
          source: `event_holders_${eventSlug}`,
          rank: holder.rank ?? null,
          walletAddress: address,
          displayName,
          pnlAllTime: null, // holders gives position value, not realized pnl
          volume30d: null,
          raw: { event: eventSlug, market: topMarket.slug, holder },
        });
      }
    } catch (err) {
      // Best-effort fan-out source — one bad event must not kill the whole
      // scan (event-top-holders' unreliability is exactly why this source
      // exists instead; the same defensiveness applies here).
      console.warn(`  event_holders_${eventSlug} failed, skipping: ${(err as Error).message}`);
    }
  }
  return rows;
}

async function insertRows(rows: LeaderboardRow[]) {
  if (rows.length === 0) return;
  await db.insert(leaderboardScan).values(
    rows.map((r) => ({
      source: r.source,
      rank: r.rank,
      walletAddress: r.walletAddress,
      displayName: r.displayName,
      pnlAllTime: r.pnlAllTime,
      volume30d: r.volume30d,
      rawJson: JSON.stringify(r.raw),
    }))
  );
}

async function main() {
  console.log(`Scanning leaderboard: discover_traders_overall + top ${TOP_EVENTS_TO_SCAN} events' holders...`);

  const overallRows = await scanDiscoverTradersOverall();
  console.log(`  discover_traders_overall: ${overallRows.length} wallets`);
  await insertRows(overallRows);

  const eventRows = await scanEventHolders();
  console.log(`  event_holders_*: ${eventRows.length} wallets`);
  await insertRows(eventRows);

  const uniqueWallets = new Set([...overallRows, ...eventRows].map((r) => r.walletAddress));
  console.log(
    `Done. Inserted ${overallRows.length + eventRows.length} leaderboard_scan rows ` +
      `(${uniqueWallets.size} unique wallets this run). Coverage accumulates across runs over time.`
  );
}

main().catch((err) => {
  console.error("scan:leaderboard failed:", err);
  process.exit(1);
});
