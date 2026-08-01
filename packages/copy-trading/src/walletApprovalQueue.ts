// Telegram wallet-approval workflow (2026-08-01) — the single funnel every
// promotion path (scoreWallets.ts's global top-N pool AND
// discoverCategorySpecialists.ts's category-quota system, Rule 24) writes
// through instead of committing wallet_profile.status='track'/'bench'
// directly. See packages/db/src/schema.ts's walletApprovalRequest doc
// comment for the table shape, and send_wallet_approvals.py /
// telegram_approval_listener.py for the Telegram send/receive halves.
//
// Split into a pure decision function (shouldQueueApprovalRequest, directly
// unit-testable) and a thin DB I/O wrapper (queueApprovalRequest) — same
// "extract the pure decision, keep I/O separate" pattern scoreWallets.ts
// already uses for checkToxicFlowGate/decideStatus/computeDemotedAddresses.
// upsertWalletProfile-style DB-writing functions in this codebase aren't
// unit tested directly (no in-memory test DB convention exists here) —
// queueApprovalRequest is verified live instead, same as that precedent.

import { randomUUID } from "node:crypto";
import { and, eq } from "drizzle-orm";
import { db as defaultDb, walletApprovalRequest, walletProfile } from "@copybot/db";

// Explicit judgment call, not researched — same footing as this codebase's
// other unresearched constants (e.g. discoverCategorySpecialists.ts's
// TCA_MIN_ENTRY_PRICE). Long enough that a rejected candidate isn't re-asked
// every single day the daily scan runs, short enough that a wallet whose
// track record genuinely improves gets a second look within a few weeks.
export const APPROVAL_COOLDOWN_DAYS = 14;

export interface ScoreSnapshot {
  compositeScore?: number | null;
  pnlTStat?: number | null;
  winRate?: number | null;
  tradeCount?: number | null;
  roi?: number | null;
  washTradingSuspect?: boolean;
}

export interface QueueApprovalRequestArgs {
  walletAddress: string;
  requestedTier: "track" | "bench";
  source: "global_pool" | "category_quota";
  category: string | null;
  scoreSnapshot: ScoreSnapshot;
  reason: string;
}

export interface ExistingRequestSummary {
  status: string;
  resolvedAt: Date | null;
}

/**
 * Pure dedup/cooldown decision: given the wallet's CURRENT wallet_profile
 * status and its past wallet_approval_request history for THIS tier only
 * (caller is responsible for pre-filtering to one (wallet, requestedTier)
 * pair), decides whether a new pending request should be queued.
 *
 * Three reasons to say no, in order: (1) the wallet is already at the
 * requested status — nothing to approve; (2) a request for this exact
 * (wallet, tier) is already pending — don't spam a second Telegram message
 * for the same decision; (3) the most recent request for this pair was
 * REJECTED within the cooldown window — respect Joey's "no" for a while
 * instead of re-asking every time the daily scan happens to re-surface the
 * same candidate.
 */
export function shouldQueueApprovalRequest(
  requestedTier: string,
  currentStatus: string | null,
  existingRequestsForTier: ExistingRequestSummary[],
  now: Date = new Date(),
  cooldownDays: number = APPROVAL_COOLDOWN_DAYS
): boolean {
  if (currentStatus === requestedTier) return false;
  if (existingRequestsForTier.some((r) => r.status === "pending")) return false;

  const lastRejected = existingRequestsForTier
    .filter((r): r is ExistingRequestSummary & { resolvedAt: Date } => r.status === "rejected" && r.resolvedAt !== null)
    .sort((a, b) => b.resolvedAt.getTime() - a.resolvedAt.getTime())[0];

  if (lastRejected) {
    const cooldownMs = cooldownDays * 86400 * 1000;
    if (now.getTime() - lastRejected.resolvedAt.getTime() < cooldownMs) return false;
  }

  return true;
}

/**
 * DB I/O wrapper: fetches the state shouldQueueApprovalRequest needs, and —
 * if it says yes — inserts one new 'pending' wallet_approval_request row.
 * Idempotent in effect (a second call before Joey acts on the first is a
 * no-op via the "already pending" branch above), so callers can call this
 * freely per-candidate per-run without their own dedup bookkeeping.
 *
 * INPUT:  dbClient — the drizzle db handle (parameterized, not the module
 *         top-level import, so a future test DB can be swapped in without
 *         changing this function); args — see QueueApprovalRequestArgs.
 * OUTPUT: { queued: true, id } on insert, { queued: false } on any of the
 *         three skip reasons above.
 */
export async function queueApprovalRequest(
  args: QueueApprovalRequestArgs,
  dbClient: typeof defaultDb = defaultDb,
  now: Date = new Date()
): Promise<{ queued: boolean; id?: string }> {
  const address = args.walletAddress.toLowerCase();

  const profileRows = await dbClient
    .select({ status: walletProfile.status })
    .from(walletProfile)
    .where(eq(walletProfile.walletAddress, address))
    .limit(1);
  const currentStatus = profileRows[0]?.status ?? null;

  const existing = await dbClient
    .select({ status: walletApprovalRequest.status, resolvedAt: walletApprovalRequest.resolvedAt })
    .from(walletApprovalRequest)
    .where(
      and(
        eq(walletApprovalRequest.walletAddress, address),
        eq(walletApprovalRequest.requestedTier, args.requestedTier)
      )
    );

  if (!shouldQueueApprovalRequest(args.requestedTier, currentStatus, existing, now)) {
    return { queued: false };
  }

  const id = randomUUID();
  await dbClient.insert(walletApprovalRequest).values({
    id,
    walletAddress: address,
    requestedTier: args.requestedTier,
    source: args.source,
    category: args.category,
    scoreSnapshotJson: JSON.stringify(args.scoreSnapshot),
    reason: args.reason,
    status: "pending",
  });

  return { queued: true, id };
}
