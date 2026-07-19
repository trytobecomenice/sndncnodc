// The Early-Exit Engine's rule math (Joey, 2026-07-20) — pure, unit-tested functions deciding
// whether an open paper position should be held to settlement or exited early. Same split as
// orderSizing.ts: no DB/network I/O here, earlyExit.ts is the orchestrator that calls these.
//
// Reuses Rule 7's existing edge floor (5pp) as BOTH the entry bar and the exit trigger — a
// position's edge decaying below the same bar a new trade would need to clear is exactly "alpha
// decay": there's no real edge left to justify holding. No new profit-capture-percentage constant
// was invented for this, deliberately, to keep the two rules consistent with each other.

import type { TempBufferCheck } from "./orderSizing";

export type Side = "Yes" | "No";
export type ExitReason = "profit_target" | "stop_loss_model_inversion" | "stop_loss_temp_buffer";

export interface ExitDecision {
  shouldExit: boolean;
  reason: ExitReason | null;
  /** The fresh edge, re-signed from the HELD side's perspective — positive means our side is
   * still favored, negative means the market (or model) has moved against it. */
  sideEdge: number;
}

/** `freshEdge` is the latest weather_probability_estimate.edge, always computed from the "Yes"
 * outcome's perspective (blendedProb - marketImpliedProb) — re-signed here to the position's own
 * held side before any comparison, since a "No" position's own edge is the mirror image. */
export function evaluateExit(
  heldOutcome: Side,
  freshEdge: number,
  edgeFloor: number,
  tempBufferCheck: TempBufferCheck
): ExitDecision {
  const sideEdge = heldOutcome === "Yes" ? freshEdge : -freshEdge;

  // Stop-loss (b): the fresh forecast has drifted onto/near the wrong side of the strike,
  // independent of what the probability-based edge currently says — checked first since a point-
  // forecast drift is a more immediate danger signal than the blended probability, which can lag.
  if (!tempBufferCheck.passes) {
    return { shouldExit: true, reason: "stop_loss_temp_buffer", sideEdge };
  }

  // Stop-loss (a): a REAL opposing edge has emerged (not just noise) — the model has inverted.
  if (sideEdge < 0 && Math.abs(sideEdge) >= edgeFloor) {
    return { shouldExit: true, reason: "stop_loss_model_inversion", sideEdge };
  }

  // Profit target / alpha decay: still favorable, but no longer clears the same bar a new trade
  // would need to clear — the market has converged toward our view, most of the edge is captured.
  if (sideEdge >= 0 && sideEdge < edgeFloor) {
    return { shouldExit: true, reason: "profit_target", sideEdge };
  }

  return { shouldExit: false, reason: null, sideEdge };
}

/** Realized PnL for a closed paper position: (exitPrice - entryPrice) * shares held. Pure, no I/O
 * — exitPrice/entryPrice are both already expressed in the held outcome's own price terms (a "No"
 * position's entryPrice/exitPrice are No-side prices, not Yes-side). */
export function computeRealizedPnl(entryPrice: number, exitPrice: number, shares: number): number {
  return (exitPrice - entryPrice) * shares;
}
