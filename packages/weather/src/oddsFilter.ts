// Extreme Odds Filter (Joey, 2026-07-19) — docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 10.
//
// Rationale, as given: markets priced under 10% implied probability are "lottery tickets" (deep
// out-of-the-money, thin edge-per-dollar-of-risk); markets priced over 90% are "capital-
// inefficient steamrollers" (deep in-the-money — a YES bet there ties up capital for a payout
// ratio too small to matter, and a NO bet there is the lottery-ticket problem in reverse). The
// bot should only track markets inside the active, genuinely-contested trading band.

export const MIN_IMPLIED_PROB = 0.1;
export const MAX_IMPLIED_PROB = 0.9;

export interface OddsFilterResult {
  ok: boolean;
  reason: string | null;
}

/**
 * Decides whether a market's current implied probability falls inside the active trading band.
 * INPUT:  impliedProb — the market's current "Yes" outcome price/probability, 0..1
 * OUTPUT: { ok, reason } — ok=true means the market is worth ingesting/tracking; ok=false means
 *         skip it entirely (see this file's header for why)
 */
export function checkOddsFilter(impliedProb: number): OddsFilterResult {
  if (impliedProb < MIN_IMPLIED_PROB) {
    return {
      ok: false,
      reason: `implied probability ${impliedProb} is below ${MIN_IMPLIED_PROB} — lottery-ticket territory, skipping`,
    };
  }
  if (impliedProb > MAX_IMPLIED_PROB) {
    return {
      ok: false,
      reason: `implied probability ${impliedProb} is above ${MAX_IMPLIED_PROB} — capital-inefficient steamroller, skipping`,
    };
  }
  return { ok: true, reason: null };
}
