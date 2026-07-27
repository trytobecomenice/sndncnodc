// Unit tests for reviewOutcomes.ts's pure decision logic (Stage 1 of the
// Rule 22 design — see docs/copy-trading/RISK_MANAGEMENT.md). No network,
// no DB — mirrors the testing shape already used for aggregateCategoryScores
// / rankSpecialistsByCategory: extract the pure logic, leave DB IO untested
// directly.

import { describe, expect, it } from "vitest";
import {
  bucketCalibration,
  buildOutcomeReviewRow,
  computeBrierCalibration,
  computeBrierScore,
  computeStructuralBreaks,
  detectStructuralBreak,
  filterUnreviewedTrades,
  tCriticalValue,
  welchTTest,
  type BrierCalibrationInput,
  type ClosedPaperTradeInput,
  type ContributingScoreFactors,
  type StructuralBreakInput,
} from "./reviewOutcomes";

function trade(overrides: Partial<ClosedPaperTradeInput>): ClosedPaperTradeInput {
  return {
    id: "trade-1",
    marketSlug: "some-market",
    outcome: "Yes",
    walletAddress: "0xabc",
    closedAt: new Date("2026-07-23T00:00:00Z"),
    closeReason: "source_sell",
    realizedPnlUsd: 5.0,
    ...overrides,
  };
}

describe("buildOutcomeReviewRow", () => {
  it("skips (does not guess) when realized_pnl_usd is null", () => {
    const row = buildOutcomeReviewRow(trade({ realizedPnlUsd: null }), null);
    expect(row.skipped).toBe("missing_pnl");
  });

  it("marks was_correct_call true for a positive realized PnL", () => {
    const row = buildOutcomeReviewRow(trade({ realizedPnlUsd: 12.5 }), null);
    if (row.skipped) throw new Error("expected a written row");
    expect(row.values.wasCorrectCall).toBe(true);
    expect(row.values.pnlUsd).toBe(12.5);
  });

  it("marks was_correct_call false for a zero or negative realized PnL", () => {
    const zero = buildOutcomeReviewRow(trade({ realizedPnlUsd: 0 }), null);
    const negative = buildOutcomeReviewRow(trade({ realizedPnlUsd: -3.2 }), null);
    if (zero.skipped || negative.skipped) throw new Error("expected written rows");
    expect(zero.values.wasCorrectCall).toBe(false);
    expect(negative.values.wasCorrectCall).toBe(false);
  });

  it("mirrors close_reason into final_outcome (v1 simplification)", () => {
    const row = buildOutcomeReviewRow(trade({ closeReason: "trailing_tp" }), null);
    if (row.skipped) throw new Error("expected a written row");
    expect(row.values.finalOutcome).toBe("trailing_tp");
  });

  it("falls back final_outcome to 'unknown' rather than a null insert", () => {
    const row = buildOutcomeReviewRow(trade({ closeReason: null }), null);
    if (row.skipped) throw new Error("expected a written row");
    expect(row.values.finalOutcome).toBe("unknown");
  });

  it("falls back resolved_at to now when closed_at is somehow null", () => {
    const before = Date.now();
    const row = buildOutcomeReviewRow(trade({ closedAt: null }), null);
    if (row.skipped) throw new Error("expected a written row");
    expect(row.values.resolvedAt.getTime()).toBeGreaterThanOrEqual(before);
  });

  it("uses the real closed_at when present, not now", () => {
    const closedAt = new Date("2020-01-01T00:00:00Z");
    const row = buildOutcomeReviewRow(trade({ closedAt }), null);
    if (row.skipped) throw new Error("expected a written row");
    expect(row.values.resolvedAt).toEqual(closedAt);
  });

  it("leaves contributing_score_factors_json null when no score snapshot exists (honest limitation)", () => {
    const row = buildOutcomeReviewRow(trade({}), null);
    if (row.skipped) throw new Error("expected a written row");
    expect(row.values.contributingScoreFactorsJson).toBeNull();
  });

  it("copies score_breakdown + rule_set_version through into contributing_score_factors_json", () => {
    const scoreFactors: ContributingScoreFactors = {
      score_breakdown: { category: "crypto", sizing_tier: "category" },
      rule_set_version: 3,
    };
    const row = buildOutcomeReviewRow(trade({}), scoreFactors);
    if (row.skipped) throw new Error("expected a written row");
    expect(JSON.parse(row.values.contributingScoreFactorsJson as string)).toEqual(scoreFactors);
  });

  it("carries market_slug/outcome/wallet_address/paper_trade_id through unchanged", () => {
    const row = buildOutcomeReviewRow(
      trade({ id: "trade-42", marketSlug: "m", outcome: "No", walletAddress: "0xdef" }),
      null
    );
    if (row.skipped) throw new Error("expected a written row");
    expect(row.values.paperTradeId).toBe("trade-42");
    expect(row.values.marketSlug).toBe("m");
    expect(row.values.outcome).toBe("No");
    expect(row.values.walletAddress).toBe("0xdef");
  });
});

describe("filterUnreviewedTrades", () => {
  it("excludes trades already present in the reviewed set", () => {
    const trades = [{ id: "a" }, { id: "b" }, { id: "c" }];
    const result = filterUnreviewedTrades(trades, new Set(["b"]));
    expect(result.map((t) => t.id)).toEqual(["a", "c"]);
  });

  it("returns everything when the reviewed set is empty", () => {
    const trades = [{ id: "a" }, { id: "b" }];
    const result = filterUnreviewedTrades(trades, new Set());
    expect(result).toHaveLength(2);
  });

  it("returns nothing when every trade is already reviewed", () => {
    const trades = [{ id: "a" }, { id: "b" }];
    const result = filterUnreviewedTrades(trades, new Set(["a", "b"]));
    expect(result).toEqual([]);
  });
});

// ============================================================
// Stage 2: Brier score calibration
// ============================================================

describe("computeBrierScore", () => {
  it("is 0 for a perfectly calibrated, always-correct forecast", () => {
    const inputs = Array.from({ length: 5 }, () => ({ forecastWinRate: 1, actualOutcome: true }));
    expect(computeBrierScore(inputs)).toBeCloseTo(0);
  });

  it("is 1 for a maximally overconfident, always-wrong forecast", () => {
    const inputs = Array.from({ length: 5 }, () => ({ forecastWinRate: 1, actualOutcome: false }));
    expect(computeBrierScore(inputs)).toBeCloseTo(1);
  });

  it("is 0.25 for the textbook 'always guess 50%' reference case", () => {
    const inputs = [
      { forecastWinRate: 0.5, actualOutcome: true },
      { forecastWinRate: 0.5, actualOutcome: false },
    ];
    expect(computeBrierScore(inputs)).toBeCloseTo(0.25);
  });

  it("rewards a well-calibrated 70% forecast that wins about 70% of the time", () => {
    const wins = Array.from({ length: 7 }, () => ({ forecastWinRate: 0.7, actualOutcome: true }));
    const losses = Array.from({ length: 3 }, () => ({ forecastWinRate: 0.7, actualOutcome: false }));
    // 7*(0.3^2) + 3*(0.7^2) = 7*0.09 + 3*0.49 = 0.63 + 1.47 = 2.1 / 10 = 0.21
    expect(computeBrierScore([...wins, ...losses])).toBeCloseTo(0.21);
  });
});

describe("bucketCalibration", () => {
  it("groups into the correct quintile and computes predicted/actual per bucket", () => {
    const inputs = [
      { forecastWinRate: 0.85, actualOutcome: true },
      { forecastWinRate: 0.9, actualOutcome: true },
      { forecastWinRate: 0.82, actualOutcome: false },
    ];
    const buckets = bucketCalibration(inputs);
    expect(buckets).toHaveLength(1);
    expect(buckets[0].bucketLabel).toBe("[0.8, 1.0]");
    expect(buckets[0].n).toBe(3);
    expect(buckets[0].predictedMeanWinRate).toBeCloseTo((0.85 + 0.9 + 0.82) / 3);
    expect(buckets[0].actualWinRate).toBeCloseTo(2 / 3);
  });

  it("omits empty buckets rather than reporting them at n=0", () => {
    const inputs = [{ forecastWinRate: 0.95, actualOutcome: true }];
    const buckets = bucketCalibration(inputs);
    expect(buckets).toHaveLength(1);
    expect(buckets[0].bucketLabel).toBe("[0.8, 1.0]");
  });

  it("uses a half-open interval except for the final bucket, which is closed", () => {
    // 0.2 belongs to [0.2, 0.4), not [0.0, 0.2) — and 1.0 belongs to the
    // final closed bucket [0.8, 1.0], not excluded entirely.
    const buckets = bucketCalibration([
      { forecastWinRate: 0.2, actualOutcome: true },
      { forecastWinRate: 1.0, actualOutcome: true },
    ]);
    const labels = buckets.map((b) => b.bucketLabel);
    expect(labels).toContain("[0.2, 0.4)");
    expect(labels).toContain("[0.8, 1.0]");
  });
});

describe("computeBrierCalibration", () => {
  function input(overrides: Partial<BrierCalibrationInput>): BrierCalibrationInput {
    return { walletAddress: "0xabc", category: "crypto", forecastWinRate: 0.7, actualOutcome: true, ...overrides };
  }

  it("omits a group below the minimum sample gate", () => {
    const inputs = Array.from({ length: 3 }, () => input({}));
    expect(computeBrierCalibration(inputs, "wallet", 5)).toEqual([]);
  });

  it("computes a result once a group clears the sample gate", () => {
    const inputs = Array.from({ length: 5 }, () => input({}));
    const results = computeBrierCalibration(inputs, "wallet", 5);
    expect(results).toHaveLength(1);
    expect(results[0].key).toBe("0xabc");
    expect(results[0].n).toBe(5);
  });

  it("pools across categories when grouping by wallet, but keeps categories separate when grouping by wallet_category", () => {
    const inputs = [
      ...Array.from({ length: 3 }, () => input({ category: "crypto" })),
      ...Array.from({ length: 3 }, () => input({ category: "sports" })),
    ];
    const byWallet = computeBrierCalibration(inputs, "wallet", 5);
    expect(byWallet).toHaveLength(1); // 3+3=6 pooled, clears a 5-sample gate
    expect(byWallet[0].n).toBe(6);

    const byWalletCategory = computeBrierCalibration(inputs, "wallet_category", 5);
    expect(byWalletCategory).toEqual([]); // 3 each, neither clears a 5-sample gate alone
  });
});

// ============================================================
// Stage 3: structural-break test
// ============================================================

describe("tCriticalValue", () => {
  it("returns the exact tabulated value for df in the documented [9,18] range", () => {
    expect(tCriticalValue(9)).toBeCloseTo(2.262);
    expect(tCriticalValue(18)).toBeCloseTo(2.101);
  });

  it("falls back to the normal approximation (1.96) for large df", () => {
    expect(tCriticalValue(30)).toBeCloseTo(1.96);
    expect(tCriticalValue(1000)).toBeCloseTo(1.96);
  });

  it("uses the most conservative tabulated value (df=9's) rather than extrapolating below df=9", () => {
    expect(tCriticalValue(5)).toBeCloseTo(2.262);
  });

  it("rounds a fractional df down before lookup", () => {
    expect(tCriticalValue(12.9)).toBeCloseTo(tCriticalValue(12));
  });
});

describe("welchTTest", () => {
  it("computes a known t-stat and Welch-Satterthwaite df for two equal-variance samples", () => {
    const early = [0, 2, 4, 6, 8]; // mean 4, sample variance 10
    const recent = [10, 12, 14, 16, 18]; // mean 14, sample variance 10
    const { tStat, degreesOfFreedom } = welchTTest(early, recent);
    // se^2 = 10/5 + 10/5 = 4; t = (14-4)/2 = 5
    expect(tStat).toBeCloseTo(5);
    // equal variances, equal n -> Welch df reduces to n1+n2-2 = 8
    expect(degreesOfFreedom).toBeCloseTo(8);
  });

  it("is positive when recent outperforms early (improved)", () => {
    const { tStat } = welchTTest([1, 2, 3], [10, 11, 12]);
    expect(tStat).toBeGreaterThan(0);
  });

  it("is negative when recent underperforms early (declined)", () => {
    const { tStat } = welchTTest([10, 11, 12], [1, 2, 3]);
    expect(tStat).toBeLessThan(0);
  });

  it("is a finite sentinel (not Infinity) for identical zero-variance samples with different means", () => {
    const { tStat } = welchTTest([1, 1, 1], [5, 5, 5]);
    expect(Number.isFinite(tStat)).toBe(true);
    expect(tStat).toBeGreaterThan(1000);
    expect(JSON.parse(JSON.stringify({ tStat })).tStat).toBe(tStat); // survives round-trip, unlike Infinity
  });

  it("is exactly 0 for identical zero-variance samples with the SAME mean (no evidence of a shift)", () => {
    const { tStat } = welchTTest([3, 3, 3], [3, 3, 3]);
    expect(tStat).toBe(0);
  });
});

describe("detectStructuralBreak", () => {
  it("returns null when there isn't enough history for two full windows", () => {
    expect(detectStructuralBreak([1, 2, 3, 4, 5], 3)).toBeNull(); // needs 6, has 5
  });

  it("returns a result at exactly two full windows' worth of history", () => {
    const result = detectStructuralBreak([1, 2, 3, 4, 5, 6], 3);
    expect(result).not.toBeNull();
  });

  it("flags a clear, large improvement", () => {
    const result = detectStructuralBreak([1, 1, 1, 50, 50, 50], 3);
    expect(result?.flagged).toBe(true);
    expect(result?.direction).toBe("improved");
  });

  it("flags a clear, large decline", () => {
    const result = detectStructuralBreak([50, 50, 50, 1, 1, 1], 3);
    expect(result?.flagged).toBe(true);
    expect(result?.direction).toBe("declined");
  });

  it("does not flag two windows with no real difference", () => {
    const result = detectStructuralBreak([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], 10);
    expect(result?.flagged).toBe(false);
  });

  it("only uses the MOST RECENT two windows, ignoring older history beyond them", () => {
    // Ancient history (huge negative) should not affect a break test comparing
    // only the two most recent windows.
    const ancient = [-1000, -1000, -1000];
    const result = detectStructuralBreak([...ancient, 1, 1, 1, 50, 50, 50], 3);
    expect(result?.earlyMeanPnl).toBeCloseTo(1);
    expect(result?.recentMeanPnl).toBeCloseTo(50);
  });
});

describe("computeStructuralBreaks", () => {
  function input(overrides: Partial<StructuralBreakInput>): StructuralBreakInput {
    return {
      walletAddress: "0xabc",
      category: "crypto",
      resolvedAt: new Date("2026-01-01T00:00:00Z"),
      pnlUsd: 1,
      ...overrides,
    };
  }

  it("sorts chronologically before windowing, regardless of input order", () => {
    const early = Array.from({ length: 3 }, (_, i) =>
      input({ resolvedAt: new Date(`2026-01-0${i + 1}T00:00:00Z`), pnlUsd: 1 })
    );
    const recent = Array.from({ length: 3 }, (_, i) =>
      input({ resolvedAt: new Date(`2026-02-0${i + 1}T00:00:00Z`), pnlUsd: 50 })
    );
    // Deliberately shuffled/out-of-order input.
    const shuffled = [recent[1], early[2], recent[0], early[0], recent[2], early[1]];
    const results = computeStructuralBreaks(shuffled, "wallet", 3);
    expect(results).toHaveLength(1);
    expect(results[0].earlyMeanPnl).toBeCloseTo(1);
    expect(results[0].recentMeanPnl).toBeCloseTo(50);
  });

  it("pools across categories for 'wallet' grouping, including rows with category=null", () => {
    const inputs = [
      ...Array.from({ length: 3 }, () => input({ category: "crypto", pnlUsd: 1 })),
      ...Array.from({ length: 3 }, () => input({ category: null, pnlUsd: 1 })),
    ];
    const results = computeStructuralBreaks(inputs, "wallet", 3);
    expect(results).toHaveLength(1); // 6 total pooled, clears a window*2=6 requirement
  });

  it("excludes category=null rows entirely from 'wallet_category' grouping", () => {
    const inputs = [
      ...Array.from({ length: 3 }, () => input({ category: "crypto", pnlUsd: 1 })),
      ...Array.from({ length: 3 }, () => input({ category: null, pnlUsd: 1 })),
    ];
    const results = computeStructuralBreaks(inputs, "wallet_category", 3);
    expect(results).toEqual([]); // only 3 real category rows, needs 6
  });

  it("keeps different wallets and categories independent of each other", () => {
    const walletA = Array.from({ length: 6 }, (_, i) =>
      input({ walletAddress: "0xaaa", category: "crypto", pnlUsd: i < 3 ? 1 : 50 })
    );
    const walletB = Array.from({ length: 6 }, (_, i) =>
      input({ walletAddress: "0xbbb", category: "sports", pnlUsd: 5 })
    );
    const results = computeStructuralBreaks([...walletA, ...walletB], "wallet_category", 3);
    expect(results.map((r) => r.key).sort()).toEqual(["0xaaa|crypto", "0xbbb|sports"]);
  });
});
