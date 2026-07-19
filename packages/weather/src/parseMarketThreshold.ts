// Title/slug parsing engine (Joey, 2026-07-20) — extracts the actual temperature threshold a
// Polymarket weather market resolves against, from the market's SLUG (not free-text title, see
// below), for weather_market_mapping.metric/targetTempMinF/targetTempMaxF.
//
// PARSES THE SLUG, NOT THE TITLE — a deliberate correction from the literal ask. Live-verified
// against all 100 real events available (2026-07-20): the slug suffix follows exactly 6
// consistent, machine-parseable patterns across every city and both "highest"/"lowest" event
// types — free-text titles are natural language ("Will the highest temperature in Seoul be 22°C
// on July 19?") and far more fragile to parse reliably than the already-structured slug.
//
// REAL PATTERNS CONFIRMED LIVE (2026-07-20), not assumed:
//   Celsius cities:   ...-21corbelow  ...-22c  ...-31corhigher
//   Fahrenheit cities: ...-73forbelow ...-74-75f ...-92forhigher   (no bare "-73f" exact form —
//                      Fahrenheit markets always use 2-degree ranges for interior buckets)
//   Both "highest-temperature-in-X" and "lowest-temperature-in-X" event types exist (e.g.
//   lowest-temperature-in-seoul, lowest-temperature-in-nyc) — the ensemble/historical query must
//   use tMinF, not tMaxF, for a "lowest" market, or the probability is simply wrong.
//
// ROUNDING-BOUNDARY MATH, GROUNDED IN THE REAL RESOLUTION RULE: every market's own description
// states its resolution source "measures temperatures to whole degrees [C|F]... this is the
// level of precision used when resolving" — confirmed live for both a Celsius market (Seoul) and
// a Fahrenheit market (NYC). That means a bucket's TRUE continuous-value boundary is wider than
// the stated integer by ±0.5° in the market's OWN native unit: "22C" really means the raw reading
// rounds to 22, i.e. the true value is anywhere in [21.5C, 22.5C]; "74-75F" really means
// [73.5F, 75.5F]; "21C or below" means anywhere below 21.5C. This expansion happens in the
// market's native unit first, then the whole range converts to Fahrenheit — converting the
// integer boundary alone (skipping the ±0.5 expansion) would be a systematic, silent bias in
// every downstream probability calculation.

export type Metric = "max" | "min";

export interface ParsedThreshold {
  metric: Metric;
  /** Always Fahrenheit, always already boundary-expanded — see module comment. Null = open-ended
   * below. */
  targetTempMinF: number | null;
  /** Always Fahrenheit, always already boundary-expanded. Null = open-ended above. */
  targetTempMaxF: number | null;
}

function cToF(c: number): number {
  return (c * 9) / 5 + 32;
}

// The unit/suffix must be ONE combined alternation, not "(c|f)" followed by an optional suffix
// group — a naive split fails because "corbelow"/"corhigher" both START with "c" (and
// "forbelow"/"forhigher" both start with "f"), so a greedy "(c|f)" matches just that leading
// letter before the suffix group ever sees it, silently breaking the whole match. Caught by this
// file's own test suite (4 of 9 cases failed on first run) — longer alternatives listed first so
// they're tried before the bare single-letter "c"/"f" forms.
const SLUG_RE =
  /^(highest|lowest)-temperature-in-[a-z0-9-]+-on-[a-z]+-\d+-\d{4}-(\d+)(?:-(\d+))?(corbelow|corhigher|forbelow|forhigher|c|f)$/;

/**
 * Parses a weather market's slug into a metric + Fahrenheit threshold range. Returns null for
 * anything that doesn't match a recognized temperature-bucket shape (e.g. the Polymarket
 * "Weather" category's non-station markets — air quality, earthquake counts, "hottest year on
 * record" — verified live to exist and correctly NOT match this pattern) — callers must skip,
 * never guess.
 *
 * INPUT:  slug, e.g. "highest-temperature-in-seoul-on-july-19-2026-22c" or
 *         "highest-temperature-in-nyc-on-july-19-2026-74-75f"
 * OUTPUT: { metric, targetTempMinF, targetTempMaxF } or null
 */
export function parseMarketThreshold(slug: string): ParsedThreshold | null {
  const match = SLUG_RE.exec(slug);
  if (!match) return null;

  const [, direction, num1Str, num2Str, unitOrSuffix] = match;
  const suffix = ["corbelow", "corhigher", "forbelow", "forhigher"].includes(unitOrSuffix) ? unitOrSuffix : null;

  const metric: Metric = direction === "highest" ? "max" : "min";
  const num1 = Number(num1Str);
  const num2 = num2Str !== undefined ? Number(num2Str) : null;

  // Native-unit boundary, BEFORE Fahrenheit conversion — ±0.5 expansion applied here, in
  // whatever unit the market itself uses, per the module comment.
  let nativeMin: number | null;
  let nativeMax: number | null;

  if (suffix === "corbelow" || suffix === "forbelow") {
    nativeMin = null;
    nativeMax = num1 + 0.5;
  } else if (suffix === "corhigher" || suffix === "forhigher") {
    nativeMin = num1 - 0.5;
    nativeMax = null;
  } else if (num2 !== null) {
    // A ranged bucket, e.g. "74-75f" — Fahrenheit-only pattern, confirmed live (Celsius markets
    // never use a range, always single-degree exact buckets instead).
    nativeMin = num1 - 0.5;
    nativeMax = num2 + 0.5;
  } else {
    // Exact single-degree bucket, e.g. "22c" — Celsius-only pattern, confirmed live.
    nativeMin = num1 - 0.5;
    nativeMax = num1 + 0.5;
  }

  const isCelsius = unitOrSuffix === "c" || unitOrSuffix === "corbelow" || unitOrSuffix === "corhigher";
  return {
    metric,
    targetTempMinF: nativeMin === null ? null : isCelsius ? cToF(nativeMin) : nativeMin,
    targetTempMaxF: nativeMax === null ? null : isCelsius ? cToF(nativeMax) : nativeMax,
  };
}
