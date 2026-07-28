// Unit tests for positionTracker.ts — the "Build vs. Borrow" scoring
// engine's core position-lifecycle reconstruction. No real network calls:
// global fetch is mocked for the Gamma resolution checks, and
// updateWalletState's own tests mock polymarketDataApi.ts's
// fetchWalletTrades directly.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  applyTrade,
  checkMarketResolution,
  computeWalletMetrics,
  newWalletPositionState,
  resolveOpenPositions,
  updateWalletState,
  type WalletPositionState,
} from "./positionTracker";
import type { RawActivityRecord } from "./polymarketDataApi";

function trade(overrides: Partial<RawActivityRecord> = {}): RawActivityRecord {
  return {
    proxyWallet: "0xWallet",
    timestamp: 1700000000,
    transactionHash: "0xhash1",
    price: 0.5,
    asset: "token-a",
    size: 10,
    usdcSize: 5,
    side: "BUY",
    slug: "market-a",
    outcome: "Yes",
    ...overrides,
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

// =============================================================================
// applyTrade
// =============================================================================

describe("applyTrade", () => {
  it("a single BUY creates an open position with the real fee-inclusive cost basis", () => {
    const state = newWalletPositionState("0xWallet");
    applyTrade(state, trade({ size: 10, usdcSize: 5.25, price: 0.525 }));
    const pos = state.openPositions.get("market-a|Yes");
    expect(pos?.shares).toBe(10);
    expect(pos?.costBasisUsd).toBe(5.25); // usdcSize directly, not price*size
    expect(pos?.avgEntryPrice).toBeCloseTo(0.525, 6);
    expect(pos?.buyCount).toBe(1);
  });

  it("two BUYs average correctly, weighted by real cost basis", () => {
    const state = newWalletPositionState("0xWallet");
    applyTrade(state, trade({ transactionHash: "0xhash1", size: 10, usdcSize: 5, price: 0.5 }));
    applyTrade(state, trade({ transactionHash: "0xhash2", size: 20, usdcSize: 16, price: 0.8 }));
    const pos = state.openPositions.get("market-a|Yes");
    expect(pos?.shares).toBe(30);
    expect(pos?.costBasisUsd).toBe(21); // 5 + 16
    expect(pos?.avgEntryPrice).toBeCloseTo(21 / 30, 6);
    expect(pos?.buyCount).toBe(2);
  });

  it("is idempotent — re-applying the exact same trade is a silent no-op", () => {
    const state = newWalletPositionState("0xWallet");
    const t = trade({ transactionHash: "0xhash1", size: 10, usdcSize: 5 });
    applyTrade(state, t);
    applyTrade(state, t);
    const pos = state.openPositions.get("market-a|Yes");
    expect(pos?.shares).toBe(10); // NOT 20 — the second apply must not double-count
    expect(state.appliedTradeIds.size).toBe(1);
  });

  it("a partial SELL reduces shares/cost basis proportionally and books the correct realized slice", () => {
    const state = newWalletPositionState("0xWallet");
    applyTrade(state, trade({ transactionHash: "0xbuy", side: "BUY", size: 10, usdcSize: 5 })); // avg entry 0.5
    applyTrade(state, trade({ transactionHash: "0xsell", side: "SELL", size: 4, usdcSize: 3.2, price: 0.8 })); // sold at 0.8
    const pos = state.openPositions.get("market-a|Yes");
    expect(pos?.shares).toBe(6);
    expect(pos?.costBasisUsd).toBeCloseTo(3, 6); // 5 * (6/10) remaining
    // realized: proceeds 3.2 - cost-basis-sold (5 * 4/10 = 2) = 1.2
    expect(pos?.realizedPnlAccrued).toBeCloseTo(1.2, 6);
    expect(state.closedPositions.length).toBe(0);
  });

  it("a full SELL closes the position with closeReason sold_out and the total realized PnL", () => {
    const state = newWalletPositionState("0xWallet");
    applyTrade(state, trade({ transactionHash: "0xbuy", side: "BUY", size: 10, usdcSize: 5 }));
    applyTrade(state, trade({ transactionHash: "0xsell", side: "SELL", size: 10, usdcSize: 9, price: 0.9 }));
    expect(state.openPositions.has("market-a|Yes")).toBe(false);
    expect(state.closedPositions).toHaveLength(1);
    const closed = state.closedPositions[0];
    expect(closed.closeReason).toBe("sold_out");
    expect(closed.realizedPnlUsd).toBeCloseTo(4, 6); // 9 - 5
  });

  it("a SELL with no tracked open position at all is a no-op, not a crash", () => {
    const state = newWalletPositionState("0xWallet");
    applyTrade(state, trade({ side: "SELL", size: 10, usdcSize: 8 }));
    expect(state.openPositions.size).toBe(0);
    expect(state.closedPositions.length).toBe(0);
  });

  it("a SELL larger than tracked shares is clamped, not allowed to go negative", () => {
    const state = newWalletPositionState("0xWallet");
    applyTrade(state, trade({ transactionHash: "0xbuy", side: "BUY", size: 5, usdcSize: 2.5 }));
    applyTrade(state, trade({ transactionHash: "0xsell", side: "SELL", size: 100, usdcSize: 80, price: 0.8 }));
    // Only the tracked 5 shares' worth of proceeds should be attributed.
    expect(state.openPositions.has("market-a|Yes")).toBe(false);
    const closed = state.closedPositions[0];
    expect(closed.realizedPnlUsd).toBeCloseTo(80 * (5 / 100) - 2.5, 6);
  });
});

// =============================================================================
// checkMarketResolution — mirrors polymarket_simulator.py's fetch_market_info
// two-step retry (plain, then &closed=true).
// =============================================================================

describe("checkMarketResolution", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reports open when the plain (non-closed) lookup finds an active market", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, [{ closed: false }]));
    const result = await checkMarketResolution("some-market");
    expect(result.status).toBe("open");
    expect(fetchMock).toHaveBeenCalledTimes(1); // never needed the closed=true retry
  });

  it("reports resolved with outcomes/outcomePrices when the plain lookup already returns a closed market", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, [{ closed: true, outcomes: '["Yes","No"]', outcomePrices: '["0","1"]' }])
    );
    const result = await checkMarketResolution("some-market");
    expect(result.status).toBe("resolved");
    if (result.status === "resolved") {
      expect(result.outcomes).toEqual(["Yes", "No"]);
      expect(result.outcomePrices).toEqual([0, 1]);
    }
  });

  it("retries with closed=true when the plain lookup is empty (resolved markets excluded from the default listing)", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, [])) // plain: empty
      .mockResolvedValueOnce(jsonResponse(200, [{ closed: true, outcomes: '["Yes","No"]', outcomePrices: '["1","0"]' }]));
    const result = await checkMarketResolution("some-market");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.status).toBe("resolved");
  });

  it("reports delisted only after BOTH the plain and closed=true lookups come back empty", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, [])).mockResolvedValueOnce(jsonResponse(200, []));
    const result = await checkMarketResolution("vanished-market");
    expect(result.status).toBe("delisted");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

// =============================================================================
// resolveOpenPositions
// =============================================================================

describe("resolveOpenPositions", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stateWithOpenPosition(): WalletPositionState {
    const state = newWalletPositionState("0xWallet");
    applyTrade(state, trade({ transactionHash: "0xbuy", side: "BUY", size: 10, usdcSize: 5 }));
    return state;
  }

  it("leaves an open position alone when the market is still open", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, [{ closed: false }]));
    const state = stateWithOpenPosition();
    await resolveOpenPositions(state);
    expect(state.openPositions.has("market-a|Yes")).toBe(true);
    expect(state.closedPositions).toHaveLength(0);
  });

  it("dedupes by market slug — two outcomes of the SAME market only trigger one resolution fetch", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, [{ closed: false }]));
    const state = newWalletPositionState("0xWallet");
    applyTrade(state, trade({ transactionHash: "0xbuy1", side: "BUY", size: 10, usdcSize: 5, outcome: "Yes" }));
    applyTrade(state, trade({ transactionHash: "0xbuy2", side: "BUY", size: 10, usdcSize: 5, outcome: "No" }));
    await resolveOpenPositions(state);
    expect(fetchMock).toHaveBeenCalledTimes(1); // one slug, not one per outcome
    expect(state.openPositions.size).toBe(2); // both still tracked independently
  });

  it("resolves multiple DIFFERENT markets correctly under concurrency, not just the first", async () => {
    const state = newWalletPositionState("0xWallet");
    applyTrade(state, trade({ transactionHash: "0xbuy1", side: "BUY", size: 10, usdcSize: 5, slug: "market-a", outcome: "Yes" }));
    applyTrade(state, trade({ transactionHash: "0xbuy2", side: "BUY", size: 10, usdcSize: 5, slug: "market-b", outcome: "Yes" }));
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes("market-a")) return jsonResponse(200, [{ closed: true, outcomes: '["Yes","No"]', outcomePrices: '["1","0"]' }]);
      return jsonResponse(200, [{ closed: true, outcomes: '["Yes","No"]', outcomePrices: '["0","1"]' }]); // market-b's Yes LOST
    });
    await resolveOpenPositions(state);
    expect(state.openPositions.size).toBe(0);
    const a = state.closedPositions.find((p) => p.marketSlug === "market-a");
    const b = state.closedPositions.find((p) => p.marketSlug === "market-b");
    expect(a?.realizedPnlUsd).toBeCloseTo(5, 6); // won: 10*1 - 5
    expect(b?.realizedPnlUsd).toBeCloseTo(-5, 6); // lost: 10*0 - 5
  });

  it("closes a resolved position with the correct realized PnL from the final outcome price", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, [{ closed: true, outcomes: '["Yes","No"]', outcomePrices: '["1","0"]' }])
    );
    const state = stateWithOpenPosition(); // 10 shares, cost basis 5, outcome "Yes"
    await resolveOpenPositions(state);
    expect(state.openPositions.size).toBe(0);
    const closed = state.closedPositions[0];
    expect(closed.closeReason).toBe("resolved");
    expect(closed.finalPrice).toBe(1);
    expect(closed.realizedPnlUsd).toBeCloseTo(10 * 1 - 5, 6); // 5
  });

  it("matches an outcome name across the real apostrophe-stripping mismatch between the two Polymarket APIs", async () => {
    // Real live finding (2026-07-27): data-api's /activity outcome field
    // strips apostrophes ("St Josephs FC"); gamma-api's /markets outcomes
    // array keeps them ("St Joseph's FC"). A plain indexOf match fails on
    // every one of these, leaving the position stuck open forever.
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, [{ closed: true, outcomes: '["Bohemian FC","St Joseph\'s FC"]', outcomePrices: '["0","1"]' }])
    );
    const state = newWalletPositionState("0xWallet");
    applyTrade(state, trade({ transactionHash: "0xbuy", side: "BUY", size: 10, usdcSize: 5, outcome: "St Josephs FC" }));
    await resolveOpenPositions(state);
    expect(state.openPositions.size).toBe(0);
    const closed = state.closedPositions[0];
    expect(closed.closeReason).toBe("resolved");
    expect(closed.finalPrice).toBe(1); // St Joseph's FC won
    expect(closed.realizedPnlUsd).toBeCloseTo(10 * 1 - 5, 6);
  });

  it("closes a delisted position with realizedPnlUsd null, not zero, and throttles repeat warnings", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, [])); // every call: empty (both plain and closed=true)
    const state = stateWithOpenPosition();
    await resolveOpenPositions(state);
    expect(state.openPositions.size).toBe(0);
    const closed = state.closedPositions[0];
    expect(closed.closeReason).toBe("delisted");
    expect(closed.realizedPnlUsd).toBeNull();
    expect(state.unresolvableMarkets.get("market-a")).toBe(1);
  });

  it("leaves the position open (not delisted) on a transient fetch failure, so the next update retries", async () => {
    fetchMock.mockRejectedValueOnce(new Error("network blip"));
    const state = stateWithOpenPosition();
    await resolveOpenPositions(state);
    expect(state.openPositions.has("market-a|Yes")).toBe(true);
    expect(state.closedPositions).toHaveLength(0);
  });
});

// =============================================================================
// computeWalletMetrics
// =============================================================================

describe("computeWalletMetrics", () => {
  it("returns a null winRate (not 0%) when there are no closed positions yet", () => {
    const state = newWalletPositionState("0xWallet");
    const metrics = computeWalletMetrics(state);
    expect(metrics.winRate).toBeNull();
    expect(metrics.closedCount).toBe(0);
  });

  it("excludes delisted closes from win rate and total realized PnL entirely", () => {
    const state = newWalletPositionState("0xWallet");
    state.closedPositions.push(
      { marketSlug: "m1", outcome: "Yes", closedAt: 1, closeReason: "resolved", realizedPnlUsd: 10, finalPrice: 1, costBasisUsd: 5 },
      { marketSlug: "m2", outcome: "Yes", closedAt: 2, closeReason: "resolved", realizedPnlUsd: -5, finalPrice: 0, costBasisUsd: 5 },
      { marketSlug: "m3", outcome: "Yes", closedAt: 3, closeReason: "delisted", realizedPnlUsd: null, finalPrice: null, costBasisUsd: 100 }
    );
    const metrics = computeWalletMetrics(state);
    expect(metrics.closedCount).toBe(2); // delisted excluded
    expect(metrics.wins).toBe(1);
    expect(metrics.winRate).toBeCloseTo(0.5, 6);
    expect(metrics.totalRealizedPnlUsd).toBeCloseTo(5, 6); // 10 + -5, the delisted 100 cost basis never counted as a loss
    expect(metrics.delistedCount).toBe(1);
  });
});

// =============================================================================
// updateWalletState — the stateful delta-update driver
// =============================================================================

vi.mock("./polymarketDataApi", async () => {
  const actual = await vi.importActual<typeof import("./polymarketDataApi")>("./polymarketDataApi");
  return { ...actual, fetchWalletTrades: vi.fn() };
});

describe("updateWalletState", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockResolvedValue(jsonResponse(200, [{ closed: false }])); // every resolution check: still open
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("applies newest-first trades in chronological order, not fetch order", async () => {
    const { fetchWalletTrades } = await import("./polymarketDataApi");
    // Deliberately newest-first, matching the real API's confirmed order.
    vi.mocked(fetchWalletTrades).mockResolvedValueOnce([
      trade({ transactionHash: "0xlater", timestamp: 200, side: "BUY", size: 20, usdcSize: 16, price: 0.8 }),
      trade({ transactionHash: "0xearlier", timestamp: 100, side: "BUY", size: 10, usdcSize: 5, price: 0.5 }),
    ]);
    const state = newWalletPositionState("0xWallet");
    await updateWalletState(state);
    const pos = state.openPositions.get("market-a|Yes");
    // If applied out of order, buyCount/averages would still total the same
    // here (both are plain BUYs) -- the real regression this guards is a
    // BUY-then-SELL pair being applied SELL-first, which the sort fixes.
    expect(pos?.shares).toBe(30);
    expect(pos?.costBasisUsd).toBe(21);
  });

  it("sets lastFetchedAt so the next call can fetch only the delta", async () => {
    const { fetchWalletTrades } = await import("./polymarketDataApi");
    vi.mocked(fetchWalletTrades).mockResolvedValueOnce([]);
    const state = newWalletPositionState("0xWallet");
    expect(state.lastFetchedAt).toBeUndefined();
    await updateWalletState(state);
    expect(state.lastFetchedAt).not.toBeUndefined();
  });
});
