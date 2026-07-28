// pnpm reconcile:position-tracker — the "ground truth" check from the
// Build vs. Borrow architecture discussion (2026-07-27): before
// PositionTracker's from-scratch fee/position math is trusted for ANY
// new wallet, it must first agree with bullpen's own numbers for the
// wallets we already know well — our own currently-tracked ones.
//
// GROUND TRUTH SOURCE: bot_risk_state.tracked_traders — the same
// always-current, DB-published list bot.py itself writes at every
// startup (used by the dashboard for the same reason: it's the real
// answer to "what's actually being copied right now," not a guess).
//
// WINDOWED, NOT LIFETIME (2026-07-27, fixed after the first real run):
// the first version of this script fetched each wallet's FULL history
// (capped at fetchWalletTrades' MAX_PAGES*DEFAULT_LIMIT = 5000 trades)
// and compared against bullpen's lifetime_pnl. 14 of 17 wallets hit that
// 5000-trade cap and showed huge diffs (60-120%, two even sign-flipped)
// — but the 3 wallets whose FULL history fit under the cap all landed
// within ~10%, cleanly explained by open positions being excluded. That
// pattern makes the root cause obvious: comparing a truncated RECENT
// slice against a TRUE LIFETIME number was never going to agree,
// regardless of whether the underlying math is correct. It's also the
// wrong comparison on principle — this whole architecture (Rule 41, the
// rolling-window consistency/win-rate scoring in scoreWallets.ts) already
// committed to a bounded rolling window, not lifetime reconstruction.
// Fixed by bounding BOTH sides to the same window: `fetchWalletTrades` is
// seeded with `startEpochSeconds` = now - RECONCILIATION_WINDOW_DAYS, and
// the comparison target switches to `wallet_profile.pnl30d` (a direct,
// unmodified pass-through of bullpen's wallet-stats `pnl_30d` — same
// provenance as `pnlAllTime` was, just the windowed sibling field).
//
// REMAINING CAVEAT, HONESTLY STATED: a position OPENED before the window
// start but CLOSED inside it is invisible to our windowed fetch (the BUY
// that opened it never gets applied — see positionTracker.ts's applyTrade,
// "SELL with no tracked open position is a no-op"). That close's PnL is
// silently excluded from our number, not fabricated — but it means our
// windowed PnL can still UNDER-count relative to bullpen's pnl_30d for any
// wallet with boundary-straddling positions. This is a smaller, more
// bounded gap than the lifetime-vs-recent-slice mismatch it replaces, not
// a claim of an exact match — the report still prints open-position count
// so a human can judge whether a remaining gap fits this explanation.

import { eq } from "drizzle-orm";
import { botRiskState, db, walletProfile } from "@copybot/db";
import { computeWalletMetrics, newWalletPositionState, updateWalletState } from "./positionTracker";

const RECONCILIATION_WINDOW_DAYS = 30; // matches the one bullpen field with an exact
// same-window counterpart (pnl_30d) — the OTHER rolling window already used elsewhere
// in this codebase (90d, scoreWallets.ts's rollingWindowDays) has no equivalent
// wallet_profile field to diff against, so 30d was picked for a clean apples-to-apples
// comparison, not because it's inherently the "right" window for anything else.
const PNL_DIFF_FLAG_THRESHOLD_PCT = 10; // a judgment call, not a verified-safe number --
// see the module doc comment above: this just decides which rows get a
// visual flag in the printed report, it never fails the run or blocks
// anything. A real mismatch needs a human reading the OPEN POSITION COUNT
// column anyway, not a hardcoded pass/fail bar.

async function getTrackedWallets(): Promise<Record<string, string>> {
  const rows = await db.select().from(botRiskState).where(eq(botRiskState.key, "tracked_traders")).limit(1);
  if (rows.length === 0) {
    throw new Error(
      "bot_risk_state.tracked_traders not found — bot.py must have run at least once " +
        "(it publishes this at every startup) before this script has a ground-truth wallet list to check against."
    );
  }
  return JSON.parse(rows[0].valueJson) as Record<string, string>; // {address_lower: nickname}
}

async function main() {
  console.log(
    `reconcile:position-tracker — validating PositionTracker's math against bullpen's own ` +
      `${RECONCILIATION_WINDOW_DAYS}-day PnL...`
  );

  const tracked = await getTrackedWallets();
  const addresses = Object.keys(tracked);
  console.log(`Found ${addresses.length} currently-tracked wallet(s) to reconcile against.\n`);

  const windowStartEpochSeconds = Math.floor(Date.now() / 1000) - RECONCILIATION_WINDOW_DAYS * 86400;

  const rows: Array<{
    nickname: string;
    address: string;
    ourPnl: number;
    bullpenPnl30d: number | null;
    diffPct: number | null;
    openPositions: number;
    closedPositions: number;
    delisted: number;
  }> = [];

  for (const address of addresses) {
    const nickname = tracked[address];
    process.stdout.write(`  scoring ${nickname} (${address})...`);

    const profile = await db.select().from(walletProfile).where(eq(walletProfile.walletAddress, address)).limit(1);
    const bullpenPnl30d = profile[0]?.pnl30d ?? null;

    const state = newWalletPositionState(address);
    state.lastFetchedAt = windowStartEpochSeconds; // bounds fetchWalletTrades' startEpochSeconds
    try {
      await updateWalletState(state);
    } catch (err) {
      console.log(` FAILED: ${(err as Error).message}`);
      continue;
    }

    const metrics = computeWalletMetrics(state);
    const diffPct =
      bullpenPnl30d !== null && bullpenPnl30d !== 0
        ? Math.abs((metrics.totalRealizedPnlUsd - bullpenPnl30d) / bullpenPnl30d) * 100
        : null;

    rows.push({
      nickname,
      address,
      ourPnl: metrics.totalRealizedPnlUsd,
      bullpenPnl30d,
      diffPct,
      openPositions: metrics.openPositionCount,
      closedPositions: metrics.closedCount,
      delisted: metrics.delistedCount,
    });
    console.log(` done (${metrics.closedCount} closed, ${metrics.openPositionCount} open, ${metrics.delistedCount} delisted)`);
  }

  console.log(`\n=== Reconciliation report (${RECONCILIATION_WINDOW_DAYS}-day window) ===\n`);
  for (const r of rows) {
    const flag = r.diffPct !== null && r.diffPct > PNL_DIFF_FLAG_THRESHOLD_PCT ? "  <-- CHECK THIS ONE" : "";
    console.log(`${r.nickname} (${r.address})`);
    console.log(
      `  our realized PnL (closed only, ${RECONCILIATION_WINDOW_DAYS}d window): $${r.ourPnl.toFixed(2)}  |  bullpen pnl_30d: ` +
        `${r.bullpenPnl30d !== null ? "$" + r.bullpenPnl30d.toFixed(2) : "unknown"}` +
        `${r.diffPct !== null ? `  |  diff: ${r.diffPct.toFixed(1)}%${flag}` : ""}`
    );
    console.log(
      `  ${r.closedPositions} closed, ${r.openPositions} still open (excluded from PnL above — not a bug, see doc comment), ` +
        `${r.delisted} delisted (excluded entirely)`
    );
    console.log("");
  }

  const flaggedCount = rows.filter((r) => r.diffPct !== null && r.diffPct > PNL_DIFF_FLAG_THRESHOLD_PCT).length;
  console.log(
    `Done. ${rows.length} wallet(s) reconciled, ${flaggedCount} flagged for manual review ` +
      `(diff > ${PNL_DIFF_FLAG_THRESHOLD_PCT}%). This script only reports — it writes nothing to any table.`
  );
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("reconcile:position-tracker failed:", err);
    process.exit(1);
  });
}
