import { describe, expect, it } from "vitest";
import { parseMarketThreshold } from "./parseMarketThreshold";

describe("parseMarketThreshold", () => {
  it("parses an exact Celsius bucket with the ±0.5C boundary expansion (real Seoul slug)", () => {
    const r = parseMarketThreshold("highest-temperature-in-seoul-on-july-19-2026-22c");
    expect(r?.metric).toBe("max");
    // 21.5C -> 70.7F, 22.5C -> 72.5F
    expect(r?.targetTempMinF).toBeCloseTo((21.5 * 9) / 5 + 32, 5);
    expect(r?.targetTempMaxF).toBeCloseTo((22.5 * 9) / 5 + 32, 5);
  });

  it("parses a Celsius 'or below' open-ended bucket (real Seoul slug)", () => {
    const r = parseMarketThreshold("highest-temperature-in-seoul-on-july-19-2026-21corbelow");
    expect(r?.targetTempMinF).toBeNull();
    // 21.5C -> 70.7F
    expect(r?.targetTempMaxF).toBeCloseTo((21.5 * 9) / 5 + 32, 5);
  });

  it("parses a Celsius 'or higher' open-ended bucket (real Seoul slug)", () => {
    const r = parseMarketThreshold("highest-temperature-in-seoul-on-july-19-2026-31corhigher");
    // 30.5C -> 86.9F
    expect(r?.targetTempMinF).toBeCloseTo((30.5 * 9) / 5 + 32, 5);
    expect(r?.targetTempMaxF).toBeNull();
  });

  it("parses a Fahrenheit ranged bucket with the ±0.5F boundary expansion (real NYC slug)", () => {
    const r = parseMarketThreshold("highest-temperature-in-nyc-on-july-19-2026-74-75f");
    expect(r?.targetTempMinF).toBeCloseTo(73.5, 5);
    expect(r?.targetTempMaxF).toBeCloseTo(75.5, 5);
  });

  it("parses a Fahrenheit 'or below' open-ended bucket (real NYC slug)", () => {
    const r = parseMarketThreshold("highest-temperature-in-nyc-on-july-19-2026-73forbelow");
    expect(r?.targetTempMinF).toBeNull();
    expect(r?.targetTempMaxF).toBeCloseTo(73.5, 5);
  });

  it("parses a Fahrenheit 'or higher' open-ended bucket (real NYC slug)", () => {
    const r = parseMarketThreshold("highest-temperature-in-nyc-on-july-19-2026-92forhigher");
    expect(r?.targetTempMinF).toBeCloseTo(91.5, 5);
    expect(r?.targetTempMaxF).toBeNull();
  });

  it("recognizes 'lowest-temperature' events as metric=min (real Seoul/NYC slugs)", () => {
    expect(parseMarketThreshold("lowest-temperature-in-seoul-on-july-19-2026-20c")?.metric).toBe("min");
    expect(parseMarketThreshold("lowest-temperature-in-nyc-on-july-19-2026-64-65f")?.metric).toBe("min");
  });

  it("returns null for non-temperature-bucket weather-category markets (real slugs, verified live)", () => {
    expect(parseMarketThreshold("where-will-2026-rank-among-the-hottest-years-on-record")).toBeNull();
    expect(parseMarketThreshold("nyc-air-quality-index-below-100-byptptpt-20260717052808748")).toBeNull();
    expect(parseMarketThreshold("hantavirus-pandemic-in-2026")).toBeNull();
  });

  it("returns null for a completely unrelated slug", () => {
    expect(parseMarketThreshold("some-random-market-slug")).toBeNull();
  });
});
