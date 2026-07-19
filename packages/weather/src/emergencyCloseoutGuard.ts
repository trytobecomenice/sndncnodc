// Pure decision logic for the Sold-Out Switch's Emergency Closeout step
// (docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 4 — Extreme Data Anomaly Protection).
//
// PROBLEM THIS SOLVES: Rule 4, as originally written, said an anomaly trip "force[s] an
// Emergency Closeout (a market sell)" unconditionally. That's dangerous on its own — a weather
// market can go illiquid (thin book, wide spread, or simply no bids) at the exact moment
// something else has already gone wrong, and market-selling blind into that book can realize a
// far worse loss than the anomaly itself, for a paper/eventually-real principal-protection
// mechanism whose entire purpose is protecting capital. This mirrors bot.py's
// check_slippage_ceiling ("Disciplined Taker") in spirit — a risk mechanism must never trade its
// way into a worse outcome than the risk it's defending against — but is NOT the same function,
// because the two contexts differ:
//   - bot.py's version gates a NEW BUY only (Copy Bot exits are never gated, since a risk layer
//     that traps you in a losing position adds risk).
//   - THIS version gates an EMERGENCY SELL specifically, by explicit instruction (Joey,
//     2026-07-19): "the Sold-Out Switch must NOT blindly execute market sells." That is a
//     deliberate, different design choice for this different context (a sudden anomaly-driven
//     exit from a possibly one-off, thin weather market), not an oversight or inconsistency with
//     the Copy Bot's principle.
//
// RESULTING BEHAVIOR WHEN BLOCKED (important — this is not "do nothing"): if this check fails,
// the emergency closeout must NOT silently not-happen. The position stays open and MUST be
// flagged for urgent manual review (same fail-closed principle as Rule 6's settlement
// verification) — this function only decides whether an IMMEDIATE unconditional market sell is
// safe; it is not itself a decision to abandon principal protection. Wiring this into an actual
// retry/escalation path is `managePositions.ts`'s job once that's built (not yet — see
// docs/weather/WEATHER_ARCHITECTURE.md's "explicitly out of scope" list); this file is the
// tested, ready-to-wire-in decision function.

export interface SlippageCeilingConfig {
  /** Maximum adverse move (as a fraction, e.g. 0.05 = 5%) from the reference price before the
   * emergency sell is considered too costly to execute blind. Joey's stated default. */
  maxSlippagePct: number;
  /** Absolute price floor (Polymarket prices are 0..1) — never sell into a book quoting below
   * this, regardless of relative slippage, since a near-zero price usually means "no real bid,"
   * not "fair value happens to be low." Joey's stated default: $0.05. */
  minSellPrice: number;
}

export const DEFAULT_SLIPPAGE_CEILING: SlippageCeilingConfig = {
  maxSlippagePct: 0.05,
  minSellPrice: 0.05,
};

export interface SlippageCeilingResult {
  ok: boolean;
  reason: string | null;
}

/**
 * Decides whether an emergency closeout market sell is safe to execute right now.
 *
 * INPUT:  referencePrice   — the price the position should reasonably be worth absent an
 *                            anomaly (e.g. the position's last known-good mark, NOT the
 *                            anomalous reading itself — using the anomalous reading as the
 *                            reference would defeat the point of this check entirely)
 *         executablePrice  — the CURRENT price a real market sell could actually fill at (from
 *                            a fresh preview, same convention as bot.py's check_spread_tolerance)
 *         config           — defaults to DEFAULT_SLIPPAGE_CEILING; kept overridable so this is
 *                            unit-testable against edge cases without module-level mutation
 * OUTPUT: { ok, reason } — ok=true means the sell may proceed; ok=false means it must NOT
 *         execute blind (see the module comment above for what must happen instead)
 */
export function checkEmergencyCloseoutSlippage(
  referencePrice: number,
  executablePrice: number,
  config: SlippageCeilingConfig = DEFAULT_SLIPPAGE_CEILING
): SlippageCeilingResult {
  if (executablePrice < config.minSellPrice) {
    return {
      ok: false,
      reason:
        `executable price ${executablePrice} is below the absolute floor ${config.minSellPrice} — ` +
        `book likely has no real bid, not just a low fair value; refusing to sell blind`,
    };
  }

  // Selling is a BUY's mirror for slippage purposes: worse means receiving LESS than the
  // reference price, i.e. executablePrice < referencePrice. A tiny epsilon absorbs binary
  // floating-point noise (e.g. 1.0 - 0.95 != exactly 0.05 in IEEE 754) so a price landing
  // deliberately exactly on the ceiling isn't flipped to "blocked" by representation error —
  // verified necessary, not defensive-for-its-own-sake: caught by this file's own test suite.
  const EPSILON = 1e-9;
  const adversePct = (referencePrice - executablePrice) / referencePrice;
  if (adversePct > config.maxSlippagePct + EPSILON) {
    return {
      ok: false,
      reason:
        `executable price ${executablePrice} is ${(adversePct * 100).toFixed(1)}% below reference ` +
        `${referencePrice}, exceeding the ${(config.maxSlippagePct * 100).toFixed(0)}% slippage ceiling — ` +
        `refusing to dump into an illiquid book; position needs urgent manual review instead`,
    };
  }

  return { ok: true, reason: null };
}
