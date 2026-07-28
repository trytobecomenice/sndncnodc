// PositionTracker — reconstructs an ARBITRARY external wallet's real
// position lifecycle (open positions, weighted-average cost basis,
// realized PnL) from raw Polymarket trade history, for the in-house
// scoring engine (2026-07-27, "Build vs. Borrow" architecture).
//
// COST BASIS: uses RawActivityRecord.usdcSize directly, NOT price*size and
// NOT polymarket_simulator.py's fee formula. This was verified live and
// documented in polymarket_data_api.py's own module docstring: usdcSize IS
// the actual on-chain settled amount, already fee-inclusive (confirmed
// exactly against two independent real trades backing out to Polymarket's
// documented feeRate). The nonlinear fee formula (fee = shares * feeRate *
// price * (1-price)) only matters for SIMULATING a fill that hasn't
// happened yet (our own prospective paper trades) — reconstructing a
// wallet's ALREADY-SETTLED history needs no such estimate, the real number
// is already sitting in the trade record. This is a real simplification
// from what was originally scoped, not an oversight.
//
// WIN RATE SCOPE: computeWalletMetrics only considers CLOSED positions
// (resolved or sold out). An open position's "win/loss" is genuinely
// unknown until it resolves — same "missing data isn't evidence" posture
// as every other gate in this codebase (toxic-flow, liquidity-farming,
// zombie-position). No mark-to-market/unrealized-PnL estimate is computed
// here; that would need the fee-formula-based fill simulation this module
// deliberately avoids, for a number this scoring use case doesn't need.
//
// ORDERING: data-api.polymarket.com/activity returns newest-first
// (confirmed live) — trades MUST be applied oldest-first for the
// weighted-average math to mean anything, so updateWalletState sorts
// before applying. Never assume caller-supplied order.
//
// DELISTED MARKETS: mirrors bot.py's fetch_market_info two-step retry
// (plain fetch, then retry with closed=true before concluding "genuinely
// missing") and sweep_zombie_positions' throttled-alert pattern (first
// failure logs, repeats are throttled, never silently retried forever).

import { mapWithConcurrency } from "@copybot/shared";
import { fetchWalletTrades, type RawActivityRecord } from "./polymarketDataApi";

const GAMMA_API_HOST = "https://gamma-api.polymarket.com";
const REQUEST_HEADERS = { "User-Agent": "polymarket-copybot/1.0 (+personal research bot)", Accept: "application/json" };
// Every Nth consecutive "still can't resolve this market" failure re-warns
// — same throttling reasoning as bot.py's _zombie_unresolvable_failures/
// _closeout_fetch_failures: log the first failure, don't spam every call
// after that, but don't let a chronically-broken market vanish from the
// log entirely either.
const UNRESOLVABLE_LOG_EVERY = 4;
// How many resolution checks run at once per wallet. Found live
// (2026-07-27): resolveOpenPositions originally checked one held market
// at a time, and a single hyperactive wallet holding 100+ distinct
// markets made the reconciliation script stall on its very first wallet.
// Same concurrency level as scoreWallets.ts's own pass-1/pass-2 fan-out
// (PASS1_CONCURRENCY/PASS2_CONCURRENCY = 5) — not tuned independently,
// reusing an already-reasonable number rather than guessing a new one.
const RESOLUTION_CHECK_CONCURRENCY = 5;

/**
 * Found live (2026-07-27, first real reconciliation run): the same
 * real-world outcome name comes back DIFFERENTLY spelled from two
 * different Polymarket APIs. data-api.polymarket.com/activity's
 * `outcome` field strips apostrophes ("St Josephs FC", "OHiggins FC",
 * "Côte dIvoire"); gamma-api.polymarket.com/markets' `outcomes` array
 * keeps them ("St Joseph's FC", "O'Higgins FC", "Côte d'Ivoire") — six+
 * confirmed occurrences in a single wallet's history, all foreign-team
 * names. A plain `indexOf` match fails on every one of these, leaving the
 * position stuck "open" forever (never resolved, never counted) with no
 * future run ever fixing it, since the mismatch is structural, not
 * transient. Strips apostrophe-like characters from BOTH sides before
 * comparing, deliberately not lowercasing or fixing anything else —
 * apostrophes are the one confirmed, evidenced difference; over-
 * normalizing beyond that risks silently matching two GENUINELY
 * different outcomes (e.g. two similarly-named but distinct teams).
 */
function normalizeOutcomeName(name: string): string {
  return name.replace(/['‘’ʼ`]/g, "");
}

export interface OpenPosition {
  marketSlug: string;
  outcome: string;
  shares: number;
  costBasisUsd: number; // fee-inclusive (usdcSize-derived), weighted-average
  avgEntryPrice: number;
  buyCount: number;
  realizedPnlAccrued: number; // running total from any partial sells so far
}

export interface ClosedPosition {
  marketSlug: string;
  outcome: string;
  closedAt: number;
  closeReason: "resolved" | "sold_out" | "delisted";
  // null ONLY for "delisted" — a genuinely unknown outcome, not a zero.
  // Every aggregate below must filter these out, not average them in as 0.
  realizedPnlUsd: number | null;
  finalPrice: number | null;
  costBasisUsd: number;
}

export interface WalletPositionState {
  walletAddress: string;
  lastFetchedAt: number | undefined; // undefined = never fetched (full history next time)
  openPositions: Map<string, OpenPosition>; // key: `${marketSlug}|${outcome}`
  closedPositions: ClosedPosition[];
  appliedTradeIds: Set<string>; // idempotency — see applyTrade
  unresolvableMarkets: Map<string, number>; // marketSlug -> consecutive failure count
}

export function newWalletPositionState(walletAddress: string): WalletPositionState {
  return {
    walletAddress,
    lastFetchedAt: undefined,
    openPositions: new Map(),
    closedPositions: [],
    appliedTradeIds: new Set(),
    unresolvableMarkets: new Map(),
  };
}

function positionKey(marketSlug: string, outcome: string): string {
  return `${marketSlug}|${outcome}`;
}

/**
 * Same composite trade-id format polymarket_data_api.py already uses for
 * its own seen-trade dedup (tx_hash:asset:side:timestamp) — kept identical
 * across languages deliberately, not reinvented, since it's a proven,
 * already-verified-unique key.
 */
function tradeId(trade: RawActivityRecord): string {
  return `${trade.transactionHash}:${trade.asset}:${trade.side}:${trade.timestamp}`;
}

/**
 * Applies one real trade to wallet state, mutating it in place. Mirrors
 * bot.py's weighted-average-cost-basis math exactly (new_cost/new_shares),
 * fed with the trade's real usdcSize/size rather than our own execution's
 * reported cost — see module doc comment for why no fee formula is needed.
 *
 * Idempotent: re-applying an already-seen trade (tradeId already in
 * state.appliedTradeIds) is a silent no-op, not an error — a delta re-fetch
 * whose window overlaps the last one is the expected, normal case, not a
 * bug to guard against with an exception.
 *
 * A SELL for more shares than are currently tracked open (a real
 * possibility if tracking started mid-history, after the position was
 * already partially built) is clamped to the shares actually held rather
 * than going negative — the untracked portion is a known, accepted gap
 * from partial history, not silently fabricated.
 */
export function applyTrade(state: WalletPositionState, trade: RawActivityRecord): void {
  const id = tradeId(trade);
  if (state.appliedTradeIds.has(id)) return;
  state.appliedTradeIds.add(id);

  const key = positionKey(trade.slug, trade.outcome);

  if (trade.side === "BUY") {
    const existing = state.openPositions.get(key);
    const prevShares = existing?.shares ?? 0;
    const prevCost = existing?.costBasisUsd ?? 0;
    const newShares = prevShares + trade.size;
    const newCost = prevCost + trade.usdcSize;
    state.openPositions.set(key, {
      marketSlug: trade.slug,
      outcome: trade.outcome,
      shares: newShares,
      costBasisUsd: newCost,
      avgEntryPrice: newShares > 0 ? newCost / newShares : 0,
      buyCount: (existing?.buyCount ?? 0) + 1,
      realizedPnlAccrued: existing?.realizedPnlAccrued ?? 0,
    });
    return;
  }

  // SELL
  const existing = state.openPositions.get(key);
  if (!existing || existing.shares <= 0) {
    // A sell with no tracked open position at all (fully pre-dates our
    // fetch window) — nothing to reduce, and nothing to safely attribute
    // a realized PnL slice to. Not an error; just untracked history.
    return;
  }
  const soldShares = Math.min(trade.size, existing.shares);
  const fractionSold = soldShares / existing.shares;
  const costBasisSold = existing.costBasisUsd * fractionSold;
  // Proceeds for the CLAMPED portion only, proportional to what we're
  // actually attributing — a sell larger than our tracked shares still
  // only closes out what we know we hold.
  const proceeds = trade.size > 0 ? trade.usdcSize * (soldShares / trade.size) : 0;
  const realizedSlice = proceeds - costBasisSold;

  const remainingShares = existing.shares - soldShares;
  const remainingCost = existing.costBasisUsd - costBasisSold;
  const accrued = existing.realizedPnlAccrued + realizedSlice;

  if (remainingShares <= 1e-9) {
    state.openPositions.delete(key);
    state.closedPositions.push({
      marketSlug: trade.slug,
      outcome: trade.outcome,
      closedAt: trade.timestamp,
      closeReason: "sold_out",
      realizedPnlUsd: accrued,
      finalPrice: trade.price,
      costBasisUsd: existing.costBasisUsd,
    });
    return;
  }

  state.openPositions.set(key, {
    ...existing,
    shares: remainingShares,
    costBasisUsd: remainingCost,
    avgEntryPrice: remainingShares > 0 ? remainingCost / remainingShares : 0,
    realizedPnlAccrued: accrued,
  });
}

export type MarketResolution =
  | { status: "open" }
  | { status: "resolved"; outcomes: string[]; outcomePrices: number[] }
  | { status: "delisted" };

/**
 * Two-step Gamma lookup, mirroring polymarket_simulator.py's
 * fetch_market_info exactly: Gamma's default /markets listing excludes
 * already-resolved markets, so a plain miss is retried once with
 * &closed=true before concluding the market is genuinely gone (renamed/
 * delisted) rather than just resolved. Verified live (2026-07-27): a
 * resolved market's object carries closed=true, outcomePrices as strings
 * in the SAME index order as outcomes (["0","1"] = second outcome won),
 * and umaResolutionStatus="resolved".
 */
export async function checkMarketResolution(marketSlug: string, timeoutMs = 10000): Promise<MarketResolution> {
  const plain = await fetchGammaMarket(marketSlug, false, timeoutMs);
  if (plain) return parseGammaMarket(plain);
  const closed = await fetchGammaMarket(marketSlug, true, timeoutMs);
  if (closed) return parseGammaMarket(closed);
  return { status: "delisted" };
}

async function fetchGammaMarket(marketSlug: string, closedParam: boolean, timeoutMs: number): Promise<GammaMarket | null> {
  const params = new URLSearchParams({ slug: marketSlug });
  if (closedParam) params.set("closed", "true");
  const url = `${GAMMA_API_HOST}/markets?${params.toString()}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { headers: REQUEST_HEADERS, signal: controller.signal });
    if (!response.ok) return null;
    const data = (await response.json()) as GammaMarket[];
    return data.length > 0 ? data[0] : null;
  } finally {
    clearTimeout(timer);
  }
}

interface GammaMarket {
  outcomes?: string; // JSON-encoded string array, matches polymarket_simulator.py's parsing
  outcomePrices?: string; // JSON-encoded string array, same index order as outcomes
  closed?: boolean;
}

function parseGammaMarket(market: GammaMarket): MarketResolution {
  if (!market.closed) return { status: "open" };
  const outcomes: string[] = JSON.parse(market.outcomes ?? "[]");
  const outcomePrices: number[] = JSON.parse(market.outcomePrices ?? "[]").map((p: string) => Number(p));
  return { status: "resolved", outcomes, outcomePrices };
}

/**
 * Checks resolution for every currently-open position and closes whatever
 * has resolved or gone delisted, mutating state in place. Scoped ONLY to
 * held markets (never a bulk/history-wide resolution sweep) — the same
 * "only pay for what you actually hold" principle as bot.py's
 * run_closeout_sweep.
 *
 * Resolution checks fan out with bounded concurrency
 * (RESOLUTION_CHECK_CONCURRENCY), deduplicated by market slug first — a
 * wallet holding both Yes and No of the SAME market only needs one Gamma
 * call, not one per outcome. Found live (2026-07-27): the original
 * sequential, one-market-at-a-time version made the reconciliation
 * script stall on its very first (hyperactive, 100+ distinct markets)
 * wallet. All the network I/O happens in this fan-out; the actual state
 * mutations below run single-threaded afterward, so there's no risk of
 * two concurrent lanes racing on the same Map.
 */
export async function resolveOpenPositions(state: WalletPositionState): Promise<void> {
  const uniqueSlugs = Array.from(new Set(Array.from(state.openPositions.values()).map((p) => p.marketSlug)));

  const resolutions = await mapWithConcurrency(uniqueSlugs, RESOLUTION_CHECK_CONCURRENCY, async (slug) => {
    try {
      return { slug, resolution: await checkMarketResolution(slug) };
    } catch (err) {
      return { slug, error: (err as Error).message };
    }
  });
  const resolutionBySlug = new Map(resolutions.map((r) => [r.slug, r]));

  for (const [key, pos] of Array.from(state.openPositions.entries())) {
    const outcome = resolutionBySlug.get(pos.marketSlug)!;
    if ("error" in outcome) {
      console.warn(`  checkMarketResolution failed for ${pos.marketSlug}: ${outcome.error}`);
      continue; // transient fetch failure -- retry next update, not a delisted verdict
    }
    const resolution = outcome.resolution;

    if (resolution.status === "open") {
      state.unresolvableMarkets.delete(pos.marketSlug);
      continue;
    }

    if (resolution.status === "delisted") {
      const failures = (state.unresolvableMarkets.get(pos.marketSlug) ?? 0) + 1;
      state.unresolvableMarkets.set(pos.marketSlug, failures);
      if (failures === 1 || failures % UNRESOLVABLE_LOG_EVERY === 0) {
        console.warn(
          `  ${pos.marketSlug} unresolvable (${failures} consecutive check(s), repeats throttled) — ` +
            `flagging position as delisted rather than guessing a PnL for it`
        );
      }
      state.openPositions.delete(key);
      state.closedPositions.push({
        marketSlug: pos.marketSlug,
        outcome: pos.outcome,
        closedAt: Math.floor(Date.now() / 1000),
        closeReason: "delisted",
        realizedPnlUsd: null,
        finalPrice: null,
        costBasisUsd: pos.costBasisUsd,
      });
      continue;
    }

    // resolved
    state.unresolvableMarkets.delete(pos.marketSlug);
    const normalizedTarget = normalizeOutcomeName(pos.outcome);
    const outcomeIndex = resolution.outcomes.findIndex((o) => normalizeOutcomeName(o) === normalizedTarget);
    if (outcomeIndex === -1) {
      console.warn(`  ${pos.marketSlug} resolved but outcome ${JSON.stringify(pos.outcome)} not found in ${resolution.outcomes}`);
      continue;
    }
    const finalPrice = resolution.outcomePrices[outcomeIndex];
    state.openPositions.delete(key);
    state.closedPositions.push({
      marketSlug: pos.marketSlug,
      outcome: pos.outcome,
      closedAt: Math.floor(Date.now() / 1000),
      closeReason: "resolved",
      realizedPnlUsd: pos.realizedPnlAccrued + (pos.shares * finalPrice - pos.costBasisUsd),
      finalPrice,
      costBasisUsd: pos.costBasisUsd,
    });
  }
}

/**
 * Fetches everything new since state.lastFetchedAt (full history the
 * first time, a bounded delta every time after), applies it in
 * chronological order (the API returns newest-first — confirmed live —
 * so this always sorts before applying, never trusts fetch order), then
 * resolves whatever's now due. Mutates and returns the same state object.
 */
export async function updateWalletState(state: WalletPositionState): Promise<WalletPositionState> {
  const trades = await fetchWalletTrades(state.walletAddress, {
    startEpochSeconds: state.lastFetchedAt,
  });
  const chronological = [...trades].sort((a, b) => a.timestamp - b.timestamp);
  for (const trade of chronological) {
    applyTrade(state, trade);
  }
  await resolveOpenPositions(state);
  state.lastFetchedAt = Math.floor(Date.now() / 1000);
  return state;
}

export interface WalletMetrics {
  closedCount: number; // resolved + sold_out only, delisted excluded
  wins: number;
  winRate: number | null; // null if closedCount is 0 -- unknown, not 0%
  totalRealizedPnlUsd: number;
  openPositionCount: number;
  delistedCount: number;
}

/**
 * Aggregates the metrics the scoring engine actually wants — deliberately
 * excludes "delisted" closes (realizedPnlUsd === null) from every
 * computation rather than treating the null as a zero, which would
 * silently understate a wallet's true win rate/PnL for every wallet that
 * happens to hold a since-vanished market.
 */
export function computeWalletMetrics(state: WalletPositionState): WalletMetrics {
  const scored = state.closedPositions.filter((p) => p.realizedPnlUsd !== null) as Array<
    ClosedPosition & { realizedPnlUsd: number }
  >;
  const wins = scored.filter((p) => p.realizedPnlUsd > 0).length;
  const totalRealizedPnlUsd = scored.reduce((sum, p) => sum + p.realizedPnlUsd, 0);
  return {
    closedCount: scored.length,
    wins,
    winRate: scored.length > 0 ? wins / scored.length : null,
    totalRealizedPnlUsd,
    openPositionCount: state.openPositions.size,
    delistedCount: state.closedPositions.filter((p) => p.closeReason === "delisted").length,
  };
}
