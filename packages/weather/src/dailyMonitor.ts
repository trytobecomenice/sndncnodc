// pnpm --filter @copybot/weather monitor:daily
//
// The Daily Telemetry Protocol (Joey, 2026-07-20) — read-only forward-test dashboard. Prints
// scheduler health, portfolio equity, and open-position status in one glance.
//
// STRICTLY READ-ONLY, BY DESIGN: every query here is a SELECT against tables the orchestrator
// already writes on its own schedule. No `bullpen` calls, no INSERT/UPDATE/DELETE anywhere in
// this file — this is the "safe way to monitor without disrupting the loop" Joey asked for. Safe
// to run at any moment, including mid-loop-execution: the shared SQLite connection runs in WAL
// mode (set up in packages/db/src/migrate.ts), so a concurrent read never blocks or is blocked by
// the orchestrator's own writes.
//
// SCHEDULER HEALTH uses THREE independent signals, not one, because a single timestamp can lie
// about what's actually healthy:
//   1. Last PnL Snapshot — updatePnl.ts runs LAST and ALWAYS (even if Check Markets failed), so
//      this tells you the orchestrator process itself is alive and completing runs.
//   2. Last Probability Estimate — checkMarkets.ts is the pipeline's CRITICAL step (Rule 18); if
//      this timestamp is meaningfully older than the PnL Snapshot timestamp, the orchestrator is
//      "alive" but Check Markets has been silently failing every tick — the PnL Snapshot alone
//      would never reveal that, since it always runs regardless.
//   3. Orchestrator log file mtime — the crudest, cheapest signal (just a filesystem stat, no
//      parsing): if this hasn't moved at all, launchd itself likely isn't ticking.

import { desc, eq, gte } from "drizzle-orm";
import { db, weatherPnlSnapshot, weatherPosition, weatherProbabilityEstimate } from "@copybot/db";
import { existsSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PACKAGE_DIR = path.resolve(__dirname, "..");
const LOOP_LOG_PATH = path.join(PACKAGE_DIR, "data", "weather-loop.log");

// Hourly cadence (Rule 18) + generous buffer for a slow tick — anything older than this and the
// scheduler is very likely dead or stuck, not just running a bit behind schedule.
const STALE_ALERT_MINUTES = 90;

function minutesAgo(date: Date): number {
  return (Date.now() - date.getTime()) / 60_000;
}

function fmtAgo(minutes: number): string {
  if (minutes < 60) return `${minutes.toFixed(0)}m ago`;
  return `${(minutes / 60).toFixed(1)}h ago`;
}

function fmtUsd(n: number): string {
  const sign = n >= 0 ? "+" : "-";
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

async function printSchedulerHealth() {
  console.log("=== Scheduler Health ===");

  const [latestSnapshot] = await db
    .select({ capturedAt: weatherPnlSnapshot.capturedAt })
    .from(weatherPnlSnapshot)
    .orderBy(desc(weatherPnlSnapshot.capturedAt))
    .limit(1);

  const [latestEstimate] = await db
    .select({ estimatedAt: weatherProbabilityEstimate.estimatedAt })
    .from(weatherProbabilityEstimate)
    .orderBy(desc(weatherProbabilityEstimate.estimatedAt))
    .limit(1);

  if (latestSnapshot) {
    const ageMin = minutesAgo(latestSnapshot.capturedAt);
    const alert = ageMin > STALE_ALERT_MINUTES ? "  *** ALERT: no PnL Snapshot in over 90 minutes — scheduler may be dead ***" : "";
    console.log(`Last PnL Snapshot:          ${latestSnapshot.capturedAt.toISOString()} (${fmtAgo(ageMin)})${alert}`);
  } else {
    console.log("Last PnL Snapshot:          none recorded yet.");
  }

  if (latestEstimate) {
    const ageMin = minutesAgo(latestEstimate.estimatedAt);
    let note = "";
    if (latestSnapshot) {
      const gapMin = minutesAgo(latestEstimate.estimatedAt) - minutesAgo(latestSnapshot.capturedAt);
      if (gapMin > STALE_ALERT_MINUTES) {
        note = "  *** ALERT: Check Markets is meaningfully older than PnL Snapshot — likely failing silently every tick ***";
      }
    }
    console.log(`Last Probability Estimate:  ${latestEstimate.estimatedAt.toISOString()} (${fmtAgo(ageMin)})${note}`);
  } else {
    console.log("Last Probability Estimate:  none recorded yet.");
  }

  if (existsSync(LOOP_LOG_PATH)) {
    const mtime = statSync(LOOP_LOG_PATH).mtime;
    console.log(`Orchestrator log last write: ${mtime.toISOString()} (${fmtAgo(minutesAgo(mtime))})`);
  } else {
    console.log(`Orchestrator log:            not found at ${LOOP_LOG_PATH} (scheduler never run, or not yet activated).`);
  }
  console.log("");
}

async function printEquity() {
  console.log("=== Equity ===");

  const [latest] = await db.select().from(weatherPnlSnapshot).orderBy(desc(weatherPnlSnapshot.capturedAt)).limit(1);
  if (!latest) {
    console.log("No PnL snapshot recorded yet — run update:pnl or the full loop at least once.\n");
    return;
  }

  const dayAgo = new Date(Date.now() - 24 * 60 * 60 * 1000);
  const recentClosed = await db
    .select({ realizedPnlUsd: weatherPosition.realizedPnlUsd })
    .from(weatherPosition)
    .where(gte(weatherPosition.closedAt, dayAgo));
  const last24hPnl = recentClosed.reduce((sum, r) => sum + (r.realizedPnlUsd ?? 0), 0);

  console.log(`Total Equity:            $${(latest.totalEquityUsd ?? 0).toFixed(2)}`);
  console.log(`Available Cash:          $${(latest.availableCashUsd ?? 0).toFixed(2)}`);
  console.log(`Unrealized PnL (open):   ${fmtUsd(latest.unrealizedPnlUsd)}`);
  console.log(`Realized PnL (all-time): ${fmtUsd(latest.realizedPnlUsd)}`);
  console.log(`Realized PnL (last 24h): ${fmtUsd(last24hPnl)} (${recentClosed.length} position(s) closed)`);
  console.log(`Win rate (all-time):     ${latest.winRate !== null ? (latest.winRate * 100).toFixed(1) + "%" : "n/a (no closes yet)"}`);
  console.log(`Open positions:          ${latest.openPositionsCount}`);
  console.log(`(as of snapshot ${latest.capturedAt.toISOString()})\n`);
}

async function printOpenPositions() {
  console.log("=== Open Positions ===");

  const positions = await db.select().from(weatherPosition).where(eq(weatherPosition.status, "open"));
  if (positions.length === 0) {
    console.log("(none)\n");
    return;
  }

  for (const position of positions) {
    const [estimate] = await db
      .select({ edge: weatherProbabilityEstimate.edge, estimatedAt: weatherProbabilityEstimate.estimatedAt })
      .from(weatherProbabilityEstimate)
      .where(eq(weatherProbabilityEstimate.marketSlug, position.marketSlug))
      .orderBy(desc(weatherProbabilityEstimate.estimatedAt))
      .limit(1);

    let edgeLabel = "no estimate yet";
    if (estimate && estimate.edge !== null) {
      // Re-sign to the position's own held side (Yes-side edge as stored -> flip for a No position).
      const sideEdge = position.outcome === "Yes" ? estimate.edge : -estimate.edge;
      edgeLabel = `${sideEdge >= 0 ? "+" : ""}${(sideEdge * 100).toFixed(1)}pp (as of ${fmtAgo(minutesAgo(estimate.estimatedAt))})`;
    }

    console.log(`${position.marketSlug}`);
    console.log(
      `  Side: ${position.outcome.padEnd(4)} Size: $${position.ourSizeUsd.toFixed(2).padEnd(9)} ` +
        `Entry: ${position.entryPrice.toFixed(3)}  Current edge: ${edgeLabel}`
    );
  }
  console.log("");
}

async function main() {
  console.log(`\nWeather Bot — Forward Test Daily Monitor`);
  console.log(`Generated: ${new Date().toISOString()}\n`);

  await printSchedulerHealth();
  await printEquity();
  await printOpenPositions();
}

main().catch((err) => {
  console.error("dailyMonitor failed:", err);
  process.exit(1);
});
