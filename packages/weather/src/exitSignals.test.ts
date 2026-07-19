import { describe, expect, it } from "vitest";
import { computeRealizedPnl, evaluateExit } from "./exitSignals";

const passingBuffer = { passes: true, distanceF: 3.0 };
const failingBuffer = { passes: false, distanceF: 0.5 };

describe("evaluateExit", () => {
  it("holds a Yes position when its edge is still comfortably above the floor", () => {
    const result = evaluateExit("Yes", 0.15, 0.05, passingBuffer);
    expect(result.shouldExit).toBe(false);
    expect(result.reason).toBeNull();
    expect(result.sideEdge).toBeCloseTo(0.15, 5);
  });

  it("holds a No position when the Yes-side edge is negative enough to favor No comfortably", () => {
    // freshEdge=-0.20 (Yes-side) -> No-side edge = +0.20, still favorable
    const result = evaluateExit("No", -0.2, 0.05, passingBuffer);
    expect(result.shouldExit).toBe(false);
    expect(result.sideEdge).toBeCloseTo(0.2, 5);
  });

  it("flags profit_target when a Yes position's edge has decayed below the floor but is still positive", () => {
    const result = evaluateExit("Yes", 0.03, 0.05, passingBuffer);
    expect(result.shouldExit).toBe(true);
    expect(result.reason).toBe("profit_target");
  });

  it("flags profit_target when a No position's edge has decayed below the floor", () => {
    // freshEdge=-0.02 (Yes-side) -> No-side edge = +0.02, positive but under the 5pp floor
    const result = evaluateExit("No", -0.02, 0.05, passingBuffer);
    expect(result.shouldExit).toBe(true);
    expect(result.reason).toBe("profit_target");
  });

  it("flags stop_loss_model_inversion when a real opposing edge has emerged", () => {
    // Held Yes, but freshEdge is now -0.10 -> side edge -0.10, a real (>=5pp) opposing edge
    const result = evaluateExit("Yes", -0.1, 0.05, passingBuffer);
    expect(result.shouldExit).toBe(true);
    expect(result.reason).toBe("stop_loss_model_inversion");
  });

  it("does not flag model inversion for a small, noise-level adverse move under the floor", () => {
    const result = evaluateExit("Yes", -0.02, 0.05, passingBuffer);
    expect(result.shouldExit).toBe(false);
    expect(result.reason).toBeNull();
  });

  it("flags stop_loss_temp_buffer when the forecast has drifted into the buffer zone, even with a strong edge", () => {
    const result = evaluateExit("Yes", 0.3, 0.05, failingBuffer);
    expect(result.shouldExit).toBe(true);
    expect(result.reason).toBe("stop_loss_temp_buffer");
  });

  it("prioritizes the temp-buffer stop-loss over a profit-target read on the same position", () => {
    // Edge has decayed (would be profit_target on its own) AND buffer has failed -> buffer wins
    const result = evaluateExit("Yes", 0.02, 0.05, failingBuffer);
    expect(result.reason).toBe("stop_loss_temp_buffer");
  });
});

describe("computeRealizedPnl", () => {
  it("computes positive PnL when the exit price is above the entry price", () => {
    // Bought at 0.60, exit at 0.88, 100 shares
    expect(computeRealizedPnl(0.6, 0.88, 100)).toBeCloseTo(28, 5);
  });

  it("computes negative PnL when the exit price is below the entry price", () => {
    expect(computeRealizedPnl(0.6, 0.45, 100)).toBeCloseTo(-15, 5);
  });

  it("computes zero PnL when the exit price equals the entry price", () => {
    expect(computeRealizedPnl(0.6, 0.6, 100)).toBe(0);
  });
});
