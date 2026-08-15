// Unit tests for polymarketDataApi.ts — the TS twin of polymarket_data_api.py's
// pagination/backoff logic, needed for category-specific wallet scoring.
// Mocks global fetch — no real network calls.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  fetchOfficialLeaderboard,
  fetchOfficialLeaderboardPage,
  OFFICIAL_LEADERBOARD_CATEGORIES,
  fetchWalletTrades,
} from "./polymarketDataApi";

function jsonResponse(status: number, body: unknown): Response {
  return {
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

describe("fetchWalletTrades", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns a single short page without paginating further", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, [{ id: 1 }, { id: 2 }]));

    const result = await fetchWalletTrades("0xABC", { limit: 100 });

    expect(result).toEqual([{ id: 1 }, { id: 2 }]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("follows a full page with a second request at the next offset", async () => {
    const page1 = [{ id: 0 }, { id: 1 }, { id: 2 }]; // full page (== limit)
    const page2 = [{ id: 99 }]; // short page -> stop here
    fetchMock.mockResolvedValueOnce(jsonResponse(200, page1)).mockResolvedValueOnce(jsonResponse(200, page2));

    const result = await fetchWalletTrades("0xABC", { limit: 3 });

    expect(result).toEqual([...page1, ...page2]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const secondCallUrl = fetchMock.mock.calls[1][0] as string;
    expect(secondCallUrl).toContain("offset=3");
    const firstCallUrl = fetchMock.mock.calls[0][0] as string;
    expect(firstCallUrl).toContain("offset=0");
  });

  it("stops at MAX_PAGES even if every page is full, without throwing", async () => {
    // MAX_PAGES=10 full pages of 2 records each (limit=2) — must stop at the
    // page cap, not loop forever. (MAX_PAGES itself is capped at 10 so that
    // MAX_PAGES * DEFAULT_LIMIT never exceeds Polymarket's documented 5000
    // total-offset ceiling — see MAX_PAGES's own comment.)
    for (let i = 0; i < 15; i++) {
      fetchMock.mockResolvedValueOnce(jsonResponse(200, [{ id: i * 2 }, { id: i * 2 + 1 }]));
    }
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    const result = await fetchWalletTrades("0xABC", { limit: 2 });

    expect(fetchMock).toHaveBeenCalledTimes(10); // MAX_PAGES
    expect(result.length).toBe(20);
    expect(warnSpy).toHaveBeenCalledOnce();
    warnSpy.mockRestore();
  });

  it("includes start param in the request when given", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, []));
    await fetchWalletTrades("0xABC", { startEpochSeconds: 1700000000 });
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("start=1700000000");
  });

  it("omits start param when not given", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, []));
    await fetchWalletTrades("0xABC");
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).not.toContain("start=");
  });

  it("retries 429 with exponential backoff and eventually succeeds", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(429, { error: "rate limited" }))
      .mockResolvedValueOnce(jsonResponse(429, { error: "rate limited" }))
      .mockResolvedValueOnce(jsonResponse(200, [{ id: 1 }]));
    vi.useFakeTimers();
    const promise = fetchWalletTrades("0xABC");
    // Advance past both backoff sleeps (1s, 2s).
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(2000);
    const result = await promise;
    vi.useRealTimers();

    expect(result).toEqual([{ id: 1 }]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("does not retry a permanent 4xx at all", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(400, { error: "bad request" }));

    await expect(fetchWalletTrades("0xABC")).rejects.toThrow(/HTTP 400/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("gives up after MAX_RETRIES and raises", async () => {
    for (let i = 0; i < 10; i++) {
      fetchMock.mockResolvedValueOnce(jsonResponse(429, { error: "rate limited" }));
    }
    vi.useFakeTimers();
    const promise = fetchWalletTrades("0xABC");
    const expectation = expect(promise).rejects.toThrow(/HTTP 429/);
    await vi.advanceTimersByTimeAsync(1000 + 2000 + 4000 + 8000 + 1000);
    await expectation;
    vi.useRealTimers();
  });
});

describe("official leaderboard", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("freezes the documented category universe", () => {
    expect(OFFICIAL_LEADERBOARD_CATEGORIES).toEqual([
      "OVERALL", "POLITICS", "SPORTS", "CRYPTO", "CULTURE", "MENTIONS",
      "WEATHER", "ECONOMICS", "TECH", "FINANCE",
    ]);
  });

  it("uses the real timePeriod parameter and exact category", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, []));
    await fetchOfficialLeaderboardPage({ category: "POLITICS", timePeriod: "MONTH", orderBy: "PNL" });
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("category=POLITICS");
    expect(url).toContain("timePeriod=MONTH");
    expect(url).not.toContain("window=");
  });

  it("paginates beyond the 50-row page instead of treating it as a total cap", async () => {
    const full = Array.from({ length: 50 }, (_, rank) => ({ rank }));
    fetchMock.mockResolvedValueOnce(jsonResponse(200, full)).mockResolvedValueOnce(jsonResponse(200, [{ rank: 51 }]));
    const rows = await fetchOfficialLeaderboard({
      category: "OVERALL", timePeriod: "MONTH", orderBy: "PNL", maxRows: 100,
    });
    expect(rows).toHaveLength(51);
    expect(fetchMock.mock.calls[1][0]).toContain("offset=50");
  });

  it("rejects page sizes above the official maximum", async () => {
    await expect(fetchOfficialLeaderboardPage({
      category: "OVERALL", timePeriod: "MONTH", orderBy: "PNL", limit: 51,
    })).rejects.toThrow(/\[1, 50\]/);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
