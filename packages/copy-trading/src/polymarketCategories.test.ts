// Unit tests for polymarketCategories.ts. Real event/market shapes taken
// from live curls against gamma-api.polymarket.com (see polymarket_simulator.py's
// docstring on the Python side, where these were originally verified).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CATEGORY_TAG_SLUGS,
  fetchMarketEventSlug,
  resolveCategoryForEvent,
  resolveMarketCategory,
} from "./polymarketCategories";

function jsonResponse(status: number, body: unknown): Response {
  return { status, json: async () => body } as Response;
}

const NOVELTY_EVENT = {
  slug: "what-will-happen-before-gta-vi",
  tags: [{ slug: "pop-culture" }, { slug: "all" }, { slug: "politics" }, { slug: "gta-vi" }],
};
const POLITICS_EVENT = {
  slug: "democratic-presidential-nominee-2028",
  tags: [{ slug: "united-states" }, { slug: "elections" }, { slug: "politics" }],
};

describe("fetchMarketEventSlug", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("extracts the parent event slug from a market response", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, [{ events: [{ slug: "what-will-happen-before-gta-vi" }] }]));
    const slug = await fetchMarketEventSlug("new-rhianna-album-before-gta-vi-926");
    expect(slug).toBe("what-will-happen-before-gta-vi");
  });

  it("returns null when the market has no events field", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, [{ events: [] }]));
    expect(await fetchMarketEventSlug("some-slug")).toBeNull();
  });

  it("returns null when the market isn't found", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, []));
    expect(await fetchMarketEventSlug("no-such-slug")).toBeNull();
  });
});

describe("resolveCategoryForEvent", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("matches a configured category", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, [POLITICS_EVENT]));
    expect(await resolveCategoryForEvent("democratic-presidential-nominee-2028")).toBe("politics");
  });

  it("a multi-tag event matches the first configured slug in list order", async () => {
    // NOVELTY_EVENT carries both "pop-culture" (first in its raw tags) and
    // "politics" — CATEGORY_TAG_SLUGS lists "politics" before "pop-culture".
    expect(CATEGORY_TAG_SLUGS.indexOf("politics")).toBeLessThan(CATEGORY_TAG_SLUGS.indexOf("pop-culture"));
    fetchMock.mockResolvedValueOnce(jsonResponse(200, [NOVELTY_EVENT]));
    expect(await resolveCategoryForEvent("what-will-happen-before-gta-vi")).toBe("politics");
  });

  it("returns other when no configured tag matches", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, [{ slug: "some-event", tags: [{ slug: "caitlin-clark" }] }]));
    expect(await resolveCategoryForEvent("some-event")).toBe("other");
  });

  it("returns null for a falsy event slug without a network call", async () => {
    expect(await resolveCategoryForEvent(null)).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns null (not a throw) when the fetch itself fails", async () => {
    fetchMock.mockRejectedValueOnce(new Error("network error"));
    expect(await resolveCategoryForEvent("some-event")).toBeNull();
  });

  it("returns null when the event isn't found", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, []));
    expect(await resolveCategoryForEvent("no-such-event")).toBeNull();
  });
});

describe("resolveMarketCategory (composed)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("chains market lookup into event lookup into category bucketing", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, [{ events: [{ slug: "democratic-presidential-nominee-2028" }] }]))
      .mockResolvedValueOnce(jsonResponse(200, [POLITICS_EVENT]));
    expect(await resolveMarketCategory("will-gavin-newsom-win-the-2028-democratic-presidential-nomination-568")).toBe(
      "politics"
    );
  });

  it("degrades to null if the market lookup itself fails", async () => {
    fetchMock.mockRejectedValueOnce(new Error("network error"));
    expect(await resolveMarketCategory("some-slug")).toBeNull();
  });
});
