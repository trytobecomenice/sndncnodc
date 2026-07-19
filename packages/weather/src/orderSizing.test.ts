import { describe, expect, it } from "vitest";
import { checkEdgeFloor, checkTempBuffer, computeKellyFraction, computePositionSize } from "./orderSizing";

describe("computeKellyFraction", () => {
  it("computes the Yes-side Kelly fraction when our estimate exceeds the market price", () => {
    // pHat=0.60, market=0.40 -> f* = (0.60-0.40)/(1-0.40) = 0.3333...
    const result = computeKellyFraction(0.6, 0.4);
    expect(result.side).toBe("Yes");
    expect(result.fullKellyFraction).toBeCloseTo(0.3333, 3);
  });

  it("computes the No-side Kelly fraction when the market overprices Yes", () => {
    // pHat=0.20, market=0.40 -> f* = (0.40-0.20)/0.40 = 0.5
    const result = computeKellyFraction(0.2, 0.4);
    expect(result.side).toBe("No");
    expect(result.fullKellyFraction).toBeCloseTo(0.5, 5);
  });

  it("returns zero fraction when there is no edge at all", () => {
    const result = computeKellyFraction(0.4, 0.4);
    expect(result.fullKellyFraction).toBe(0);
  });

  it("is defensive against degenerate market prices at the 0/1 boundary", () => {
    expect(computeKellyFraction(0.9, 0).fullKellyFraction).toBe(0);
    expect(computeKellyFraction(0.9, 1).fullKellyFraction).toBe(0);
  });

  it("reproduces the real Seoul smoke-test numbers (highest-temp -26c bucket)", () => {
    // climatology=12.9%, forecast=14.6%, blended=14.0%, market=40.5% -> No side, real edge -26.5pp
    const result = computeKellyFraction(0.14, 0.405);
    expect(result.side).toBe("No");
    // f* = (0.405-0.14)/0.405
    expect(result.fullKellyFraction).toBeCloseTo(0.6543, 3);
  });
});

describe("checkEdgeFloor", () => {
  it("passes when |edge| meets the floor exactly", () => {
    expect(checkEdgeFloor(0.05, 0.05)).toBe(true);
    expect(checkEdgeFloor(-0.05, 0.05)).toBe(true);
  });

  it("fails when |edge| is below the floor", () => {
    expect(checkEdgeFloor(0.049, 0.05)).toBe(false);
    expect(checkEdgeFloor(-0.03, 0.05)).toBe(false);
  });
});

describe("checkTempBuffer", () => {
  it("passes a one-sided 'X or higher' bucket when the forecast clears the buffer", () => {
    // strike >= 80F, forecast mean 82F -> distance 2.0F, buffer 1.5F -> passes
    const result = checkTempBuffer(82, { min: 80, max: null }, 1.5);
    expect(result.passes).toBe(true);
    expect(result.distanceF).toBeCloseTo(2.0, 5);
  });

  it("rejects a one-sided bucket when the forecast sits inside the buffer zone", () => {
    const result = checkTempBuffer(81, { min: 80, max: null }, 1.5);
    expect(result.passes).toBe(false);
    expect(result.distanceF).toBeCloseTo(1.0, 5);
  });

  it("rejects a two-sided bucket when the forecast is close to either edge, even if inside it", () => {
    // bucket [79.5, 81.5], forecast mean 80.5 -> 1.0F from BOTH edges -> fails a 1.5F buffer
    const result = checkTempBuffer(80.5, { min: 79.5, max: 81.5 }, 1.5);
    expect(result.passes).toBe(false);
    expect(result.distanceF).toBeCloseTo(1.0, 5);
  });

  it("passes a two-sided bucket when the forecast clears both edges", () => {
    // bucket [79.5, 84.5], forecast mean 82 -> min(2.5, 2.5) -> passes
    const result = checkTempBuffer(82, { min: 79.5, max: 84.5 }, 1.5);
    expect(result.passes).toBe(true);
    expect(result.distanceF).toBeCloseTo(2.5, 5);
  });
});

describe("computePositionSize", () => {
  it("applies the fractional-Kelly multiplier when it is the binding constraint", () => {
    // full Kelly 0.10, quarter-Kelly -> 0.025, well under the 5% cap
    const result = computePositionSize(0.1, 0.25, 0.05, 10000);
    expect(result.appliedFraction).toBeCloseTo(0.025, 5);
    expect(result.sizeUsd).toBeCloseTo(250, 5);
  });

  it("applies the 5% hard cap when scaled Kelly would exceed it", () => {
    // full Kelly 0.65 (the real Seoul -26c case), quarter-Kelly -> 0.1635, capped at 0.05
    const result = computePositionSize(0.6543, 0.25, 0.05, 10000);
    expect(result.appliedFraction).toBeCloseTo(0.05, 5);
    expect(result.sizeUsd).toBeCloseTo(500, 5);
  });

  it("produces zero size for zero Kelly fraction", () => {
    const result = computePositionSize(0, 0.25, 0.05, 10000);
    expect(result.sizeUsd).toBe(0);
  });
});
