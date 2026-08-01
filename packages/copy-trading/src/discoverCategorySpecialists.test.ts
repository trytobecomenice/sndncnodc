// Unit tests for discoverCategorySpecialists.ts's pure ranking logic.
// No network, no DB — mirrors the testing shape already used for
// aggregateCategoryScores in scoreWalletCategories.test.ts.

import { describe, expect, it } from "vitest";
import {
  DEFAULT_WASH_TRADING_THRESHOLDS,
  estimatedRelativeSpread,
  filterSignificantByCategory,
  flagsWashTradingSuspicion,
  passesEntryPriceFloor,
  passesTcaFilter,
  rankAndCapCategory,
  splitTrackAndBench,
  type CategoryCandidateResult,
} from "./discoverCategorySpecialists";

const TARGET_CATEGORIES = ["politics", "sports", "crypto", "pop-culture"];

function result(overrides: Partial<CategoryCandidateResult>): CategoryCandidateResult {
  return {
    walletAddress: "0xabc",
    category: "crypto",
    score: 0.7,
    winRate: 0.7,
    tradeCount: 20,
    avgPnlUsd: 50,
    pnlTStat: 2.0,
    roi: 0.1,
    avgEntryPrice: 0.5,
    avgFeeRate: 0.05,
    washTradingSuspect: false,
    ...overrides,
  };
}

describe("filterSignificantByCategory", () => {
  it("excludes candidates below the significance threshold", () => {
    const results = [
      result({ walletAddress: "0xsignificant", pnlTStat: 2.5 }),
      result({ walletAddress: "0xnotsignificant", pnlTStat: 1.0 }),
    ];
    const { byCategory } = filterSignificantByCategory(results, 1.645, TARGET_CATEGORIES);
    const addresses = byCategory.crypto.map((r) => r.walletAddress);
    expect(addresses).toContain("0xsignificant");
    expect(addresses).not.toContain("0xnotsignificant");
  });

  it("excludes negative t-stats even if the raw score happens to be positive", () => {
    // Guards against ever accidentally treating "harmful" as "specialist."
    const results = [result({ walletAddress: "0xbad", score: 0.6, pnlTStat: -3.0 })];
    const { byCategory } = filterSignificantByCategory(results, 1.645, TARGET_CATEGORIES);
    expect(byCategory.crypto).toBeUndefined();
  });

  it("includes a result exactly at the critical value", () => {
    const results = [result({ pnlTStat: 1.645 })];
    const { byCategory } = filterSignificantByCategory(results, 1.645, TARGET_CATEGORIES);
    expect(byCategory.crypto).toHaveLength(1);
  });

  it("does NOT cap a category, even with far more than a typical quota's worth of significant candidates", () => {
    // This is the actual fix for the pre-2026-07-24 ordering bug: capping
    // must never happen before TCA gets a chance to see the full pool.
    const results = Array.from({ length: 10 }, (_, i) =>
      result({ walletAddress: `0xwallet${i}`, pnlTStat: 2.0 + i })
    );
    const { byCategory } = filterSignificantByCategory(results, 1.645, TARGET_CATEGORIES);
    expect(byCategory.crypto).toHaveLength(10);
  });

  it("keeps categories independent of each other", () => {
    const results = [
      result({ walletAddress: "0xcrypto1", category: "crypto", pnlTStat: 3.0 }),
      result({ walletAddress: "0xsports1", category: "sports", pnlTStat: 2.5 }),
    ];
    const { byCategory } = filterSignificantByCategory(results, 1.645, TARGET_CATEGORIES);
    expect(byCategory.crypto.map((r) => r.walletAddress)).toEqual(["0xcrypto1"]);
    expect(byCategory.sports.map((r) => r.walletAddress)).toEqual(["0xsports1"]);
  });

  it("returns an empty byCategory object when nothing clears the bar", () => {
    const results = [result({ pnlTStat: 0.5 })];
    const { byCategory } = filterSignificantByCategory(results, 1.645, TARGET_CATEGORIES);
    expect(byCategory).toEqual({});
  });

  it("routes a significant candidate outside the target categories to outsideTargetCategories, not byCategory", () => {
    const results = [result({ walletAddress: "0xmisc", category: "other", pnlTStat: 3.0 })];
    const { byCategory, outsideTargetCategories } = filterSignificantByCategory(results, 1.645, TARGET_CATEGORIES);
    expect(byCategory.other).toBeUndefined();
    expect(outsideTargetCategories.map((r) => r.walletAddress)).toEqual(["0xmisc"]);
  });

  it("an insignificant candidate outside the target categories is dropped entirely, not surfaced anywhere", () => {
    const results = [result({ walletAddress: "0xmisc", category: "other", pnlTStat: 0.5 })];
    const { outsideTargetCategories } = filterSignificantByCategory(results, 1.645, TARGET_CATEGORIES);
    expect(outsideTargetCategories).toEqual([]);
  });
});

describe("rankAndCapCategory", () => {
  it("sorts by pnl_t_stat descending, not by raw score", () => {
    // Deliberately contradictory ordering between score and t_stat, to
    // confirm t_stat (evidence strength) drives ranking, not the score.
    const entries = [
      result({ walletAddress: "0xhigh_score_weak_evidence", score: 0.95, pnlTStat: 1.7 }),
      result({ walletAddress: "0xlow_score_strong_evidence", score: 0.55, pnlTStat: 4.2 }),
    ];
    const ranked = rankAndCapCategory(entries, 5);
    expect(ranked[0].walletAddress).toBe("0xlow_score_strong_evidence");
    expect(ranked[1].walletAddress).toBe("0xhigh_score_weak_evidence");
  });

  it("caps at topN", () => {
    const entries = Array.from({ length: 10 }, (_, i) => result({ walletAddress: `0xwallet${i}`, pnlTStat: 2.0 + i }));
    const ranked = rankAndCapCategory(entries, 3);
    expect(ranked).toHaveLength(3);
    // Top 3 by t_stat: wallets 9, 8, 7 (t_stat 11, 10, 9).
    expect(ranked.map((r) => r.walletAddress)).toEqual(["0xwallet9", "0xwallet8", "0xwallet7"]);
  });

  it("returns an empty array for an empty input, not an error", () => {
    expect(rankAndCapCategory([], 5)).toEqual([]);
  });

  it("sorts a wash-trading-suspect candidate BELOW a clean candidate, even with a higher t-stat", () => {
    // A scarce quota slot should go to the clean candidate first — Rule 23's
    // "warning, not exclusion" principle still holds: the suspect candidate
    // isn't dropped, just de-prioritized.
    const entries = [
      result({ walletAddress: "0xsuspect_higher_tstat", pnlTStat: 5.0, washTradingSuspect: true }),
      result({ walletAddress: "0xclean_lower_tstat", pnlTStat: 3.0, washTradingSuspect: false }),
    ];
    const ranked = rankAndCapCategory(entries, 5);
    expect(ranked.map((r) => r.walletAddress)).toEqual(["0xclean_lower_tstat", "0xsuspect_higher_tstat"]);
  });

  it("a suspect candidate still fills a slot when there aren't enough clean candidates", () => {
    const entries = [result({ walletAddress: "0xonly_option", washTradingSuspect: true })];
    const ranked = rankAndCapCategory(entries, 5);
    expect(ranked.map((r) => r.walletAddress)).toEqual(["0xonly_option"]);
  });

  it("breaks ties between two suspect (or two clean) candidates by t-stat, same as before", () => {
    const entries = [
      result({ walletAddress: "0xsuspect_low", pnlTStat: 2.0, washTradingSuspect: true }),
      result({ walletAddress: "0xsuspect_high", pnlTStat: 4.0, washTradingSuspect: true }),
    ];
    const ranked = rankAndCapCategory(entries, 5);
    expect(ranked.map((r) => r.walletAddress)).toEqual(["0xsuspect_high", "0xsuspect_low"]);
  });
});

describe("splitTrackAndBench", () => {
  it("splits the top trackQuota into track, the rest into bench", () => {
    const entries = ["a", "b", "c", "d", "e"];
    const { track, bench } = splitTrackAndBench(entries, 2);
    expect(track).toEqual(["a", "b"]);
    expect(bench).toEqual(["c", "d", "e"]);
  });

  it("bench is empty when there aren't more entries than the track quota", () => {
    const entries = ["a", "b"];
    const { track, bench } = splitTrackAndBench(entries, 5);
    expect(track).toEqual(["a", "b"]);
    expect(bench).toEqual([]);
  });

  it("handles an empty input", () => {
    expect(splitTrackAndBench([], 5)).toEqual({ track: [], bench: [] });
  });

  it("composes with rankAndCapCategory's own capping: track+bench never exceeds the pre-capped input", () => {
    const entries = Array.from({ length: 20 }, (_, i) => result({ walletAddress: `0xwallet${i}`, pnlTStat: i }));
    const ranked = rankAndCapCategory(entries, 11); // quotaPerCategory(5) + benchQuotaPerCategory(6)
    const { track, bench } = splitTrackAndBench(ranked, 5);
    expect(track).toHaveLength(5);
    expect(bench).toHaveLength(6);
    // Highest t_stat wallets land in track, next-highest in bench, in order.
    expect(track.map((r) => r.walletAddress)).toEqual(["0xwallet19", "0xwallet18", "0xwallet17", "0xwallet16", "0xwallet15"]);
    expect(bench.map((r) => r.walletAddress)).toEqual(["0xwallet14", "0xwallet13", "0xwallet12", "0xwallet11", "0xwallet10", "0xwallet9"]);
  });
});

describe("category quota pipeline ordering bug fix (regression test)", () => {
  it("a TCA-rejected top-t-stat candidate is correctly replaced by a lower-t-stat-but-TCA-viable one", () => {
    // Reproduces the exact pre-2026-07-24 bug: with only 2 significant
    // candidates and a quota of 1, the OLD code (cap-then-TCA-filter) would
    // have capped to [0xhigh_tstat_bad_roi] BEFORE TCA ran, then lost that
    // slot entirely when it failed TCA — even though 0xlow_tstat_good_roi
    // was sitting right there, fully qualified, and never got a chance.
    const highTStatBadRoi = result({
      walletAddress: "0xhigh_tstat_bad_roi", pnlTStat: 5.0, roi: 0.01, avgEntryPrice: 0.5, avgFeeRate: 0.05,
    });
    const lowTStatGoodRoi = result({
      walletAddress: "0xlow_tstat_good_roi", pnlTStat: 1.7, roi: 0.3, avgEntryPrice: 0.5, avgFeeRate: 0.05,
    });
    const { byCategory } = filterSignificantByCategory([highTStatBadRoi, lowTStatGoodRoi], 1.645, TARGET_CATEGORIES);
    // Both are significant, so filterSignificantByCategory keeps BOTH (uncapped) —
    // this is what actually fixes the bug, by construction.
    expect(byCategory.crypto).toHaveLength(2);

    const tcaSurvivors = byCategory.crypto.filter((e) =>
      passesTcaFilter({ roi: e.roi, avgEntryPrice: e.avgEntryPrice, avgFeeRate: e.avgFeeRate }, 0.02)
    );
    // highTStatBadRoi fails TCA (1% roi doesn't clear ~6.5% costs at center price);
    // lowTStatGoodRoi survives.
    expect(tcaSurvivors.map((e) => e.walletAddress)).toEqual(["0xlow_tstat_good_roi"]);

    const finalRanked = rankAndCapCategory(tcaSurvivors, 1);
    expect(finalRanked.map((e) => e.walletAddress)).toEqual(["0xlow_tstat_good_roi"]);
  });
});

describe("estimatedRelativeSpread", () => {
  it("returns the center anchor (4%) for prices within [0.4, 0.6]", () => {
    expect(estimatedRelativeSpread(0.5)).toBeCloseTo(0.04);
    expect(estimatedRelativeSpread(0.4)).toBeCloseTo(0.04);
    expect(estimatedRelativeSpread(0.6)).toBeCloseTo(0.04);
  });

  it("returns the extreme anchor (15.5%) for longshot prices below 0.10 or above 0.90", () => {
    expect(estimatedRelativeSpread(0.1)).toBeCloseTo(0.155);
    expect(estimatedRelativeSpread(0.05)).toBeCloseTo(0.155);
    expect(estimatedRelativeSpread(0.9)).toBeCloseTo(0.155);
    expect(estimatedRelativeSpread(0.97)).toBeCloseTo(0.155);
  });

  it("interpolates linearly between the two anchors", () => {
    // distance from 0.5 = 0.25, halfway between 0.1 and 0.4 -> halfway between 0.04 and 0.155
    expect(estimatedRelativeSpread(0.25)).toBeCloseTo((0.04 + 0.155) / 2);
  });

  it("is symmetric around the 0.5 center price", () => {
    expect(estimatedRelativeSpread(0.3)).toBeCloseTo(estimatedRelativeSpread(0.7));
    expect(estimatedRelativeSpread(0.2)).toBeCloseTo(estimatedRelativeSpread(0.8));
  });
});

describe("passesTcaFilter", () => {
  it("rejects a null avg_entry_price as insufficient data, not a pass", () => {
    expect(passesTcaFilter({ roi: 10, avgEntryPrice: null, avgFeeRate: 0.05 }, 0.02)).toBe(false);
  });

  it("passes when roi comfortably clears slippage + fee + buffer at a center price", () => {
    // price 0.5 -> spread/2 = 2%, fee = 0.05*(1-0.5) = 2.5%, buffer 2% -> bar = 6.5%
    expect(passesTcaFilter({ roi: 0.1, avgEntryPrice: 0.5, avgFeeRate: 0.05 }, 0.02)).toBe(true);
  });

  it("rejects when roi is below the combined cost bar at a center price", () => {
    // same 6.5% bar as above, roi below it
    expect(passesTcaFilter({ roi: 0.03, avgEntryPrice: 0.5, avgFeeRate: 0.05 }, 0.02)).toBe(false);
  });

  it("rejects a thin-margin longshot edge that looked significant but can't survive the wider spread", () => {
    // price 0.05 -> spread/2 = 7.75%, fee = 0.05*0.95 = 4.75%, buffer 2% -> bar = 14.5%
    // a modest 5% roi (the kind a 100%-win-rate/$0.03-avg-profit wallet might show) fails outright
    expect(passesTcaFilter({ roi: 0.05, avgEntryPrice: 0.05, avgFeeRate: 0.05 }, 0.02)).toBe(false);
  });

  it("uses a strict inequality — exactly at the bar does not pass", () => {
    // price 0.5 -> bar = 0.02 + 0.025 + 0.02 = 0.065 exactly
    expect(passesTcaFilter({ roi: 0.065, avgEntryPrice: 0.5, avgFeeRate: 0.05 }, 0.02)).toBe(false);
  });

  it("a higher configurable safety buffer raises the bar", () => {
    const candidate = { roi: 0.08, avgEntryPrice: 0.5, avgFeeRate: 0.05 };
    expect(passesTcaFilter(candidate, 0.02)).toBe(true); // bar 6.5%, roi 8% passes
    expect(passesTcaFilter(candidate, 0.05)).toBe(false); // bar 9.5%, roi 8% now fails
  });
});

describe("flagsWashTradingSuspicion", () => {
  it("does not flag a high win rate with too small a sample (insufficient evidence)", () => {
    const candidate = { winRate: 0.95, tradeCount: 8, roi: 0.03 };
    expect(flagsWashTradingSuspicion(candidate)).toBe(false);
  });

  it("flags a near-perfect win rate, large sample, and thin roi (the wash-trading signature)", () => {
    const candidate = { winRate: 0.98, tradeCount: 40, roi: 0.03 };
    expect(flagsWashTradingSuspicion(candidate)).toBe(true);
  });

  it("does not flag a near-perfect win rate with a genuinely large roi (real edge, not suspicious)", () => {
    const candidate = { winRate: 0.98, tradeCount: 40, roi: 0.3 };
    expect(flagsWashTradingSuspicion(candidate)).toBe(false);
  });

  it("does not flag a moderate win rate even with a large sample and thin roi", () => {
    // win rate itself isn't the suspicious part here — 70% with a thin edge
    // looks like ordinary mediocre trading, not the near-guaranteed-win
    // pattern this screen targets.
    const candidate = { winRate: 0.7, tradeCount: 40, roi: 0.03 };
    expect(flagsWashTradingSuspicion(candidate)).toBe(false);
  });

  it("uses inclusive/exclusive boundaries exactly as documented", () => {
    // win rate and trade count are >= (inclusive); roi is < (exclusive) —
    // exactly-at-the-roi-ceiling does NOT count as suspiciously thin.
    const atWinRateBoundary = { winRate: 0.9, tradeCount: 20, roi: 0.05 };
    expect(flagsWashTradingSuspicion(atWinRateBoundary)).toBe(true);

    const belowWinRateBoundary = { winRate: 0.899, tradeCount: 20, roi: 0.05 };
    expect(flagsWashTradingSuspicion(belowWinRateBoundary)).toBe(false);

    const atTradeCountBoundary = { winRate: 0.95, tradeCount: 20, roi: 0.05 };
    expect(flagsWashTradingSuspicion(atTradeCountBoundary)).toBe(true);

    const belowTradeCountBoundary = { winRate: 0.95, tradeCount: 19, roi: 0.05 };
    expect(flagsWashTradingSuspicion(belowTradeCountBoundary)).toBe(false);

    const atRoiCeiling = { winRate: 0.95, tradeCount: 20, roi: 0.1 };
    expect(flagsWashTradingSuspicion(atRoiCeiling)).toBe(false);

    const justBelowRoiCeiling = { winRate: 0.95, tradeCount: 20, roi: 0.0999 };
    expect(flagsWashTradingSuspicion(justBelowRoiCeiling)).toBe(true);
  });

  it("respects custom thresholds when provided", () => {
    const candidate = { winRate: 0.8, tradeCount: 15, roi: 0.15 };
    expect(flagsWashTradingSuspicion(candidate)).toBe(false); // fails DEFAULT_WASH_TRADING_THRESHOLDS on all three counts
    expect(
      flagsWashTradingSuspicion(candidate, { minWinRate: 0.8, minTradeCount: 15, maxRoi: 0.2 })
    ).toBe(true);
  });

  it("DEFAULT_WASH_TRADING_THRESHOLDS matches the documented defaults", () => {
    expect(DEFAULT_WASH_TRADING_THRESHOLDS).toEqual({ minWinRate: 0.9, minTradeCount: 20, maxRoi: 0.1 });
  });
});

describe("passesEntryPriceFloor", () => {
  it("rejects a null avg_entry_price as untrustworthy, not a pass", () => {
    expect(passesEntryPriceFloor(null, 0.02)).toBe(false);
  });

  it("rejects a price exactly at the floor (inclusive boundary)", () => {
    expect(passesEntryPriceFloor(0.02, 0.02)).toBe(false);
  });

  it("rejects a price below the floor", () => {
    expect(passesEntryPriceFloor(0.019, 0.02)).toBe(false);
  });

  it("passes a price comfortably above the floor", () => {
    expect(passesEntryPriceFloor(0.5, 0.02)).toBe(true);
    expect(passesEntryPriceFloor(0.05, 0.02)).toBe(true);
  });

  it("is symmetric — rejects a price equally close to 1.0", () => {
    expect(passesEntryPriceFloor(0.98, 0.02)).toBe(false); // exactly at the mirrored boundary
    expect(passesEntryPriceFloor(0.981, 0.02)).toBe(false);
    expect(passesEntryPriceFloor(0.9, 0.02)).toBe(true);
  });

  it("uses the default threshold when none is given", () => {
    expect(passesEntryPriceFloor(0.001)).toBe(false);
    expect(passesEntryPriceFloor(0.5)).toBe(true);
  });

  it("regression: the real 0xc21ea96b-shaped case (avg_entry_price=$0.001, huge ROI) is rejected here, not by TCA", () => {
    // This candidate would PASS passesTcaFilter on its own (ROI comfortably
    // clears the cost bar) — confirming the entry-price floor is doing real,
    // independent work, not duplicating what TCA already catches.
    const candidate = { roi: 1.0, avgEntryPrice: 0.001, avgFeeRate: 0.05 };
    expect(passesTcaFilter(candidate, 0.02)).toBe(true);
    expect(passesEntryPriceFloor(candidate.avgEntryPrice, 0.02)).toBe(false);
  });
});
