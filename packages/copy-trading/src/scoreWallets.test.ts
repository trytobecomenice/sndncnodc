// Unit tests for scoreWallets.ts's pure scoring math and hard gates.
//
// WHY THIS FILE EXISTS: three scoring rewrites shipped in quick succession
// (rolling-window trim, win rate, the toxic-flow consistency gate) and were
// validated only by eyeballing live production runs against a flaky bullpen
// backend — real financial-decision logic (this feeds actual copy-trading
// decisions once TRACKED_TRADERS_SOURCE flips to "db") that had zero
// synthetic-data regression coverage. These tests exist to lock in the
// exact behavior that motivated each change, using constructed data where
// the "correct" answer is known up front, independent of whatever bullpen
// happens to return on a given day.
//
// scoreWallets.ts guards its main() entry point behind an
// import.meta.url === file://argv[1] check specifically so this file can
// import its pure functions without triggering a real DB-writing
// production scan — see that guard's comment in scoreWallets.ts.

import { describe, expect, it } from "vitest";
import {
  DEFAULT_RULES,
  analyzePnlSeries,
  checkRecencyGate,
  checkToxicFlowGate,
  clamp,
  computeCompositeScore,
  computeCopyabilityScore,
  computeDemotedAddresses,
  computeRoiScore,
  computeSampleConfidence,
  computeWinRateScore,
  decideStatus,
  mean,
  sampleStdev,
  trimToRollingWindow,
  type BehaviorStatsData,
  type Pass2Result,
  type ScoringRules,
  type TradeFlowData,
  type WalletStatsData,
} from "./scoreWallets";

const DAY = 24 * 60 * 60;
const HOUR = 60 * 60;

function nowSec(): number {
  return Math.floor(Date.now() / 1000);
}

// =============================================================================
// Synthetic pnl-series builders
// =============================================================================

/**
 * `count` points evenly spaced in time between `startDaysAgo` and
 * `endDaysAgo` (descending -> oldest first), with each point's value given
 * by `valueAt(fraction)` where fraction runs 0 (oldest) -> 1 (newest).
 */
function makeSeries(
  count: number,
  startDaysAgo: number,
  endDaysAgo: number,
  valueAt: (fraction: number) => number
): Array<{ p: number; t: number }> {
  const now = nowSec();
  const points: Array<{ p: number; t: number }> = [];
  for (let i = 0; i < count; i++) {
    const fraction = count === 1 ? 0 : i / (count - 1);
    const daysAgo = startDaysAgo - (startDaysAgo - endDaysAgo) * fraction;
    points.push({ p: valueAt(fraction), t: now - Math.round(daysAgo * DAY) });
  }
  return points;
}

/** Constructs a "dormant for months, then hot for the rolling window" pair:
 * `full` is the whole history (what a real pnl-series call would return),
 * `recentOnly` is exactly the tail slice inside a 90-day window — used to
 * assert analyzePnlSeries(full) behaves as if the dormant prefix never
 * existed. */
function dormantThenHotSeries(rollingWindowDays: number) {
  const dormant = makeSeries(400, 400, rollingWindowDays + 0.5, () => 1000); // flat, entirely OUTSIDE the window
  const recentOnly = makeSeries(200, rollingWindowDays, 0, (f) => 1000 + f * 1000); // steady 1000 -> 2000 growth, INSIDE the window
  return { full: [...dormant, ...recentOnly], recentOnly };
}

/** High win rate (90%) but low consistency: many small +1 "wins" interrupted
 * by occasional large -50 losses. Mirrors the real pizzaallosqualo /
 * gloriafoster / pizzabillgates pattern this project found (60-84% win
 * rate, ~0 consistency) — volatile mark-to-market swings drag the mean
 * delta negative even though most individual periods are nominal "wins". */
function toxicFlowSignatureSeries(): Array<{ p: number; t: number }> {
  const now = nowSec();
  let value = 10000;
  const points: Array<{ p: number; t: number }> = [{ p: value, t: now - 101 * HOUR }];
  for (let i = 0; i < 100; i++) {
    const isBigLoss = i % 10 === 9; // 10 losses, 90 wins
    value += isBigLoss ? -50 : 1;
    points.push({ p: value, t: now - (100 - i) * HOUR });
  }
  return points;
}

// =============================================================================
// Fixture factories
// =============================================================================

function baseWalletStats(overrides: Partial<WalletStatsData> = {}): WalletStatsData {
  return {
    wallet_address: "0xtest",
    lifetime_pnl: 1000,
    lifetime_volume: 10000,
    pnl_24h: 0,
    pnl_30d: 500,
    pnl_7d: 100,
    rank: 1,
    roi_30d: 0.5,
    total_position_value: 1000,
    trades_count: 100,
    capital_deployed_30d: 1000,
    join_date: new Date(Date.now() - 400 * DAY * 1000).toISOString(),
    ...overrides,
  };
}

function baseTradeFlow(overrides: Partial<TradeFlowData> = {}): TradeFlowData {
  return {
    wallet_address: "0xtest",
    volume_24h: 0,
    volume_30d: 10000,
    volume_7d: 0,
    net_flow_24h: 0,
    net_flow_30d: 0,
    net_flow_7d: 0,
    ...overrides,
  };
}

function basePass2Result(overrides: Partial<Pass2Result> = {}): Pass2Result {
  return {
    address: "0xtest",
    displayName: null,
    walletStats: baseWalletStats(),
    tradeFlow: baseTradeFlow(),
    roiScore: 1,
    consistencyScore: 1,
    winRateScore: 1,
    recentWinRate: 0.9,
    copyabilityScore: 1,
    oneHitWonderPenalty: 0,
    compositeScore: 0.9,
    daysSinceLastTrade: 1,
    insufficientConsistencyData: false,
    scoreBreakdown: {},
    ...overrides,
  };
}

const rules: ScoringRules = DEFAULT_RULES;

// =============================================================================
// Math helpers
// =============================================================================

describe("clamp", () => {
  it("clamps below min", () => expect(clamp(-5, 0, 1)).toBe(0));
  it("clamps above max", () => expect(clamp(5, 0, 1)).toBe(1));
  it("passes through in-range values", () => expect(clamp(0.5, 0, 1)).toBe(0.5));
});

describe("mean / sampleStdev", () => {
  it("mean of empty array is 0", () => expect(mean([])).toBe(0));
  it("mean of a simple array", () => expect(mean([1, 2, 3])).toBe(2));
  it("sampleStdev needs at least 2 values", () => expect(sampleStdev([5])).toBe(0));
  it("sampleStdev of identical values is 0", () => expect(sampleStdev([3, 3, 3])).toBe(0));
});

// =============================================================================
// trimToRollingWindow — the core mechanism behind the whole rolling-window fix
// =============================================================================

describe("trimToRollingWindow", () => {
  it("excludes points older than the window and keeps points inside it", () => {
    const now = nowSec();
    const series = [
      { p: 1, t: now - 200 * DAY }, // outside a 90d window
      { p: 2, t: now - 91 * DAY }, // outside
      { p: 3, t: now - 30 * DAY }, // inside
      { p: 4, t: now - 1 * DAY }, // inside
    ];
    const windowed = trimToRollingWindow(series, 90);
    expect(windowed.map((pt) => pt.p)).toEqual([3, 4]);
  });

  it("includes a point just inside the cutoff boundary", () => {
    // A few seconds of margin rather than the exact theoretical boundary
    // instant: trimToRollingWindow computes its own Date.now() slightly
    // later than nowSec() is captured here, so testing the literal instant
    // is a real-clock race, not a meaningful behavior to lock in.
    const now = nowSec();
    const series = [{ p: 1, t: now - 90 * DAY + 5 }];
    expect(trimToRollingWindow(series, 90)).toHaveLength(1);
  });

  it("sorts the result oldest-first even if the input is unordered", () => {
    const now = nowSec();
    const series = [
      { p: 2, t: now - 1 * DAY },
      { p: 1, t: now - 5 * DAY },
    ];
    expect(trimToRollingWindow(series, 90).map((pt) => pt.p)).toEqual([1, 2]);
  });
});

// =============================================================================
// computeRoiScore
// =============================================================================

describe("computeRoiScore", () => {
  it("saturates to 1.0 at/above roiSaturation", () => {
    expect(computeRoiScore(rules.roiSaturation, rules)).toBe(1);
    expect(computeRoiScore(rules.roiSaturation * 3, rules)).toBe(1);
  });
  it("floors negative ROI to 0", () => {
    expect(computeRoiScore(-0.5, rules)).toBe(0);
  });
  it("scales linearly between 0 and saturation", () => {
    expect(computeRoiScore(rules.roiSaturation / 2, rules)).toBeCloseTo(0.5, 6);
  });
});

// =============================================================================
// computeSampleConfidence
// =============================================================================

describe("computeSampleConfidence", () => {
  it("is 0 at 0 trades", () => expect(computeSampleConfidence(0, rules)).toBe(0));
  it("caps at 1.0 at/above the trades floor", () => {
    expect(computeSampleConfidence(rules.sampleConfidenceTradesFloor, rules)).toBe(1);
    expect(computeSampleConfidence(rules.sampleConfidenceTradesFloor * 10, rules)).toBe(1);
  });
  it("scales linearly below the floor", () => {
    expect(computeSampleConfidence(rules.sampleConfidenceTradesFloor / 2, rules)).toBeCloseTo(0.5, 6);
  });
});

// =============================================================================
// analyzePnlSeries — THE regression test for the rolling-window rewrite
// =============================================================================

describe("analyzePnlSeries", () => {
  it("scores a dormant-then-hot wallet identically to a wallet with only the recent history", () => {
    const { full, recentOnly } = dormantThenHotSeries(rules.rollingWindowDays);

    const fullResult = analyzePnlSeries(full, rules);
    const recentOnlyResult = analyzePnlSeries(recentOnly, rules);

    expect(fullResult.consistencyScore).toBeCloseTo(recentOnlyResult.consistencyScore, 6);
    expect(fullResult.oneHitWonderPenalty).toBeCloseTo(recentOnlyResult.oneHitWonderPenalty, 6);
    expect(fullResult.recentWinRate).toBeCloseTo(recentOnlyResult.recentWinRate, 6);
    expect(fullResult.insufficientData).toBe(false);
  });

  it("scores meaningfully better than the OLD lifetime-history (unwindowed) approach would have", () => {
    // This is the concrete regression this rewrite fixes: without
    // windowing, months of flat dormant deltas dilute the mean without a
    // compensating drop in volatility, dragging the Sharpe-proxy down.
    //
    // A perfectly smooth growth ramp (no noise) has near-zero delta
    // variance regardless of dilution, so its Sharpe-proxy saturates to the
    // 1.0 ceiling either way — that would mask the effect this test exists
    // to prove. A realistic, noisy-but-net-positive repeating delta pattern
    // avoids that: the active window alone lands at a moderate, UNSATURATED
    // score, leaving room to show dilution genuinely drags it down further.
    const now = nowSec();
    const pattern = [13, -11, 13, -11]; // net +1/step, real variance
    const activeHours = 200;

    const activePoints: Array<{ p: number; t: number }> = [];
    let value = 1000;
    for (let h = activeHours; h >= 0; h--) {
      activePoints.push({ p: value, t: now - h * HOUR });
      if (h > 0) value += pattern[(activeHours - h) % pattern.length];
    }

    // Flat dormant prefix, comfortably outside the window (ends a further
    // ~8 days before the window boundary, well clear of any edge cases).
    const dormantHours = 400 * 24;
    const dormantEndT = now - rules.rollingWindowDays * DAY - activeHours * HOUR;
    const dormantPoints: Array<{ p: number; t: number }> = [];
    for (let h = dormantHours; h >= 0; h--) {
      dormantPoints.push({ p: 1000, t: dormantEndT - h * HOUR });
    }

    const full = [...dormantPoints, ...activePoints];

    const windowedResult = analyzePnlSeries(full, rules);
    const activeOnlyResult = analyzePnlSeries(activePoints, rules);
    // Windowing the full series should reproduce scoring the active window
    // alone — same mechanism the equivalence test above already proves.
    expect(windowedResult.consistencyScore).toBeCloseTo(activeOnlyResult.consistencyScore, 6);
    expect(activeOnlyResult.consistencyScore).toBeLessThan(1); // not saturated, or this comparison is meaningless

    const naiveDeltas: number[] = [];
    for (let i = 1; i < full.length; i++) naiveDeltas.push(full[i].p - full[i - 1].p);
    const naiveSharpe = sampleStdev(naiveDeltas) > 0 ? mean(naiveDeltas) / sampleStdev(naiveDeltas) : 0;
    const naiveConsistencyScore = clamp(naiveSharpe / rules.sharpeSaturation, 0, 1);

    expect(windowedResult.consistencyScore).toBeGreaterThan(naiveConsistencyScore);
  });

  it("flags insufficientData when fewer than 3 points fall inside the window, without accusing it of anything", () => {
    const now = nowSec();
    const series = [{ p: 1, t: now - 1 * DAY }]; // only 1 point in-window
    const result = analyzePnlSeries(series, rules);
    expect(result.insufficientData).toBe(true);
    expect(result.consistencyScore).toBe(0);
    expect(result.recentWinRate).toBe(0.5); // coin-flip neutral, not 0 ("confirmed bad")
  });

  it("assigns a high one-hit-wonder penalty when one spike dominates recent gains", () => {
    const now = nowSec();
    const points = [{ p: 0, t: now - 10 * HOUR }];
    let value = 0;
    for (let i = 0; i < 9; i++) {
      value += 1; // nine tiny +1 gains
      points.push({ p: value, t: now - (9 - i) * HOUR });
    }
    value += 1000; // one dominant spike
    points.push({ p: value, t: now });
    const result = analyzePnlSeries(points, rules);
    expect(result.oneHitWonderPenalty).toBeGreaterThan(0.95);
  });

  it("reproduces the real toxic-flow signature: high win rate, near-zero consistency", () => {
    const result = analyzePnlSeries(toxicFlowSignatureSeries(), rules);
    expect(result.recentWinRate).toBeCloseTo(0.9, 2);
    expect(result.consistencyScore).toBe(0); // negative Sharpe-proxy clamps to 0
  });
});

// =============================================================================
// computeWinRateScore
// =============================================================================

describe("computeWinRateScore", () => {
  it("scores a coin-flip win rate (0.5) as exactly 0 — no credit for merely not losing", () => {
    expect(computeWinRateScore(0.5, rules)).toBe(0);
  });
  it("clamps a below-coin-flip win rate to 0, not negative", () => {
    expect(computeWinRateScore(0.2, rules)).toBe(0);
  });
  it("saturates to 1.0 at/above winRateSaturation", () => {
    expect(computeWinRateScore(rules.winRateSaturation, rules)).toBe(1);
    expect(computeWinRateScore(0.99, rules)).toBe(1);
  });
});

// =============================================================================
// computeCopyabilityScore
// =============================================================================

describe("computeCopyabilityScore", () => {
  const behaviorStats: BehaviorStatsData = {
    wallet_address: "0xtest",
    avg_trades_per_day_30d: 5, // inside [minGood, maxGood] sweet spot
    avg_trades_per_day_7d: 5,
    avg_trades_per_day_1d: 5,
    avg_trades_per_day_lifetime: 0.05, // deliberately tiny lifetime rate
    win_rate_7d: 0.6,
    win_rate_1d: 0.6,
    total_trades: 500,
    is_likely_bot: false,
    trader_tier: "medium",
  };

  it("scores near 1.0 when recent trade pace is in the sweet spot and volume is saturated", () => {
    const score = computeCopyabilityScore(behaviorStats, 500, "2020-01-01T00:00:00Z", rules.copyability.volumePresenceSaturation, rules);
    expect(score).toBeCloseTo(1, 6);
  });

  it("scores 0 frequency fit when the recent pace is at/below the floor", () => {
    const belowFloor: BehaviorStatsData = { ...behaviorStats, avg_trades_per_day_30d: rules.copyability.floor };
    const score = computeCopyabilityScore(belowFloor, 500, "2020-01-01T00:00:00Z", 0, rules);
    expect(score).toBe(0);
  });

  it("scores 0 frequency fit when the recent pace is at/above the ceiling (too frantic)", () => {
    const aboveCeiling: BehaviorStatsData = { ...behaviorStats, avg_trades_per_day_30d: rules.copyability.ceiling };
    const score = computeCopyabilityScore(aboveCeiling, 500, "2020-01-01T00:00:00Z", 0, rules);
    expect(score).toBe(0);
  });

  it("fixes the dormant-then-hot underrating: using the 30d rate scores far better than the old lifetime-average fallback would for the same wallet", () => {
    // A wallet that joined 2 years ago, dormant almost the whole time, but
    // trading 5/day for the last 90 days: lifetime average is diluted to
    // near-zero, but the real recent pace (behaviorStats) is a perfect fit.
    const twoYearsAgoIso = new Date(Date.now() - 730 * DAY * 1000).toISOString();
    const lifetimeTrades = 5 * 90; // all the activity happened recently

    const withBehaviorStats = computeCopyabilityScore(behaviorStats, lifetimeTrades, twoYearsAgoIso, 0, rules);
    const fallbackOnly = computeCopyabilityScore(null, lifetimeTrades, twoYearsAgoIso, 0, rules);

    expect(withBehaviorStats).toBeGreaterThan(fallbackOnly);
    expect(fallbackOnly).toBeLessThan(0.2); // the old bug: lifetime rate lands near the "too rare" floor
  });
});

// =============================================================================
// computeCompositeScore
// =============================================================================

describe("computeCompositeScore", () => {
  it("equals the sum of weights when every sub-score is perfect and there's no penalty", () => {
    const score = computeCompositeScore(1, 1, 1, 1, 0, 1, rules);
    const weightSum = rules.weights.roi + rules.weights.consistency + rules.weights.winRate + rules.weights.copyability;
    expect(score).toBeCloseTo(weightSum, 6);
  });

  it("applies the one-hit-wonder penalty as a percentage reduction, never past oneHitWonderPenaltyStrength", () => {
    const withMaxConcentration = computeCompositeScore(1, 1, 1, 1, 1, 1, rules);
    const weightSum = rules.weights.roi + rules.weights.consistency + rules.weights.winRate + rules.weights.copyability;
    expect(withMaxConcentration).toBeCloseTo(weightSum * (1 - rules.oneHitWonderPenaltyStrength), 6);
  });

  it("zeroes out the score when sampleConfidence is 0, regardless of how good the sub-scores are", () => {
    expect(computeCompositeScore(1, 1, 1, 1, 0, 0, rules)).toBe(0);
  });
});

// =============================================================================
// decideStatus
// =============================================================================

describe("decideStatus", () => {
  it("forces ignore below hardMinTrades regardless of compositeScore", () => {
    const result = decideStatus(1, rules.hardMinTrades - 1, rules);
    expect(result.status).toBe("ignore");
  });

  it("tracks at/above the track threshold", () => {
    expect(decideStatus(rules.statusThresholds.track, 100, rules).status).toBe("track");
  });

  it("watches between the watch and track thresholds", () => {
    expect(decideStatus(rules.statusThresholds.watch, 100, rules).status).toBe("watch");
  });

  it("ignores below the watch threshold", () => {
    expect(decideStatus(rules.statusThresholds.watch - 0.01, 100, rules).status).toBe("ignore");
  });
});

// =============================================================================
// checkToxicFlowGate — the gate this whole session's most recent change added
// =============================================================================

describe("checkToxicFlowGate", () => {
  it("gates a wallet with low consistency regardless of a maxed-out ROI and win rate", () => {
    const r = basePass2Result({
      consistencyScore: 0.05,
      insufficientConsistencyData: false,
      roiScore: 1,
      winRateScore: 1,
    });
    const gate = checkToxicFlowGate(r, rules);
    expect(gate).not.toBeNull();
    expect(gate?.status).toBe("ignore");
    expect(gate?.reason).toContain("toxic flow - volume farmer");
    expect(gate?.reason).toContain("ignored regardless of either");
  });

  it("does NOT gate a wallet whose low consistency is due to insufficient data", () => {
    const r = basePass2Result({ consistencyScore: 0, insufficientConsistencyData: true });
    expect(checkToxicFlowGate(r, rules)).toBeNull();
  });

  it("does not gate exactly at the minConsistencyScore threshold", () => {
    const r = basePass2Result({ consistencyScore: rules.minConsistencyScore, insufficientConsistencyData: false });
    expect(checkToxicFlowGate(r, rules)).toBeNull();
  });

  it("gates the real toxic-flow signature end-to-end (analyzePnlSeries -> gate)", () => {
    const analysis = analyzePnlSeries(toxicFlowSignatureSeries(), rules);
    const r = basePass2Result({
      consistencyScore: analysis.consistencyScore,
      insufficientConsistencyData: analysis.insufficientData,
      roiScore: 1,
      winRateScore: 1,
    });
    const gate = checkToxicFlowGate(r, rules);
    expect(gate?.status).toBe("ignore");
  });
});

// =============================================================================
// checkRecencyGate
// =============================================================================

describe("checkRecencyGate", () => {
  it("does not gate when daysSinceLastTrade is unknown (null) — fails open, not closed", () => {
    const r = basePass2Result({ daysSinceLastTrade: null });
    expect(checkRecencyGate(r, rules)).toBeNull();
  });

  it("does not gate exactly at the maxDaysSinceLastTrade boundary", () => {
    const r = basePass2Result({ daysSinceLastTrade: rules.maxDaysSinceLastTrade });
    expect(checkRecencyGate(r, rules)).toBeNull();
  });

  it("gates just past the boundary, regardless of compositeScore", () => {
    const r = basePass2Result({ daysSinceLastTrade: rules.maxDaysSinceLastTrade + 1, compositeScore: 0.99 });
    const gate = checkRecencyGate(r, rules);
    expect(gate?.status).toBe("ignore");
    expect(gate?.reason).toContain("ignored regardless of score");
  });
});

// =============================================================================
// computeDemotedAddresses — the top-N pool cap
// =============================================================================

describe("computeDemotedAddresses", () => {
  function trackRow(address: string, compositeScore: number) {
    return { address, status: "track", compositeScore };
  }

  it("demotes nobody when there are fewer track candidates than the pool size", () => {
    const decided = [trackRow("a", 0.9), trackRow("b", 0.8)];
    expect(computeDemotedAddresses(decided, 15).size).toBe(0);
  });

  it("demotes nobody exactly at the pool size boundary", () => {
    const decided = Array.from({ length: 15 }, (_, i) => trackRow(`w${i}`, 1 - i * 0.01));
    expect(computeDemotedAddresses(decided, 15).size).toBe(0);
  });

  it("demotes exactly the lowest-scoring wallets beyond the pool size", () => {
    const decided = Array.from({ length: 20 }, (_, i) => trackRow(`w${i}`, 1 - i * 0.01)); // w0 best .. w19 worst
    const demoted = computeDemotedAddresses(decided, 15);
    expect(demoted.size).toBe(5);
    expect(demoted.has("w19")).toBe(true);
    expect(demoted.has("w15")).toBe(true);
    expect(demoted.has("w14")).toBe(false); // rank 15 (0-indexed 14) is the last one that survives
    expect(demoted.has("w0")).toBe(false);
  });

  it("never demotes a wallet that wasn't independently 'track' status, no matter its score", () => {
    const decided = [
      ...Array.from({ length: 15 }, (_, i) => trackRow(`t${i}`, 0.9 - i * 0.01)),
      { address: "watch-but-high-score", status: "watch", compositeScore: 0.99 },
    ];
    const demoted = computeDemotedAddresses(decided, 15);
    expect(demoted.has("watch-but-high-score")).toBe(false);
  });
});
