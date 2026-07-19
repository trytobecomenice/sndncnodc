import { describe, expect, it } from "vitest";
import { checkOddsFilter, MAX_IMPLIED_PROB, MIN_IMPLIED_PROB } from "./oddsFilter";

describe("checkOddsFilter", () => {
  it("rejects a lottery-ticket market (0.0005, real value seen from live data)", () => {
    const result = checkOddsFilter(0.0005);
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/lottery-ticket/);
  });

  it("rejects a near-certain steamroller market (0.99, real value seen from live data)", () => {
    const result = checkOddsFilter(0.99);
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/steamroller/);
  });

  it("accepts a genuinely contested market (0.535, real NYC value seen from live data)", () => {
    const result = checkOddsFilter(0.535);
    expect(result.ok).toBe(true);
  });

  it("accepts exactly at the lower boundary (0.10)", () => {
    expect(checkOddsFilter(0.1).ok).toBe(true);
  });

  it("rejects just below the lower boundary", () => {
    expect(checkOddsFilter(0.0999).ok).toBe(false);
  });

  it("accepts exactly at the upper boundary (0.90)", () => {
    expect(checkOddsFilter(0.9).ok).toBe(true);
  });

  it("rejects just above the upper boundary", () => {
    expect(checkOddsFilter(0.9001).ok).toBe(false);
  });

  it("exports Joey's stated 10%/90% band", () => {
    expect(MIN_IMPLIED_PROB).toBe(0.1);
    expect(MAX_IMPLIED_PROB).toBe(0.9);
  });
});
