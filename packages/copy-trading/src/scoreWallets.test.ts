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
  checkLiquidityFarmingGate,
  checkRecencyGate,
  checkToxicFlowGate,
  clamp,
  computeCapitalMultiplier,
  computeCompositeScore,
  computeCopyabilityScore,
  computeDemotedAddresses,
  computeLiquidityFarmingSignal,
  computeNextRescoreDueAt,
  computePositionConfidence,
  computeRoiScore,
  computeSampleConfidence,
  computeWinRateScore,
  decideStatus,
  deriveTier,
  mean,
  sampleStdev,
  shouldRedirectToApprovalQueue,
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
    liquidityFarmingSignal: null,
    capitalMultiplier: 1,
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
// computePositionConfidence — the PositionTracker confidence discount,
// motivated by the yield-farmer-1 reconciliation finding (2026-07-27):
// realized-only PnL can differ enormously from bullpen's mark-to-market
// figure when a lot of recent activity is still open/unresolved.
// =============================================================================

describe("computePositionConfidence", () => {
  it("returns null (not 0) when there's no position data at all", () => {
    expect(computePositionConfidence(0, 0, rules)).toBeNull();
  });

  it("scores 0 for activity with zero closes yet — a confirmed 'no track record yet,' not missing data", () => {
    expect(computePositionConfidence(0, 10, rules)).toBe(0);
  });

  it("reproduces the yield-farmer-1 shape: mostly-open activity scores a low confidence", () => {
    // Real numbers from the live reconciliation: this wallet had far more
    // open positions than closed ones within the window.
    const confidence = computePositionConfidence(212, 226, rules)!;
    expect(confidence).toBeLessThan(0.5);
  });

  it("does not fully trust a 100%-closed wallet with too few closes to be a real sample", () => {
    const confidence = computePositionConfidence(2, 0, rules)!;
    // completeness is 1.0, but closedSampleConfidence is tiny (2 / floor) —
    // the combined score must still be low, not 1.0.
    expect(confidence).toBeLessThan(0.2);
  });

  it("scores close to 1.0 for a wallet with plenty of closes and few/no opens", () => {
    const confidence = computePositionConfidence(rules.closedPositionConfidenceFloor * 2, 1, rules)!;
    expect(confidence).toBeGreaterThan(0.9);
  });

  it("caps closedSampleConfidence at the floor, same ramp shape as computeSampleConfidence", () => {
    const atFloor = computePositionConfidence(rules.closedPositionConfidenceFloor, 0, rules)!;
    const wayAboveFloor = computePositionConfidence(rules.closedPositionConfidenceFloor * 100, 0, rules)!;
    expect(atFloor).toBeCloseTo(1, 6);
    expect(wayAboveFloor).toBeCloseTo(1, 6); // never exceeds 1.0
  });
});

// =============================================================================
// computeCapitalMultiplier — Half-Kelly sizing RANGE multiplier (v7),
// confirmed with Joey (2026-07-28): saturation=0.35, max=2.0.
// =============================================================================

describe("computeCapitalMultiplier", () => {
  it("is exactly 1.0 (no adjustment) at sharpeProxy=0, not below", () => {
    expect(computeCapitalMultiplier(0, rules)).toBe(1);
  });

  it("clamps a negative (net-losing) sharpeProxy to exactly 1.0, never shrinking the base range", () => {
    expect(computeCapitalMultiplier(-5, rules)).toBe(1);
  });

  it("caps at the configured max at/above saturation", () => {
    expect(computeCapitalMultiplier(rules.capitalMultiplier.saturation, rules)).toBeCloseTo(rules.capitalMultiplier.max, 6);
    expect(computeCapitalMultiplier(rules.capitalMultiplier.saturation * 10, rules)).toBeCloseTo(rules.capitalMultiplier.max, 6);
  });

  it("scales linearly between 1.0 and max below saturation", () => {
    const halfway = computeCapitalMultiplier(rules.capitalMultiplier.saturation / 2, rules);
    expect(halfway).toBeCloseTo(1 + 0.5 * (rules.capitalMultiplier.max - 1), 6);
  });

  it("does NOT reuse the track/watch consistencyScore cutoff (0.15) as its saturation point", () => {
    // A wallet that JUST clears the track-threshold sharpeSaturation
    // (0.15) must NOT already be at the max capital multiplier — that
    // would defeat the whole point of differentiating among top
    // performers, which is exactly the bug this test guards against.
    const atTrackThreshold = computeCapitalMultiplier(rules.sharpeSaturation, rules);
    expect(atTrackThreshold).toBeLessThan(rules.capitalMultiplier.max);
  });
});

// =============================================================================
// deriveTier / computeNextRescoreDueAt — the tiered-scoring self-throttle
// (2026-07-28). Tier 1 is deliberately sourced from the LIVE tracked-wallet
// set, not wallet_profile.status — see deriveTier's own doc comment for
// why those two are known to drift apart.
// =============================================================================

describe("deriveTier", () => {
  it("is tier1 for any address in the live tracked set, regardless of status", () => {
    const tracked = new Set(["0xabc"]);
    expect(deriveTier("0xABC", "ignore", tracked)).toBe("tier1"); // casing-insensitive, and status doesn't matter
  });

  it("is tier2 for status='watch' when not live-tracked", () => {
    expect(deriveTier("0xnottracked", "watch", new Set())).toBe("tier2");
  });

  it("is tier3 for everything else", () => {
    expect(deriveTier("0xnottracked", "ignore", new Set())).toBe("tier3");
    expect(deriveTier("0xnottracked", "track", new Set())).toBe("tier3"); // 'track' status alone isn't tier1 — live-tracked set is
  });
});

describe("computeNextRescoreDueAt", () => {
  const now = new Date("2026-07-28T00:00:00Z");

  it("uses the tier1 cadence for tier1", () => {
    const due = computeNextRescoreDueAt("tier1", rules, now);
    expect(due.getTime() - now.getTime()).toBe(rules.tierRescoreIntervalDays.tier1 * 86400 * 1000);
  });

  it("uses the tier3 (longer) cadence for tier3", () => {
    const due = computeNextRescoreDueAt("tier3", rules, now);
    expect(due.getTime() - now.getTime()).toBe(rules.tierRescoreIntervalDays.tier3 * 86400 * 1000);
  });

  it("tier3's cadence is longer than tier1's — the entire point of tiering", () => {
    const tier1Due = computeNextRescoreDueAt("tier1", rules, now);
    const tier3Due = computeNextRescoreDueAt("tier3", rules, now);
    expect(tier3Due.getTime()).toBeGreaterThan(tier1Due.getTime());
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
// shouldRedirectToApprovalQueue — Telegram approval workflow (2026-08-01)
// =============================================================================

describe("shouldRedirectToApprovalQueue", () => {
  it("redirects a wallet newly crossing the track threshold (prior status 'watch')", () => {
    expect(shouldRedirectToApprovalQueue("track", "watch")).toBe(true);
  });

  it("redirects a wallet that's never been scored before (prior status null)", () => {
    expect(shouldRedirectToApprovalQueue("track", null)).toBe(true);
  });

  it("does NOT redirect a wallet that was already 'track' — reconfirms directly", () => {
    expect(shouldRedirectToApprovalQueue("track", "track")).toBe(false);
  });

  it("does not redirect a 'watch' or 'ignore' decision regardless of prior status", () => {
    expect(shouldRedirectToApprovalQueue("watch", null)).toBe(false);
    expect(shouldRedirectToApprovalQueue("ignore", "watch")).toBe(false);
  });

  it("DOES redirect a bench->track promotion signal — a bench wallet clearing the global track bar " +
    "still needs approval, it isn't auto-promoted just because it's already on the paper bench", () => {
    expect(shouldRedirectToApprovalQueue("track", "bench")).toBe(true);
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
// computeLiquidityFarmingSignal
// =============================================================================

describe("computeLiquidityFarmingSignal", () => {
  it("returns null for an empty sample (no trade history / fetch failed)", () => {
    expect(computeLiquidityFarmingSignal([])).toBeNull();
  });

  it("reproduces the live gloriafoster-shaped signature: same quote repeated many times at an extreme price, all SELL", () => {
    const trades = Array.from({ length: 30 }, () => ({ type: "TRADE", price: 0.007, side: "SELL", slug: "market-a" }));
    const signal = computeLiquidityFarmingSignal(trades);
    expect(signal).not.toBeNull();
    expect(signal?.sampleSize).toBe(30);
    expect(signal?.extremePricePct).toBe(1);
    expect(signal?.topRepeatedQuoteCount).toBe(30);
    expect(signal?.extremePriceSellPct).toBe(1);
  });

  it("reproduces the live quant-generalist-2 false-positive shape: extreme + repeated, but BUY not SELL", () => {
    // The real numbers that slipped through v4: 47 trades, 41 at an
    // extreme price, 7x the same (market, price) pair, but the extreme
    // trades were 39 BUY / 2 SELL (~4.9% sell) -- a longshot buyer, not a
    // farmer.
    const extremeBuys = Array.from({ length: 39 }, () => ({ type: "TRADE", price: 0.02, side: "BUY", slug: "market-x" }));
    const extremeSells = Array.from({ length: 2 }, () => ({ type: "TRADE", price: 0.02, side: "SELL", slug: "market-x" }));
    const nonExtreme = Array.from({ length: 6 }, () => ({ type: "TRADE", price: 0.5, side: "BUY", slug: "market-y" }));
    const signal = computeLiquidityFarmingSignal([...extremeBuys, ...extremeSells, ...nonExtreme]);
    expect(signal?.sampleSize).toBe(47);
    expect(signal?.topRepeatedQuoteCount).toBe(41); // same (market-x, 0.02) pair across buys+sells
    expect(signal?.extremePriceSellPct).toBeCloseTo(2 / 41, 5);
  });

  it("does not flag a diversified sample of different longshots in different markets", () => {
    const trades = Array.from({ length: 30 }, (_, i) => ({ type: "TRADE", price: 0.02 + i * 0.001, side: "BUY", slug: `market-${i}` }));
    const signal = computeLiquidityFarmingSignal(trades);
    expect(signal?.topRepeatedQuoteCount).toBe(1);
  });

  it("ignores unpriced trades when computing extremePricePct but still counts them in sampleSize", () => {
    const trades = [
      { type: "TRADE", price: 0.5, side: "BUY", slug: "m" },
      { type: "TRADE", side: "BUY", slug: "m" }, // no price field
    ];
    const signal = computeLiquidityFarmingSignal(trades);
    expect(signal?.sampleSize).toBe(2);
    expect(signal?.extremePricePct).toBe(0);
  });

  it("returns a null extremePriceSellPct when there are no extreme-price trades to split by side", () => {
    const trades = Array.from({ length: 10 }, () => ({ type: "TRADE", price: 0.5, side: "BUY", slug: "m" }));
    const signal = computeLiquidityFarmingSignal(trades);
    expect(signal?.extremePriceSellPct).toBeNull();
  });
});

// =============================================================================
// checkLiquidityFarmingGate (v5) — the pre-filter added in response to the
// "being a bot isn't inherently the problem, unreplicable edge is"
// correction, then given a sell-side-majority requirement after v4 caught
// a confirmed false positive (quant-generalist-2, a longshot buyer, not a
// farmer). Mirrors checkToxicFlowGate's test shape.
// =============================================================================

describe("checkLiquidityFarmingGate", () => {
  it("gates a wallet matching the confirmed live pattern, regardless of compositeScore", () => {
    const r = basePass2Result({
      compositeScore: 0.9,
      liquidityFarmingSignal: { sampleSize: 30, extremePricePct: 1, topRepeatedQuoteCount: 30, extremePriceSellPct: 1 },
    });
    const gate = checkLiquidityFarmingGate(r, rules);
    expect(gate).not.toBeNull();
    expect(gate?.status).toBe("ignore");
    expect(gate?.reason).toContain("liquidity farming");
    expect(gate?.reason).toContain("SELL-side");
    expect(gate?.reason).toContain("ignored regardless of score");
  });

  it("does NOT gate the real quant-generalist-2 false-positive shape (buy-heavy, ~4.9% sell)", () => {
    const r = basePass2Result({
      liquidityFarmingSignal: {
        sampleSize: 47,
        extremePricePct: 41 / 47,
        topRepeatedQuoteCount: 41,
        extremePriceSellPct: 2 / 41,
      },
    });
    expect(checkLiquidityFarmingGate(r, rules)).toBeNull();
  });

  it("does NOT gate when liquidityFarmingSignal is null (fetch failed / no history) — fails open", () => {
    const r = basePass2Result({ liquidityFarmingSignal: null });
    expect(checkLiquidityFarmingGate(r, rules)).toBeNull();
  });

  it("does NOT gate a sample below minSampleSize even if every other condition is met", () => {
    const r = basePass2Result({
      liquidityFarmingSignal: {
        sampleSize: rules.liquidityFarming.minSampleSize - 1,
        extremePricePct: 1,
        topRepeatedQuoteCount: 999,
        extremePriceSellPct: 1,
      },
    });
    expect(checkLiquidityFarmingGate(r, rules)).toBeNull();
  });

  it("does NOT gate on extreme price + sell-side alone (repeated-quote count below threshold)", () => {
    const r = basePass2Result({
      liquidityFarmingSignal: {
        sampleSize: rules.liquidityFarming.minSampleSize,
        extremePricePct: 1,
        topRepeatedQuoteCount: rules.liquidityFarming.minRepeatedQuoteCount - 1,
        extremePriceSellPct: 1,
      },
    });
    expect(checkLiquidityFarmingGate(r, rules)).toBeNull();
  });

  it("does NOT gate on repeated-quote count + sell-side alone (extreme price below threshold)", () => {
    const r = basePass2Result({
      liquidityFarmingSignal: {
        sampleSize: rules.liquidityFarming.minSampleSize,
        extremePricePct: rules.liquidityFarming.maxExtremePricePct - 0.01,
        topRepeatedQuoteCount: 999,
        extremePriceSellPct: 1,
      },
    });
    expect(checkLiquidityFarmingGate(r, rules)).toBeNull();
  });

  it("does NOT gate on extreme price + repeated-quote alone (sell-side pct below threshold)", () => {
    const r = basePass2Result({
      liquidityFarmingSignal: {
        sampleSize: rules.liquidityFarming.minSampleSize,
        extremePricePct: 1,
        topRepeatedQuoteCount: 999,
        extremePriceSellPct: rules.liquidityFarming.minExtremePriceSellPct - 0.01,
      },
    });
    expect(checkLiquidityFarmingGate(r, rules)).toBeNull();
  });

  it("does NOT gate when extremePriceSellPct is null, even if the other two conditions are met", () => {
    const r = basePass2Result({
      liquidityFarmingSignal: {
        sampleSize: rules.liquidityFarming.minSampleSize,
        extremePricePct: 1,
        topRepeatedQuoteCount: 999,
        extremePriceSellPct: null,
      },
    });
    expect(checkLiquidityFarmingGate(r, rules)).toBeNull();
  });

  it("gates exactly at all three thresholds (>=, not strictly >)", () => {
    const r = basePass2Result({
      liquidityFarmingSignal: {
        sampleSize: rules.liquidityFarming.minSampleSize,
        extremePricePct: rules.liquidityFarming.maxExtremePricePct,
        topRepeatedQuoteCount: rules.liquidityFarming.minRepeatedQuoteCount,
        extremePriceSellPct: rules.liquidityFarming.minExtremePriceSellPct,
      },
    });
    expect(checkLiquidityFarmingGate(r, rules)?.status).toBe("ignore");
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
