// Dual Oracle Cross-Check (Joey, 2026-07-19) — extends
// docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 6 (Fail-closed settlement verification) with a
// concrete, automatic second failure mode it previously only described in general terms
// ("sanity-checked against an independent band... plus the same-day METAR/forecast readings").
//
// WHY THIS EXISTS, SPECIFICALLY: Rule 6's original concern was a scraper silently returning a
// plausible-looking but WRONG value (site redesign, wrong day/station, stale cache) — "Failure
// Mode 2" in the Red Team analysis. A single scraped number has no way to self-report that kind
// of corruption. Comparing it against a SECOND, INDEPENDENT, already-trusted-for-nowcast source
// (METAR, same station, same hour) gives an automatic sanity check with no extra network cost
// beyond what ingestMetar.ts-style fetching already does — if the two disagree by more than a
// physically plausible amount, that disagreement itself is the signal something is broken,
// regardless of which of the two is actually wrong.
//
// NOTE ON SCOPE: this file is PURE COMPARISON LOGIC ONLY — it takes two already-fetched
// readings and decides pass/anomaly. It does not fetch anything itself. The actual Wunderground
// read (verifySettlement.ts, Playwright-based) is a deliberately separate, not-yet-built step —
// see docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 5's note that the fetch itself is the more
// sensitive piece and gets its own dedicated review before touching the live site.

export interface SettlementCrossCheckResult {
  ok: boolean;
  deltaF: number;
  reason: string | null;
}

/** Joey's stated threshold: Wunderground reading more than 4°F hotter or colder than the
 * same-hour METAR reading at the same station is treated as physically implausible for two
 * readings of (nominally) the same real-world moment — not a "the world is different" signal,
 * a "something in the pipeline is broken" signal. This is deliberately a much tighter band than
 * the ~1-2°F systematic offset the reconciliation PoC actually measured for a DAILY high/low
 * (different instruments/aggregation windows, expected to differ somewhat) — an HOURLY,
 * same-instant cross-check has no aggregation-window excuse for a large gap the way a daily
 * max/min comparison does, so a wider anomaly here is far more likely to mean "broken," not
 * "normal micro-climate variation." */
export const MAX_HOURLY_CROSS_CHECK_DELTA_F = 4;

/**
 * Compares a Wunderground settlement reading against the co-located METAR reading for the same
 * station at the same hour, and decides whether the gap is small enough to trust.
 *
 * INPUT:  wundergroundF     — the scraped Wunderground temperature reading, °F
 *         metarSameHourF    — the METAR reading for the SAME station, SAME hour (caller's
 *                             responsibility to align these — this function does no time-window
 *                             logic of its own, matching the "each function does one thing"
 *                             convention already used throughout this codebase)
 *         maxDeltaF         — defaults to MAX_HOURLY_CROSS_CHECK_DELTA_F; kept overridable so
 *                             this stays unit-testable against edge cases
 * OUTPUT: { ok, deltaF, reason } — ok=false means: per
 *         docs/weather/WEATHER_RISK_MANAGEMENT.md Rule 4, trip the anomaly gate immediately —
 *         this specific Wunderground reading must not be trusted as a settlement value, and
 *         (per Rule 4's existing behavior) any pending order is aborted and the affected
 *         position is escalated exactly as any other anomaly trip would be.
 */
export function checkSettlementAgainstMetar(
  wundergroundF: number,
  metarSameHourF: number,
  maxDeltaF: number = MAX_HOURLY_CROSS_CHECK_DELTA_F
): SettlementCrossCheckResult {
  const deltaF = wundergroundF - metarSameHourF;
  const absDeltaF = Math.abs(deltaF);

  if (absDeltaF > maxDeltaF) {
    return {
      ok: false,
      deltaF,
      reason:
        `Wunderground reading ${wundergroundF}F is ${absDeltaF.toFixed(1)}F ${deltaF > 0 ? "warmer" : "colder"} ` +
        `than the same-hour METAR reading ${metarSameHourF}F, exceeding the ${maxDeltaF}F dual-oracle ` +
        `cross-check threshold — treating as a possible stealth site-structure change or bad scrape, ` +
        `not a real settlement value. Tripping the anomaly gate (Rule 4) rather than trusting this read.`,
    };
  }

  return { ok: true, deltaF, reason: null };
}
