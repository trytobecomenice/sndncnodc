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
// =============================================================================
// 2026-07-17 STRATEGY SHIFT: OFF THE VOLUME LEADERBOARD, ONTO PROFIT + NICHE
// =============================================================================
// scoreWallets.ts's toxic-flow gate (consistencyScore < 0.20 -> force
// "ignore") caught real volume-farmer wallets — but it caught 16 of 23 pass-2
// survivors in one run. That's the gate doing its job, but it also means
// this script was feeding it a candidate pool heavily loaded with exactly
// the wrong kind of wallet. The root cause: `scanEventHolders` (below) found
// candidates by taking the highest-VOLUME events, drilling into their
// biggest market, and pulling raw POSITION HOLDERS — with ZERO profit
// signal at discovery time (its own row shape hardcodes `pnlAllTime: null`,
// see the comment on it). A big position in a hot market says nothing about
// whether the holder is skilled or just large/active — that's the textbook
// rebate-farmer shape.
//
// The fix has two parts:
//
//   1. NEW PRIMARY SOURCE — `bullpen polymarket data leaderboard` supports
//      `--sort pnl`, `--time-period 30d/90d`, AND `--hide-farmers`/
//      `--hide-bots` flags built into the API itself (see
//      scanDataLeaderboardPnl). This is a genuine profit-leaderboard,
//      pre-filtered at the SOURCE instead of relying entirely on our own
//      downstream gate.
//
//   2. scanEventHolders IS RETARGETED (not removed) from "highest-volume
//      events overall" to "highest-volume events within data-heavy NICHE
//      categories" (sports today — see scanNicheEventHolders). Checked
//      real numbers before committing to this: weather markets are
//      currently a dead end (2 thin World-Cup-adjacent markets, ~$8K-$60K
//      volume total — matches the caveat already on file in config.py from
//      2026-07-15 that no weather-primary trader could be verified), while
//      sports has real depth right now (NFL Champion 2027 alone: $41M
//      volume). Weather stays trivial to add back once its markets deepen —
//      same mechanism, different `discover` lens.
//
// A third source, `scanSmartMoneyTopTraders`, uses `bullpen polymarket data
// smart-money --type top_traders` (gated behind the `prediction_analytics`
// experimental flag — enabled 2026-07-17) for a second, differently-sourced
// opinion on "who's currently hot," per-category.
//
// HONEST CAVEAT: `data leaderboard` and `data smart-money --type
// top_traders` were BOTH erroring server-side when this was written (first
// a ClickHouse MEMORY_LIMIT_EXCEEDED, then a timeout on retry — confirmed
// via `bullpen status` that this is a backend/infra issue, not an auth or
// client problem). That means the exact response field names below are
// NOT verified against a real successful payload — they're the closest
// reasonable guess from the CLI's documented shape and this codebase's
// existing naming conventions (`wallet_address`, etc.), with multiple
// candidate field names tried defensively (see `firstDefined`), mirroring
// how bullpen_client.py's extract_fill_price already handles an unverified
// response shape. Both new fetchers are wrapped exactly like every other
// fetch in this file: a failure logs a warning and returns an empty array,
// it can never crash the whole scan. THE FIRST REAL RUN AFTER BULLPEN'S
// BACKEND RECOVERS IS THE ACTUAL VERIFICATION STEP — inspect
// leaderboard_scan rows tagged `data_leaderboard_pnl_*` / `smart_money_*`
// and fix field names here if they came back empty/wrong.
//
// So this script fans out across genuinely different sources instead:
//   1. data_leaderboard_pnl_30d / _90d — bullpen's real profit leaderboard,
//      pre-filtered (hide-farmers/hide-bots), time-windowed. NEW, PRIMARY.
//   2. smart_money_<category> — a second opinion from bullpen's smart-money
//      top-traders view, per category. NEW.
//   3. discover_traders_overall — global top-50 (legacy, kept as a cheap
//      supplementary source; ranking criteria within it is not fully within
//      our control, see note above).
//   4. event_holders_<slug> — niche-market (sports) position holders. Still
//      has no profit signal at discovery time, same as before — but now
//      scoped to a market segment worth mining rather than "whatever's
//      loudest today," and every candidate from every source flows through
//      the SAME toxic-flow gate in scoreWallets.ts regardless of origin, so
//      a farmer that slips in here is still caught downstream.
// `event-top-holders` (which would have been the more direct version of
// source #4) was tried and timed out consistently (~10s, NETWORK_TIMEOUT)
// on both a $4B+ event and a small one — treated as unreliable, not used.
// `markets --sort volume` was also tried and rejected: its returned
// "volume" values were absurdly small and string-sorted, not real dollar
// volume (an event's own top-level `volume` field, and its embedded
// `markets[].volume`, are the trustworthy numbers instead — see
// scanNicheEventHolders below).
//
// Coverage is accumulated as append-only LeaderboardScan history across
// repeated runs over time, not a single-shot 500-wallet pull.

import { runBullpenJson } from "@copybot/bullpen-client";
import { db, leaderboardScan } from "@copybot/db";

const TOP_EVENTS_TO_SCAN = 8;
const HOLDERS_PER_MARKET = 15;
const READ_RETRIES = 3;
const READ_RETRY_DELAY_MS = 500;
const DATA_LEADERBOARD_LIMIT = 100; // unverified cap — discover traders capped at 50 despite a larger --limit; watch the real row count on first run
const SMART_MONEY_LIMIT = 100;
// Categories smart-money's own --category flag documents (--help). "overall"
// covers the general case; "sports" is the currently-verified-active niche
// (see the 2026-07-17 STRATEGY SHIFT note above) — weather isn't a
// smart-money category at all today, and isn't a `discover` sport/category
// either; it would need its own market-search-based source once its markets
// have real depth, not added here to avoid shipping a source with nothing
// to find.
const SMART_MONEY_CATEGORIES = ["overall", "sports"];

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

/**
 * Returns the first defined, non-null value found at any of `keys` on
 * `obj`. Used for response fields whose exact name isn't verified against a
 * real payload yet (see the 2026-07-17 STRATEGY SHIFT doc comment) — tries
 * every plausible candidate rather than guessing wrong and silently getting
 * `undefined` everywhere. Mirrors bullpen_client.py's extract_fill_price,
 * which does the same thing for an unverified trade-fill response shape.
 */
function firstDefined<T = unknown>(obj: any, keys: string[]): T | undefined {
  if (!obj) return undefined;
  for (const key of keys) {
    if (obj[key] !== undefined && obj[key] !== null) return obj[key] as T;
  }
  return undefined;
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

/**
 * NEW PRIMARY SOURCE (2026-07-17): bullpen's real profit leaderboard,
 * server-side sorted by pnl and pre-filtered to hide farmers/bots — see the
 * STRATEGY SHIFT doc comment at the top of this file for why this replaces
 * reliance on volume-sorted event holders as the main candidate source.
 *
 * INPUT:  timePeriod — "30d" or "90d", matching scoreWallets.ts's own ROI
 *         window and rolling-window default, so the candidates this finds
 *         and the window scoreWallets.ts scores them on are talking about
 *         the same stretch of time.
 * OUTPUT: LeaderboardRow[] (empty on failure — never throws).
 *
 * FIELD NAMES VERIFIED 2026-07-17 against a real successful `--time-period
 * 30d` payload (the 90d call was still hitting bullpen's backend outage at
 * the time — same shape assumed, not independently confirmed): the row
 * shape is MUCH richer than this function currently uses — `is_farmer`,
 * `is_bot`, `win_rate_30d`/`_90d`/`_all`, `copyability_tier`, `risk_tier`,
 * `trader_tier`, `trading_style`, `primary_category`, `max_drawdown`,
 * `profit_factor` are all present per row (all preserved in `raw` even
 * though not extracted into named fields here — a future pass could mine
 * these directly instead of re-deriving similar signals from wallet-stats
 * in scoreWallets.ts). The one bug this verification caught: `pnl`/
 * `pnl_all_time`/`total_pnl` (the original guesses) don't exist — the real
 * fields are `realized_pnl_30d`/`realized_pnl_90d`/`realized_pnl_all`/
 * `lifetime_pnl`, which is why pnlAllTime below prefers the field matching
 * the requested window first.
 */
async function scanDataLeaderboardPnl(timePeriod: "30d" | "90d"): Promise<LeaderboardRow[]> {
  let data: any;
  try {
    data = await runBullpenJson(
      [
        "polymarket",
        "data",
        "leaderboard",
        "--time-period",
        timePeriod,
        "--sort",
        "pnl",
        "--hide-farmers",
        "--hide-bots",
        "--limit",
        String(DATA_LEADERBOARD_LIMIT),
      ],
      { retries: READ_RETRIES, retryDelayMs: READ_RETRY_DELAY_MS }
    );
  } catch (err) {
    console.warn(`  data_leaderboard_pnl_${timePeriod} failed, skipping: ${(err as Error).message}`);
    return [];
  }

  const rows: any[] = firstDefined(data, ["traders", "leaderboard", "rows", "data", "results"]) ?? [];
  if (rows.length === 0 && data) {
    console.warn(
      `  data_leaderboard_pnl_${timePeriod} returned 0 rows — response shape may not match the field ` +
        `names this script guesses at (see HONEST CAVEAT doc comment). Raw top-level keys: ` +
        `${Object.keys(data).join(", ")}`
    );
  }

  return rows
    .map((row): LeaderboardRow | null => {
      const address = firstDefined<string>(row, ["wallet_address", "address", "wallet", "id"]);
      if (!address) return null;
      // Prefer the field matching the requested window (realized_pnl_30d
      // for a 30d pull, etc.) — falls back to lifetime/all-time and the
      // originally-guessed names in case bullpen's field set shifts.
      const windowedPnlKey = `realized_pnl_${timePeriod}`;
      return {
        source: `data_leaderboard_pnl_${timePeriod}`,
        rank: firstDefined<number>(row, ["rank", "position"]) ?? null,
        walletAddress: normalizeAddress(address),
        displayName: firstDefined<string>(row, ["display_name", "displayName", "name", "nickname"]) ?? null,
        pnlAllTime: firstDefined<number>(row, [
          windowedPnlKey,
          "realized_pnl_all",
          "lifetime_pnl",
          "pnl",
          "pnl_all_time",
          "pnlAllTime",
          "total_pnl",
        ]) ?? null,
        volume30d: firstDefined<number>(row, ["volume_30d", "volume30d", "volume"]) ?? null,
        raw: row,
      };
    })
    .filter((r): r is LeaderboardRow => r !== null);
}

/**
 * NEW SECOND OPINION (2026-07-17): bullpen's smart-money "top traders" view,
 * per category (see SMART_MONEY_CATEGORIES). Gated behind the
 * `prediction_analytics` experimental flag (enabled 2026-07-17 — see
 * `bullpen experimental enable prediction_analytics`). A differently-sourced
 * signal from the same underlying data as data_leaderboard_pnl, useful as a
 * cross-check rather than a sole source of truth.
 *
 * INPUT:  category — one of SMART_MONEY_CATEGORIES
 * OUTPUT: LeaderboardRow[] (empty on failure — see HONEST CAVEAT above;
 *         the ONE confirmed fact about this response shape is that the
 *         top-level trader array is called `traders` — verified against a
 *         real successful `--type aggregated` call, which returned
 *         `{ mode, signals: [...], traders: [] }`).
 */
async function scanSmartMoneyTopTraders(category: string): Promise<LeaderboardRow[]> {
  let data: any;
  try {
    data = await runBullpenJson(
      [
        "polymarket",
        "data",
        "smart-money",
        "--type",
        "top_traders",
        "--category",
        category,
        "--sort",
        "pnl",
        "--time-period",
        "30d",
        "--hide-farmers",
        "--limit",
        String(SMART_MONEY_LIMIT),
      ],
      { retries: READ_RETRIES, retryDelayMs: READ_RETRY_DELAY_MS }
    );
  } catch (err) {
    console.warn(`  smart_money_${category} failed, skipping: ${(err as Error).message}`);
    return [];
  }

  // "traders" is the one confirmed field name (see doc comment above) —
  // still tried alongside plausible alternates in case top_traders mode
  // shapes its payload differently from the aggregated mode that confirmed it.
  const rows: any[] = firstDefined(data, ["traders", "top_traders", "rows", "results"]) ?? [];
  if (rows.length === 0 && data) {
    console.warn(
      `  smart_money_${category} returned 0 rows — response shape may not match the field names this ` +
        `script guesses at (see HONEST CAVEAT doc comment). Raw top-level keys: ${Object.keys(data).join(", ")}`
    );
  }

  return rows
    .map((row): LeaderboardRow | null => {
      const address = firstDefined<string>(row, ["wallet_address", "address", "wallet", "id"]);
      if (!address) return null;
      return {
        source: `smart_money_${category}`,
        rank: firstDefined<number>(row, ["rank", "position"]) ?? null,
        walletAddress: normalizeAddress(address),
        displayName: firstDefined<string>(row, ["display_name", "displayName", "name", "nickname"]) ?? null,
        pnlAllTime: firstDefined<number>(row, ["pnl", "pnl_all_time", "pnlAllTime", "total_pnl"]) ?? null,
        volume30d: firstDefined<number>(row, ["volume_30d", "volume30d", "volume"]) ?? null,
        raw: row,
      };
    })
    .filter((r): r is LeaderboardRow => r !== null);
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

/**
 * Niche-market position-holder scan. RETARGETED 2026-07-17 (see STRATEGY
 * SHIFT doc comment): used to scan the highest-volume events OVERALL — now
 * scoped to `discover sports`, a data-heavy niche verified to have real
 * current depth (NFL Champion 2027 alone: $41M volume), rather than
 * "whatever's loudest across the entire platform today," which is what fed
 * this script's worst toxic-flow candidates. Still carries no profit signal
 * at discovery time (pnlAllTime stays null below, same honest limitation as
 * before) — that's fine, because every candidate from every source in this
 * file flows through the SAME toxic-flow gate in scoreWallets.ts regardless
 * of origin.
 *
 * Weather was considered and rejected for now: only 2 thin, World-Cup-
 * adjacent weather markets exist today (~$8K/$60K volume) — not worth a
 * dedicated source yet. Swapping `discover sports` for a weather-specific
 * `discover` call here is the whole change needed once that's no longer true.
 */
async function scanNicheEventHolders(): Promise<LeaderboardRow[]> {
  const eventsData = await runBullpenJson(
    ["polymarket", "discover", "sports", "--sort", "volume", "--limit", String(TOP_EVENTS_TO_SCAN)],
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
          source: `event_holders_sports_${eventSlug}`,
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
      console.warn(`  event_holders_sports_${eventSlug} failed, skipping: ${(err as Error).message}`);
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
  console.log(
    "Scanning leaderboard: data_leaderboard_pnl (30d+90d) + smart_money (per category) + " +
      "discover_traders_overall + event_holders_sports..."
  );

  const pnl30dRows = await scanDataLeaderboardPnl("30d");
  console.log(`  data_leaderboard_pnl_30d: ${pnl30dRows.length} wallets`);
  await insertRows(pnl30dRows);

  const pnl90dRows = await scanDataLeaderboardPnl("90d");
  console.log(`  data_leaderboard_pnl_90d: ${pnl90dRows.length} wallets`);
  await insertRows(pnl90dRows);

  const smartMoneyRows: LeaderboardRow[] = [];
  for (const category of SMART_MONEY_CATEGORIES) {
    const rows = await scanSmartMoneyTopTraders(category);
    console.log(`  smart_money_${category}: ${rows.length} wallets`);
    await insertRows(rows);
    smartMoneyRows.push(...rows);
  }

  const overallRows = await scanDiscoverTradersOverall();
  console.log(`  discover_traders_overall: ${overallRows.length} wallets`);
  await insertRows(overallRows);

  const nicheEventRows = await scanNicheEventHolders();
  console.log(`  event_holders_sports_*: ${nicheEventRows.length} wallets`);
  await insertRows(nicheEventRows);

  const allRows = [...pnl30dRows, ...pnl90dRows, ...smartMoneyRows, ...overallRows, ...nicheEventRows];
  const uniqueWallets = new Set(allRows.map((r) => r.walletAddress));
  console.log(
    `Done. Inserted ${allRows.length} leaderboard_scan rows ` +
      `(${uniqueWallets.size} unique wallets this run). Coverage accumulates across runs over time.`
  );

  if (pnl30dRows.length === 0 && pnl90dRows.length === 0 && smartMoneyRows.length === 0) {
    console.warn(
      "WARNING: all three new profit-based sources (data_leaderboard_pnl_*, smart_money_*) returned 0 " +
        "rows this run — likely still hitting bullpen's backend outage (see HONEST CAVEAT doc comment at " +
        "the top of this file) rather than a real empty leaderboard. discover_traders_overall and " +
        "event_holders_sports_* still ran and may have found candidates, but the primary profit-sourced " +
        "coverage this run was meant to add did not land. Re-run once `bullpen polymarket data leaderboard " +
        "--sort pnl --limit 5 --output json` succeeds on its own."
    );
  }
}

main().catch((err) => {
  console.error("scan:leaderboard failed:", err);
  process.exit(1);
});
