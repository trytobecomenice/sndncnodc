import { describe, expect, it } from "vitest";
import { checkEmergencyCloseoutSlippage, DEFAULT_SLIPPAGE_CEILING } from "./emergencyCloseoutGuard";

describe("checkEmergencyCloseoutSlippage", () => {
  it("allows a sell with no adverse move", () => {
    const result = checkEmergencyCloseoutSlippage(0.5, 0.5);
    expect(result.ok).toBe(true);
    expect(result.reason).toBeNull();
  });

  it("allows a sell exactly at the slippage ceiling (5%)", () => {
    // reference 1.00 -> executable 0.95 is exactly 5% adverse
    const result = checkEmergencyCloseoutSlippage(1.0, 0.95);
    expect(result.ok).toBe(true);
  });

  it("blocks a sell just past the slippage ceiling", () => {
    const result = checkEmergencyCloseoutSlippage(1.0, 0.9499);
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/slippage ceiling/);
  });

  it("allows a sell that moved favorably (executable above reference)", () => {
    const result = checkEmergencyCloseoutSlippage(0.5, 0.6);
    expect(result.ok).toBe(true);
  });

  it("blocks a sell below the absolute price floor even with zero relative slippage", () => {
    // reference and executable both 0.03 -> 0% relative slippage, but under the $0.05 floor
    const result = checkEmergencyCloseoutSlippage(0.03, 0.03);
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/absolute floor/);
  });

  it("allows a sell exactly at the absolute price floor", () => {
    const result = checkEmergencyCloseoutSlippage(0.05, 0.05);
    expect(result.ok).toBe(true);
  });

  it("respects a custom, tighter configured ceiling", () => {
    const tight = { maxSlippagePct: 0.01, minSellPrice: 0.05 };
    const result = checkEmergencyCloseoutSlippage(1.0, 0.97, tight);
    expect(result.ok).toBe(false);
  });

  it("DEFAULT_SLIPPAGE_CEILING matches Joey's stated defaults (5% / $0.05)", () => {
    expect(DEFAULT_SLIPPAGE_CEILING.maxSlippagePct).toBe(0.05);
    expect(DEFAULT_SLIPPAGE_CEILING.minSellPrice).toBe(0.05);
  });
});
