// npm run review:outcomes
//
// =============================================================================
// WHAT THIS SCRIPT DOES, IN ONE PARAGRAPH (Stage 1 of 3 — see Rule 22)
// =============================================================================
// This is Stage 1 ("the outcome_review generator") of the formally-planned,
// user-approved reviewOutcomes.ts design — see docs/copy-trading/
// RISK_MANAGEMENT.md Rule 22 for the full architecture. Stage 2 (Brier score
// calibration) and Stage 3 (Welch's-t-test structural-break/regime-shift
// detection) are NOT built yet; they read FROM outcome_review, so this stage
// has to exist first. For every CLOSED paper_trade that doesn't already have
// a corresponding outcome_review row, this writes one: was_correct_call =
// (realized_pnl_usd > 0), pnl_usd = realized_pnl_usd (already computed by
// bot.py's close path — no network calls here at all, this is a pure local
// DB join), final_outcome mirrors close_reason (the market's literal Yes/No
// resolution isn't needed for calibration/structural-break analysis —
// deferred, not forgotten, until a real need for it shows up), and
// contributing_score_factors_json is a COPY-THROUGH (not just a reference by
// id) of the OPENING decision's score_breakdown_json + rule_set_version
// (bot.py/db.py, 2026-07-23 — see Rule 22's "prerequisites" section) so this
// analysis survives independently of decision_journal's own lifecycle (which
// may one day get a retention/prune policy the way bot_event_log already
// does — see Rule 17).
//
// Idempotent and incremental: safe to run repeatedly (e.g. daily, matching
// this exact package.json script slot) without reprocessing trades already
// reviewed.
//
// =============================================================================
// JOIN DIRECTION — a deliberate refinement of the approved plan, stated here
// =============================================================================
// A paper_trade can receive multiple buys (config.MAX_BUYS_PER_TRADER_OUTCOME
// allows averaging up), so decision_journal.linked_paper_trade_id is
// many-to-one (every buy that touched a position gets linked, not just the
// first). Joining outcome_review's "why did we open this position" back to a
// SINGLE decision therefore uses paper_trade.decision_journal_id specifically
// (the OPENING decision only, set once — see Rule 22) rather than the reverse
// linked_paper_trade_id direction, which could resolve to several rows for
// the same trade. This is the unambiguous, 1:1 direction for this purpose.
//
// =============================================================================
// HONEST LIMITATION (see Rule 22's "prerequisites" section)
// =============================================================================
// A paper_trade closed BEFORE the score-snapshot prerequisites shipped
// (2026-07-23) has no decision_journal_id at all. It still gets an
// outcome_review row here — just with contributing_score_factors_json =
// NULL, never guessed at by reconstructing a score from wallet_profile's
// CURRENT (since-overwritten) values.

import { and, eq, isNotNull } from "drizzle-orm";
import { db, decisionJournal, outcomeReview, paperTrade } from "@copybot/db";
import { DEFAULT_RULES } from "./scoreWallets";

export interface ContributingScoreFactors {
  score_breakdown: unknown | null;
  rule_set_version: number | null;
}

// Minimal shape this module's pure logic actually needs from a paper_trade
// row — narrower than Drizzle's full inferred row type, so the pure
// functions below are testable with plain object literals, no DB/schema
// machinery involved (same "extract the decision logic, leave DB IO
// untested directly" convention scoreWalletCategories.ts/
// discoverCategorySpecialists.ts already established).
export interface ClosedPaperTradeInput {
  id: string;
  marketSlug: string;
  outcome: string;
  walletAddress: string;
  closedAt: Date | null;
  closeReason: string | null;
  realizedPnlUsd: number | null;
}

export type OutcomeReviewRow =
  | { skipped: "missing_pnl" }
  | {
      skipped: false;
      values: {
        marketSlug: string;
        outcome: string;
        walletAddress: string;
        paperTradeId: string;
        resolvedAt: Date;
        finalOutcome: string;
        wasCorrectCall: boolean;
        pnlUsd: number;
        contributingScoreFactorsJson: string | null;
      };
    };

/**
 * Pure decision logic for turning one closed paper_trade (+ its already-
 * fetched score factors, if any) into an outcome_review row — no DB access,
 * directly unit-testable. See module docstring for the v1 simplifications
 * (final_outcome mirrors close_reason; realized_pnl_usd === null is skipped,
 * never coerced into a guessed boolean).
 */
export function buildOutcomeReviewRow(
  trade: ClosedPaperTradeInput,
  scoreFactors: ContributingScoreFactors | null
): OutcomeReviewRow {
  if (trade.realizedPnlUsd === null) {
    return { skipped: "missing_pnl" };
  }
  return {
    skipped: false,
    values: {
      marketSlug: trade.marketSlug,
      outcome: trade.outcome,
      walletAddress: trade.walletAddress,
      paperTradeId: trade.id,
      // closedAt is set unconditionally by every close path in db.py (see
      // Rule 22's technical detail) — falling back to "now" only guards
      // against outcome_review.resolved_at's NOT NULL constraint on a
      // theoretically malformed row, not an expected case.
      resolvedAt: trade.closedAt ?? new Date(),
      finalOutcome: trade.closeReason ?? "unknown",
      wasCorrectCall: trade.realizedPnlUsd > 0,
      pnlUsd: trade.realizedPnlUsd,
      contributingScoreFactorsJson: scoreFactors ? JSON.stringify(scoreFactors) : null,
    },
  };
}

/**
 * Filters closed trades down to the ones with no outcome_review row yet —
 * pure set-difference logic, directly unit-testable without a DB.
 */
export function filterUnreviewedTrades<T extends { id: string }>(
  closedTrades: T[],
  alreadyReviewedPaperTradeIds: Set<string>
): T[] {
  return closedTrades.filter((t) => !alreadyReviewedPaperTradeIds.has(t.id));
}

async function getAlreadyReviewedPaperTradeIds(): Promise<Set<string>> {
  const rows = await db
    .select({ paperTradeId: outcomeReview.paperTradeId })
    .from(outcomeReview)
    .where(isNotNull(outcomeReview.paperTradeId));
  return new Set(rows.map((r) => r.paperTradeId as string));
}

/**
 * Pulls the opening decision's score_breakdown_json + rule_set_version for
 * one paper_trade, via its decision_journal_id (see module docstring on why
 * this direction, not linked_paper_trade_id). Returns null when the trade
 * predates the FK link (see "honest limitation" above) — callers must not
 * substitute a guess.
 */
async function fetchContributingScoreFactors(
  decisionJournalId: string | null
): Promise<ContributingScoreFactors | null> {
  if (!decisionJournalId) return null;
  const rows = await db
    .select({
      scoreBreakdownJson: decisionJournal.scoreBreakdownJson,
      ruleSetVersion: decisionJournal.ruleSetVersion,
    })
    .from(decisionJournal)
    .where(eq(decisionJournal.id, decisionJournalId))
    .limit(1);
  if (rows.length === 0) return null;
  const row = rows[0];
  return {
    score_breakdown: row.scoreBreakdownJson ? JSON.parse(row.scoreBreakdownJson) : null,
    rule_set_version: row.ruleSetVersion,
  };
}

/**
 * Generates outcome_review rows for every closed paper_trade not yet
 * reviewed. Returns counts, not rows (the rows are already durably written
 * by the time this resolves). Exported so main() stays a thin
 * argv/console.log wrapper, matching every other script in this package.
 */
export async function generateOutcomeReviews(): Promise<{
  written: number;
  skippedMissingPnl: number;
}> {
  const alreadyReviewed = await getAlreadyReviewedPaperTradeIds();

  const closedTrades = await db
    .select()
    .from(paperTrade)
    .where(and(eq(paperTrade.status, "closed"), eq(paperTrade.isDemoData, false)));

  const pending = filterUnreviewedTrades(closedTrades, alreadyReviewed);

  let written = 0;
  let skippedMissingPnl = 0;
  for (const trade of pending) {
    const scoreFactors = await fetchContributingScoreFactors(trade.decisionJournalId);
    const row = buildOutcomeReviewRow(trade, scoreFactors);
    if (row.skipped) {
      skippedMissingPnl++;
      continue;
    }
    await db.insert(outcomeReview).values(row.values);
    written++;
  }

  return { written, skippedMissingPnl };
}

// =============================================================================
// STAGE 2: BRIER SCORE CALIBRATION (Rule 22 Part C)
// =============================================================================
// Tests whether the win-rate SNAPSHOTTED at decision time (bot.py's
// score_breakdown, see Rule 22) is actually calibrated OUT OF SAMPLE against
// what happened to trades copied after that scoring window closed. Brier
// score (Brier, 1950): mean((forecast - actual)^2), 0 = perfect, 0.25 =
// random-guessing-at-50%, 1 = maximally wrong.
//
// SCOPE, stated explicitly: only decisions where sizing_tier === "category"
// carry a win-rate forecast at all — db.get_wallet_composite_scores() only
// ever surfaced {score, pnl_t_stat} for the "composite" tier (no separate
// composite-level win_rate is tracked anywhere today), so a "base"/
// "composite"-tier decision has nothing to calibrate here. Excluded, not
// guessed at — same "never fabricate a signal we don't have" discipline as
// everywhere else in this codebase. A future addition could track
// wallet_profile.win_rate (the raw column already populated by
// scoreWallets.ts) for the composite tier too — deliberately not done
// tonight, to keep this addition scoped to what was actually approved.

export interface BrierCalibrationInput {
  walletAddress: string;
  category: string;
  forecastWinRate: number;
  actualOutcome: boolean;
}

export interface CalibrationBucket {
  bucketLabel: string;
  predictedMeanWinRate: number;
  actualWinRate: number;
  n: number;
}

export interface BrierCalibrationResult {
  key: string;
  n: number;
  brierScore: number;
  buckets: CalibrationBucket[];
}

// Reuses the SAME minimum-sample constant Rule 18/20/21 already use (not a
// new number invented for this feature) — see scoreWallets.ts's DEFAULT_RULES.
const MIN_BRIER_SAMPLE = DEFAULT_RULES.hardMinTrades;

/** Pure — mean squared forecast error. Assumes a non-empty array. */
export function computeBrierScore(inputs: { forecastWinRate: number; actualOutcome: boolean }[]): number {
  const sumSq = inputs.reduce(
    (sum, i) => sum + (i.forecastWinRate - (i.actualOutcome ? 1 : 0)) ** 2,
    0
  );
  return sumSq / inputs.length;
}

// Quintile edges — a single pooled Brier score can hide systematic over/
// under-confidence a bucketed reliability table makes visible at a glance
// (the simple version of the Murphy 1973 reliability/resolution/uncertainty
// decomposition; the full decomposition is a deliberate v2, not built here).
const CALIBRATION_BUCKET_EDGES = [0, 0.2, 0.4, 0.6, 0.8, 1.0];

/** Pure — buckets by forecastWinRate into quintiles; omits empty buckets. */
export function bucketCalibration(
  inputs: { forecastWinRate: number; actualOutcome: boolean }[]
): CalibrationBucket[] {
  const buckets: CalibrationBucket[] = [];
  for (let i = 0; i < CALIBRATION_BUCKET_EDGES.length - 1; i++) {
    const lo = CALIBRATION_BUCKET_EDGES[i];
    const hi = CALIBRATION_BUCKET_EDGES[i + 1];
    const isLastBucket = i === CALIBRATION_BUCKET_EDGES.length - 2;
    const inBucket = inputs.filter(
      (x) => x.forecastWinRate >= lo && (isLastBucket ? x.forecastWinRate <= hi : x.forecastWinRate < hi)
    );
    if (inBucket.length === 0) continue;
    const predictedMeanWinRate = inBucket.reduce((s, x) => s + x.forecastWinRate, 0) / inBucket.length;
    const actualWinRate = inBucket.filter((x) => x.actualOutcome).length / inBucket.length;
    buckets.push({
      bucketLabel: `[${lo.toFixed(1)}, ${hi.toFixed(1)}${isLastBucket ? "]" : ")"}`,
      predictedMeanWinRate,
      actualWinRate,
      n: inBucket.length,
    });
  }
  return buckets;
}

/**
 * Groups by wallet (groupBy="wallet") or by wallet+category
 * (groupBy="wallet_category"), gates each group at minSample, and computes
 * a Brier score + calibration table per surviving group. Pure — no DB.
 */
export function computeBrierCalibration(
  inputs: BrierCalibrationInput[],
  groupBy: "wallet" | "wallet_category",
  minSample: number = MIN_BRIER_SAMPLE
): BrierCalibrationResult[] {
  const groups = new Map<string, BrierCalibrationInput[]>();
  for (const input of inputs) {
    const key = groupBy === "wallet" ? input.walletAddress : `${input.walletAddress}|${input.category}`;
    const list = groups.get(key) ?? [];
    list.push(input);
    groups.set(key, list);
  }

  const results: BrierCalibrationResult[] = [];
  for (const [key, group] of groups.entries()) {
    if (group.length < minSample) continue;
    results.push({
      key,
      n: group.length,
      brierScore: computeBrierScore(group),
      buckets: bucketCalibration(group),
    });
  }
  return results;
}

// =============================================================================
// STAGE 3: STRUCTURAL-BREAK TEST / REGIME-SHIFT DETECTION (Rule 22 Part D)
// =============================================================================
// A genuinely different test from Rule 19's — Rule 19 is a ONE-sample, ONE-
// tailed t-test against a fixed constant (zero PnL). This is WELCH'S TWO-
// SAMPLE t-test (Welch, 1947 — correct over Student's equal-variance test
// specifically because a regime shift plausibly changes variance too, not
// just the mean) comparing an EARLY vs. RECENT window of the same wallet's
// (or wallet+category's) outcome_review PnL history — TWO-tailed (a shift
// can be an improvement or a decline, unlike Rule 19's harm-only framing).
//
// FIXED-WINDOW split, not a full Chow-test/CUSUM breakpoint scan — a
// deliberate simplification (see Rule 22): scanning every candidate
// breakpoint and reporting the best one is a real multiple-comparisons risk
// without the sample size to have genuine power at today's scale.
//
// REFINEMENT over the originally-approved plan text, documented here rather
// than silently substituted: the plan named "critical value 1.96" — the
// NORMAL-distribution two-tailed 95% value, only a valid approximation for
// LARGE degrees of freedom. With WINDOW_SIZE=10 fixed on both sides, Welch-
// Satterthwaite degrees of freedom always fall in [n-1, 2n-2] = [9, 18] —
// small enough that the true Student's-t critical value is meaningfully
// higher (2.10-2.26) than 1.96, and using 1.96 anyway would systematically
// OVER-flag (make false positives MORE likely, not fewer). Implemented
// properly below with the standard tabulated small-df critical values
// instead.

export interface StructuralBreakInput {
  walletAddress: string;
  category: string | null; // null for the per-wallet grouping (no score snapshot needed for that one — see below)
  resolvedAt: Date;
  pnlUsd: number;
}

export interface StructuralBreakResult {
  key: string;
  n: number; // window size (both windows are always this size, by construction)
  earlyMeanPnl: number;
  recentMeanPnl: number;
  tStat: number;
  degreesOfFreedom: number;
  criticalValue: number;
  flagged: boolean;
  direction: "improved" | "declined" | "unchanged";
}

// Reuses hardMinTrades (5) doubled, so each window independently clears the
// existing minimum-sample convention used everywhere else in this codebase.
const STRUCTURAL_BREAK_WINDOW = DEFAULT_RULES.hardMinTrades * 2;

// Finite sentinel, not Infinity — same reasoning as aggregateCategoryScores'
// EXTREME_T_STAT_SENTINEL (scoreWalletCategories.ts): JSON.stringify(Infinity)
// silently becomes null, which would flip "maximally strong evidence of a
// shift" into "no evidence at all" the moment this round-trips through a log
// or a future stored report.
const EXTREME_T_STAT_SENTINEL = 1e6;

// Standard two-tailed alpha=0.05 Student's t critical values, tabulated for
// exactly the df range this fixed-window design can produce (df in [9,18]
// per the module comment above) — not computed via a numerical inverse-CDF,
// since the range is small and bounded by construction.
const T_CRITICAL_TWO_TAILED_95: Record<number, number> = {
  9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.16, 14: 2.145,
  15: 2.131, 16: 2.12, 17: 2.11, 18: 2.101,
};
const T_CRITICAL_NORMAL_APPROX = 1.96; // large-df fallback, if WINDOW_SIZE is ever reconfigured larger

/** Pure — looks up (or approximates) the two-tailed 95% critical value for a given df. */
export function tCriticalValue(degreesOfFreedom: number): number {
  const df = Math.max(1, Math.floor(degreesOfFreedom)); // round DOWN — conservative, standard practice
  if (df in T_CRITICAL_TWO_TAILED_95) return T_CRITICAL_TWO_TAILED_95[df];
  if (df > 18) return T_CRITICAL_NORMAL_APPROX;
  // df < 9 shouldn't happen given STRUCTURAL_BREAK_WINDOW=10 fixed on both
  // sides, but stay safe rather than crash: use the most conservative
  // (highest) tabulated value instead of extrapolating.
  return T_CRITICAL_TWO_TAILED_95[9];
}

/**
 * Welch's two-sample t-test (unequal variance) with Welch-Satterthwaite
 * degrees of freedom. Pure, directly unit-testable. Zero-variance-on-both-
 * sides is handled the same way aggregateCategoryScores handles a single
 * zero-variance sample: identical means -> t=0 (no evidence either way);
 * different means -> the finite sentinel (maximally strong evidence).
 */
export function welchTTest(early: number[], recent: number[]): { tStat: number; degreesOfFreedom: number } {
  const n1 = early.length;
  const n2 = recent.length;
  const mean = (xs: number[]) => xs.reduce((s, x) => s + x, 0) / xs.length;
  const variance = (xs: number[], m: number) => xs.reduce((s, x) => s + (x - m) ** 2, 0) / (xs.length - 1 || 1);

  const m1 = mean(early);
  const m2 = mean(recent);
  const v1 = variance(early, m1);
  const v2 = variance(recent, m2);
  const se2 = v1 / n1 + v2 / n2;

  if (se2 === 0) {
    const tStat = m1 === m2 ? 0 : m2 > m1 ? EXTREME_T_STAT_SENTINEL : -EXTREME_T_STAT_SENTINEL;
    return { tStat, degreesOfFreedom: n1 + n2 - 2 };
  }

  const tStat = (m2 - m1) / Math.sqrt(se2); // positive = recent > early = "improved"
  const degreesOfFreedom =
    se2 ** 2 / ((v1 / n1) ** 2 / (n1 - 1 || 1) + (v2 / n2) ** 2 / (n2 - 1 || 1));
  return { tStat, degreesOfFreedom };
}

/**
 * Splits a chronologically-sorted PnL series into a fixed-size early/recent
 * window pair (most recent WINDOW vs. the WINDOW before that) and runs
 * welchTTest. Returns null if there isn't enough history for two full,
 * non-overlapping windows — insufficient data, not a guess.
 */
export function detectStructuralBreak(
  chronologicalPnl: number[],
  windowSize: number = STRUCTURAL_BREAK_WINDOW
): Omit<StructuralBreakResult, "key"> | null {
  if (chronologicalPnl.length < windowSize * 2) return null;

  const recent = chronologicalPnl.slice(chronologicalPnl.length - windowSize);
  const early = chronologicalPnl.slice(chronologicalPnl.length - windowSize * 2, chronologicalPnl.length - windowSize);

  const { tStat, degreesOfFreedom } = welchTTest(early, recent);
  const criticalValue = tCriticalValue(degreesOfFreedom);
  const flagged = Math.abs(tStat) >= criticalValue;
  const direction = tStat > 0 ? "improved" : tStat < 0 ? "declined" : "unchanged";

  return {
    n: windowSize,
    earlyMeanPnl: early.reduce((s, x) => s + x, 0) / early.length,
    recentMeanPnl: recent.reduce((s, x) => s + x, 0) / recent.length,
    tStat,
    degreesOfFreedom,
    criticalValue,
    flagged,
    direction,
  };
}

/**
 * Groups by wallet (groupBy="wallet", category-agnostic — uses every
 * outcome_review row for that wallet regardless of whether it has a score
 * snapshot, since only walletAddress/resolvedAt/pnlUsd are needed) or by
 * wallet+category (groupBy="wallet_category" — needs `category` non-null on
 * every input, i.e. only rows with a score snapshot), sorts each group
 * chronologically, and runs detectStructuralBreak per group. Pure — no DB.
 */
export function computeStructuralBreaks(
  inputs: StructuralBreakInput[],
  groupBy: "wallet" | "wallet_category",
  windowSize: number = STRUCTURAL_BREAK_WINDOW
): StructuralBreakResult[] {
  const groups = new Map<string, StructuralBreakInput[]>();
  for (const input of inputs) {
    if (groupBy === "wallet_category" && input.category === null) continue;
    const key = groupBy === "wallet" ? input.walletAddress : `${input.walletAddress}|${input.category}`;
    const list = groups.get(key) ?? [];
    list.push(input);
    groups.set(key, list);
  }

  const results: StructuralBreakResult[] = [];
  for (const [key, group] of groups.entries()) {
    const sorted = [...group].sort((a, b) => a.resolvedAt.getTime() - b.resolvedAt.getTime());
    const result = detectStructuralBreak(
      sorted.map((x) => x.pnlUsd),
      windowSize
    );
    if (result) results.push({ key, ...result });
  }
  return results;
}

// =============================================================================
// SHARED FETCH — one query backs both Stage 2 and Stage 3
// =============================================================================

interface EnrichedOutcomeReviewRow {
  walletAddress: string;
  resolvedAt: Date;
  pnlUsd: number;
  wasCorrectCall: boolean;
  category: string | null; // from score_breakdown.category — null if no snapshot
  sizingTier: string | null;
  forecastWinRate: number | null; // from score_breakdown.category_score_detail.win_rate
}

async function fetchEnrichedOutcomeReviews(): Promise<EnrichedOutcomeReviewRow[]> {
  const rows = await db
    .select({
      walletAddress: outcomeReview.walletAddress,
      resolvedAt: outcomeReview.resolvedAt,
      pnlUsd: outcomeReview.pnlUsd,
      wasCorrectCall: outcomeReview.wasCorrectCall,
      contributingScoreFactorsJson: outcomeReview.contributingScoreFactorsJson,
    })
    .from(outcomeReview)
    .where(isNotNull(outcomeReview.pnlUsd));

  return rows.map((row) => {
    let category: string | null = null;
    let sizingTier: string | null = null;
    let forecastWinRate: number | null = null;
    if (row.contributingScoreFactorsJson) {
      try {
        const factors = JSON.parse(row.contributingScoreFactorsJson) as ContributingScoreFactors;
        const breakdown = factors.score_breakdown as
          | { category?: string; sizing_tier?: string; category_score_detail?: { win_rate?: number } }
          | null
          | undefined;
        if (breakdown) {
          category = breakdown.category ?? null;
          sizingTier = breakdown.sizing_tier ?? null;
          if (sizingTier === "category" && typeof breakdown.category_score_detail?.win_rate === "number") {
            forecastWinRate = breakdown.category_score_detail.win_rate;
          }
        }
      } catch {
        // Malformed JSON degrades to "no snapshot" — never crashes the report.
      }
    }
    return {
      walletAddress: row.walletAddress,
      resolvedAt: row.resolvedAt,
      pnlUsd: row.pnlUsd as number,
      wasCorrectCall: row.wasCorrectCall,
      category,
      sizingTier,
      forecastWinRate,
    };
  });
}

async function main() {
  const { written, skippedMissingPnl } = await generateOutcomeReviews();
  console.log(
    `outcome_review: wrote ${written} new row(s)` +
      (skippedMissingPnl > 0
        ? `, skipped ${skippedMissingPnl} closed trade(s) with no realized_pnl_usd yet (will retry next run)`
        : "")
  );

  const enriched = await fetchEnrichedOutcomeReviews();

  const brierInputs: BrierCalibrationInput[] = enriched
    .filter((r) => r.category !== null && r.forecastWinRate !== null)
    .map((r) => ({
      walletAddress: r.walletAddress,
      category: r.category as string,
      forecastWinRate: r.forecastWinRate as number,
      actualOutcome: r.wasCorrectCall,
    }));

  console.log(`\n=== Stage 2: Brier score calibration (min sample ${MIN_BRIER_SAMPLE}) ===`);
  if (brierInputs.length === 0) {
    console.log(
      "  No decisions with a category-tier score snapshot yet — insufficient data, not an error " +
        "(see Rule 22's honest limitation: only decisions made after the prerequisites shipped qualify)."
    );
  } else {
    for (const groupBy of ["wallet", "wallet_category"] as const) {
      const results = computeBrierCalibration(brierInputs, groupBy);
      console.log(`\n  by ${groupBy} (n=${results.length} group(s) clearing the sample gate):`);
      for (const r of results) {
        console.log(`    ${r.key} | n=${r.n} | brier=${r.brierScore.toFixed(4)}`);
        for (const b of r.buckets) {
          console.log(
            `      ${b.bucketLabel} | predicted=${(b.predictedMeanWinRate * 100).toFixed(1)}% | ` +
              `actual=${(b.actualWinRate * 100).toFixed(1)}% | n=${b.n}`
          );
        }
      }
    }
  }

  const structuralBreakInputsPerWallet: StructuralBreakInput[] = enriched.map((r) => ({
    walletAddress: r.walletAddress,
    category: null,
    resolvedAt: r.resolvedAt,
    pnlUsd: r.pnlUsd,
  }));
  const structuralBreakInputsPerCategory: StructuralBreakInput[] = enriched
    .filter((r) => r.category !== null)
    .map((r) => ({
      walletAddress: r.walletAddress,
      category: r.category,
      resolvedAt: r.resolvedAt,
      pnlUsd: r.pnlUsd,
    }));

  console.log(`\n=== Stage 3: Structural-break test (Welch's two-sample t-test, window=${STRUCTURAL_BREAK_WINDOW}) ===`);
  const perWalletBreaks = computeStructuralBreaks(structuralBreakInputsPerWallet, "wallet");
  const perCategoryBreaks = computeStructuralBreaks(structuralBreakInputsPerCategory, "wallet_category");
  const allBreaks = [...perWalletBreaks, ...perCategoryBreaks];
  if (allBreaks.length === 0) {
    console.log(
      `  No wallet (or wallet+category) has ${STRUCTURAL_BREAK_WINDOW * 2}+ resolved outcomes yet — ` +
        "insufficient data, not an error."
    );
  } else {
    for (const r of allBreaks) {
      const flag = r.flagged ? ` | ⚠ FLAGGED (${r.direction})` : "";
      console.log(
        `  ${r.key} | early_mean=$${r.earlyMeanPnl.toFixed(2)} | recent_mean=$${r.recentMeanPnl.toFixed(2)} | ` +
          `t=${r.tStat.toFixed(2)} | df=${r.degreesOfFreedom.toFixed(1)} | crit=${r.criticalValue.toFixed(3)}${flag}`
      );
    }
  }

  console.log(
    "\nStages 2/3 are REPORT ONLY — nothing written to wallet_profile.circuitBreakerMuted or " +
      "rule_change. A flagged wallet is a candidate for manual review, same pattern as Rule 20."
  );
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}
