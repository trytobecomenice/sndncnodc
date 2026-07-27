// Direct Polymarket Data API client — the TypeScript twin of
// polymarket_data_api.py (the Python bot's tracking-feed module). Needed for
// category-specific wallet scoring (scoreWalletCategories.ts): reconstructing
// a wallet's own trade history requires RAW per-trade records, and bullpen's
// wallet-stats API only exposes whole-wallet aggregates (fetchWalletStatsSummary
// /fetchTradeFlow/fetchBehaviorStats/fetchPnlSeries in scoreWallets.ts — none
// of them take a market/category filter, confirmed by reading that file
// before building this). No new facts are re-verified here about the API
// itself — data-api.polymarket.com/activity's no-auth/pagination/field-shape
// behavior was already confirmed live from the Python side; this file just
// re-implements the same request shape in TS.
//
// Deliberately NOT using bullpen for this — this is exactly the kind of
// read-only market data the copy bot already moved off bullpen for on the
// Python side (Rule 14/Rule 10 in docs/copy-trading/RISK_MANAGEMENT.md).
//
// Simpler than the Python version on purpose: this runs once a month as part
// of the scoring job, not on a 30s poll loop, so there's no persistent-
// connection-reuse optimization to build — Node's own fetch() already reuses
// HTTP/1.1 keep-alive connections per-host without any extra code here.

const DATA_API_HOST = "https://data-api.polymarket.com";
const DEFAULT_LIMIT = 500; // Polymarket's documented per-page max for /activity
// Polymarket's own docs (and a live 400 hit while building this: "max
// historical activity offset of 5000 exceeded") cap total offset at 5000 —
// MAX_PAGES * DEFAULT_LIMIT must stay under that. 10 * 500 = 5000, so the
// last page requested is at offset 4500 (covering records 0-4999), never
// requesting offset 5000 itself. A wallet with more than 5000 trades in the
// lookback window needs Polymarket's own start/end-windowed pagination to
// go deeper — out of scope here, logged instead of silently truncated.
const MAX_PAGES = 10;
const RETRYABLE_STATUS = new Set([429, 500, 502, 503, 504]);
const MAX_RETRIES = 4;
const BACKOFF_BASE_MS = 1000; // 1s, 2s, 4s, 8s — same schedule as polymarket_data_api.py

const REQUEST_HEADERS = {
  "User-Agent": "polymarket-copybot/1.0 (+personal research bot)",
  Accept: "application/json",
};

// Field names exactly as Polymarket's /activity endpoint returns them — see
// polymarket_data_api.py's normalize_activity_record() for the same mapping,
// confirmed live there against a real side-by-side bullpen call.
export interface RawActivityRecord {
  proxyWallet: string;
  timestamp: number; // unix epoch seconds
  transactionHash: string;
  price: number;
  asset: string; // CLOB token ID
  size: number; // raw share count — used directly for position tracking, not backed out via usdcSize/price
  usdcSize: number; // actual on-chain settled amount, fee-inclusive
  side: "BUY" | "SELL";
  slug: string; // market slug
  outcome: string;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchOnePage(
  walletAddress: string,
  limit: number,
  offset: number,
  startEpochSeconds?: number
): Promise<RawActivityRecord[]> {
  const params = new URLSearchParams({
    user: walletAddress,
    type: "TRADE",
    limit: String(limit),
    offset: String(offset),
  });
  if (startEpochSeconds !== undefined) {
    params.set("start", String(Math.floor(startEpochSeconds)));
  }
  const url = `${DATA_API_HOST}/activity?${params.toString()}`;

  let attempt = 0;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const response = await fetch(url, { headers: REQUEST_HEADERS });
    if (response.status === 200) {
      return (await response.json()) as RawActivityRecord[];
    }
    if (RETRYABLE_STATUS.has(response.status) && attempt < MAX_RETRIES) {
      await sleep(BACKOFF_BASE_MS * 2 ** attempt);
      attempt += 1;
      continue;
    }
    const body = await response.text();
    throw new Error(`HTTP ${response.status} after ${attempt} backoff retr(y/ies): ${body.slice(0, 200)}`);
  }
}

/**
 * Fetches one wallet's raw TRADE activity, paginated automatically (stops
 * the first time a page comes back shorter than `limit`, meaning no more
 * data). `startEpochSeconds` filters server-side to trades at or after that
 * time — used by scoreWalletCategories.ts to bound the same rolling window
 * scoreWallets.ts already uses for the global score.
 *
 * Raises on a genuine fetch failure — callers that fan out across many
 * wallets must catch per-wallet so one bad wallet can't take down a batch
 * (see scoreWalletCategories.ts, mirroring bot.py's fetch_all_wallets_concurrent
 * per-wallet error isolation).
 */
export async function fetchWalletTrades(
  walletAddress: string,
  options: { limit?: number; startEpochSeconds?: number } = {}
): Promise<RawActivityRecord[]> {
  const limit = options.limit ?? DEFAULT_LIMIT;
  const allRecords: RawActivityRecord[] = [];
  let offset = 0;

  for (let page = 0; page < MAX_PAGES; page++) {
    const pageRecords = await fetchOnePage(walletAddress, limit, offset, options.startEpochSeconds);
    allRecords.push(...pageRecords);
    if (pageRecords.length < limit) {
      return allRecords; // short page -> no more data, stop paging
    }
    offset += limit;
  }

  console.warn(
    `fetchWalletTrades: hit MAX_PAGES=${MAX_PAGES} for ${walletAddress} — this wallet's trade ` +
      `history may be incomplete beyond this point (a hyperactive wallet exceeding the page cap, ` +
      `not a bug) — category scoring for it should be treated as partial, not silently trusted as complete.`
  );
  return allRecords;
}
