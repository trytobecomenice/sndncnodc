// The Order Builder's rule math (Joey, 2026-07-20) — pure, unit-tested functions implementing
// Rule 7 (residual basis-risk margin), Rule 12 (minimum forecast margin of safety), and Rule 11
// (5% Kelly-inspired position cap) from docs/weather/WEATHER_RISK_MANAGEMENT.md. No DB/network I/O
// here by design, same split this codebase already uses elsewhere (computeHitRate vs.
// calculateProbability.ts, checkOddsFilter vs. discoverMarkets.ts) — orderBuilder.ts is the
// DB-touching orchestrator that calls these.

import type { ThresholdRange } from "./calculateProbability";

export type Side = "Yes" | "No";

export interface KellyResult {
  side: Side;
  /** Full, unscaled Kelly fraction (0..1-ish) — 0 when there's no edge at all (pHat === marketProb)
   * or the inputs are degenerate (marketProb at exactly 0 or 1, which the Rule 10 odds filter
   * should already prevent from reaching here, but guarded defensively regardless). */
  fullKellyFraction: number;
}

/** Standard binary-market Kelly fraction. For buying "Yes" at price `marketProb` when our
 * estimate `pHat` is higher: f* = (pHat - marketProb) / (1 - marketProb) — the well-known
 * simplification of the general Kelly formula f* = (b*p - q)/b for a market where $1 staked buys
 * 1/price shares worth $1 each. Buying "No" is the mirror image using the No side's own price
 * (1 - marketProb) and probability (1 - pHat). */
export function computeKellyFraction(pHat: number, marketProb: number): KellyResult {
  if (marketProb <= 0 || marketProb >= 1) return { side: "Yes", fullKellyFraction: 0 };

  if (pHat > marketProb) {
    const fraction = (pHat - marketProb) / (1 - marketProb);
    return { side: "Yes", fullKellyFraction: Math.max(0, fraction) };
  }
  if (pHat < marketProb) {
    const fraction = (marketProb - pHat) / marketProb;
    return { side: "No", fullKellyFraction: Math.max(0, fraction) };
  }
  return { side: "Yes", fullKellyFraction: 0 };
}

/** Rule 7 — residual basis-risk margin: edge must clear a floor beyond zero, not just be
 * positive. `floorPp` is a probability-point threshold (e.g. 0.05 = 5 percentage points). */
export function checkEdgeFloor(edge: number, floorPp: number): boolean {
  return Math.abs(edge) >= floorPp;
}

export interface TempBufferCheck {
  passes: boolean;
  /** Distance in °F from the forecast point estimate to the NEAREST finite strike boundary. For a
   * one-sided bucket (e.g. "80°F or above") this is exactly Rule 12's original single-boundary
   * check. For a two-sided bucket (e.g. an exact-degree "27C" market, already widened to its
   * rounding-boundary range by parseMarketThreshold.ts) this is a deliberate generalization:
   * requiring clearance from BOTH edges, not just one, since a forecast sitting close to either
   * edge of a bucket is exactly the "small forecast error flips the outcome" jump risk this rule
   * exists to catch — it reduces to the original single-boundary rule when only one bound is set. */
  distanceF: number;
}

/** Rule 12 — minimum forecast margin of safety. Unconditionally rejects a trade if the ensemble's
 * own point-estimate forecast sits within `bufferF` of a strike boundary. */
export function checkTempBuffer(meanForecastF: number, range: ThresholdRange, bufferF: number): TempBufferCheck {
  const distances: number[] = [];
  if (range.min !== null) distances.push(Math.abs(meanForecastF - range.min));
  if (range.max !== null) distances.push(Math.abs(meanForecastF - range.max));
  const distanceF = distances.length > 0 ? Math.min(...distances) : Infinity;
  return { passes: distanceF >= bufferF, distanceF };
}

export interface PositionSize {
  /** The Kelly fraction actually used for sizing, after the fractional-Kelly multiplier and the
   * Rule 11 hard cap — whichever bound bit first. */
  appliedFraction: number;
  sizeUsd: number;
}

/** Rule 11 — position sizing. Scales the full Kelly fraction down by `kellyMultiplier` (quarter-
 * Kelly = 0.25, Joey's call 2026-07-20 — full Kelly over-bets on a probability estimate this
 * codebase's own docs already flag as uncalibrated), then hard-caps at `capFraction` (0.05 = 5% of
 * total capital) regardless of how large the scaled Kelly fraction is. */
export function computePositionSize(
  fullKellyFraction: number,
  kellyMultiplier: number,
  capFraction: number,
  totalCapitalUsd: number
): PositionSize {
  const scaled = fullKellyFraction * kellyMultiplier;
  const appliedFraction = Math.min(scaled, capFraction);
  return { appliedFraction, sizeUsd: appliedFraction * totalCapitalUsd };
}
