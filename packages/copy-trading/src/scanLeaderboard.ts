// Candidate discovery from Polymarket's official, unauthenticated Data API.
//
// Safety boundary: discovery is decision input, therefore this module must
// never import @copybot/bullpen-client. Bullpen is reserved for signed order
// execution and its isolated read-only capability canary.

import { db, leaderboardScan } from "@copybot/db";
import {
  fetchOfficialLeaderboard,
  OFFICIAL_LEADERBOARD_CATEGORIES,
  type OfficialLeaderboardCategory,
  type OfficialLeaderboardOrderBy,
  type OfficialLeaderboardRow,
  type OfficialLeaderboardTimePeriod,
} from "./polymarketDataApi.js";

const ROWS_PER_LENS = 200;

interface ScanLens {
  category: OfficialLeaderboardCategory;
  timePeriod: OfficialLeaderboardTimePeriod;
  orderBy: OfficialLeaderboardOrderBy;
}

interface CandidateRow {
  source: string;
  rank: number | null;
  walletAddress: string;
  displayName: string | null;
  pnl30d: number | null;
  pnlAllTime: number | null;
  volume30d: number | null;
  raw: unknown;
}

export function normalizeAddress(address: string): string {
  return address.toLowerCase();
}

export function officialDiscoveryLenses(): ScanLens[] {
  // Monthly PnL by every documented category gives independent niches.
  // Overall all-time PnL and monthly volume add longevity/capacity lenses.
  return [
    ...OFFICIAL_LEADERBOARD_CATEGORIES.map((category) => ({
      category,
      timePeriod: "MONTH" as const,
      orderBy: "PNL" as const,
    })),
    { category: "OVERALL", timePeriod: "ALL", orderBy: "PNL" },
    { category: "OVERALL", timePeriod: "MONTH", orderBy: "VOL" },
  ];
}

export function mapOfficialLeaderboardRow(lens: ScanLens, row: OfficialLeaderboardRow): CandidateRow | null {
  if (typeof row.proxyWallet !== "string" || !/^0x[a-fA-F0-9]{40}$/.test(row.proxyWallet)) return null;
  const source = `polymarket_official_${lens.timePeriod}_${lens.category}_${lens.orderBy}`;
  return {
    source,
    rank: Number.isFinite(Number(row.rank)) ? Number(row.rank) : null,
    walletAddress: normalizeAddress(row.proxyWallet),
    displayName: typeof row.userName === "string" && row.userName.length > 0 ? row.userName : null,
    pnl30d: lens.timePeriod === "MONTH" && lens.orderBy === "PNL" ? Number(row.pnl) : null,
    pnlAllTime: lens.timePeriod === "ALL" && lens.orderBy === "PNL" ? Number(row.pnl) : null,
    volume30d: lens.timePeriod === "MONTH" ? Number(row.vol) : null,
    raw: { provider: "polymarket_data_api", endpoint: "/v1/leaderboard", query: lens, row },
  };
}

async function insertRows(rows: CandidateRow[]): Promise<void> {
  if (rows.length === 0) return;
  await db.insert(leaderboardScan).values(
    rows.map((row) => ({
      source: row.source,
      rank: row.rank,
      walletAddress: row.walletAddress,
      displayName: row.displayName,
      pnl30d: row.pnl30d,
      pnlAllTime: row.pnlAllTime,
      volume30d: row.volume30d,
      rawJson: JSON.stringify(row.raw),
    }))
  );
}

async function main(): Promise<void> {
  const allRows: CandidateRow[] = [];
  console.log("Scanning official Polymarket leaderboards (paginated, frozen category enum)...");

  for (const lens of officialDiscoveryLenses()) {
    const source = `polymarket_official_${lens.timePeriod}_${lens.category}_${lens.orderBy}`;
    try {
      const rawRows = await fetchOfficialLeaderboard({ ...lens, limit: 50, maxRows: ROWS_PER_LENS });
      const rows = rawRows
        .map((row) => mapOfficialLeaderboardRow(lens, row))
        .filter((row): row is CandidateRow => row !== null);
      await insertRows(rows);
      allRows.push(...rows);
      console.log(`  ${source}: ${rows.length} wallet rows`);
    } catch (error) {
      // One category may fail without discarding successful independent
      // lenses. The overall command still fails below if nothing landed.
      console.warn(`  ${source} failed: ${(error as Error).message}`);
    }
  }

  if (allRows.length === 0) {
    throw new Error("all official leaderboard lenses failed or returned no valid wallet addresses");
  }
  console.log(
    `Done. Inserted ${allRows.length} official leaderboard rows ` +
      `(${new Set(allRows.map((row) => row.walletAddress)).size} unique wallets).`
  );
}

if (process.env.NODE_ENV !== "test") {
  main().catch((error) => {
    console.error("scan:leaderboard failed:", error);
    process.exit(1);
  });
}
