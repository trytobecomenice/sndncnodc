// Shared, correctness-critical constants used across multiple weather scripts. Deliberately kept
// to this one file only for values where drift between scripts would silently break paper-trading
// accounting (e.g. two different bankroll figures in orderBuilder.ts vs. updatePnl.ts) — most
// other tunables (edge floors, buffer widths) stay local to their own script, per this codebase's
// established convention, since disagreement between those is a much lower-stakes kind of drift.

/** Mock paper-trading capital base (Joey's stated default, 2026-07-20) — the first concrete value
 * for the capital-base constant Rule 11 was blocked on. Shared between orderBuilder.ts (position
 * sizing) and updatePnl.ts (equity accounting) so the two can never silently disagree on what "the
 * bankroll" actually is. */
export const WEATHER_PAPER_BANKROLL_USD = 10000;
