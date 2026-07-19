import { describe, expect, it } from "vitest";
import { checkSettlementAgainstMetar, MAX_HOURLY_CROSS_CHECK_DELTA_F } from "./checkSettlementAgainstMetar";

describe("checkSettlementAgainstMetar", () => {
  it("passes when both readings match exactly", () => {
    const result = checkSettlementAgainstMetar(78.8, 78.8);
    expect(result.ok).toBe(true);
    expect(result.deltaF).toBe(0);
  });

  it("passes within the 4F threshold", () => {
    const result = checkSettlementAgainstMetar(80, 77);
    expect(result.ok).toBe(true);
    expect(result.deltaF).toBe(3);
  });

  it("passes exactly at the 4F threshold", () => {
    const result = checkSettlementAgainstMetar(80, 76);
    expect(result.ok).toBe(true);
  });

  it("trips the anomaly gate just past the threshold (WU warmer)", () => {
    const result = checkSettlementAgainstMetar(80.1, 76);
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/warmer/);
    expect(result.reason).toMatch(/anomaly gate/);
  });

  it("trips the anomaly gate when WU reads colder than METAR", () => {
    const result = checkSettlementAgainstMetar(70, 76);
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/colder/);
  });

  it("respects a custom threshold", () => {
    const result = checkSettlementAgainstMetar(80, 78, 1);
    expect(result.ok).toBe(false);
  });

  it("exports Joey's stated 4F default", () => {
    expect(MAX_HOURLY_CROSS_CHECK_DELTA_F).toBe(4);
  });

  it("reproduces this session's actual PoC-scale gap as a real-world sanity check (informational, not a pass/fail assertion on the module)", () => {
    // The daily-high PoC found METAR 26C (78.8F) vs WU 80F -> an 1.2F gap on a DAILY max/min
    // comparison. This same-magnitude gap, applied hourly (same-instant), should still PASS this
    // tighter check, since 1.2F < 4F -- confirming the 4F threshold doesn't false-positive on
    // exactly the scale of discrepancy already proven to occur between these two sources.
    const result = checkSettlementAgainstMetar(80, 78.8);
    expect(result.ok).toBe(true);
  });
});
