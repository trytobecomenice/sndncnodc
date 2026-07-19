// The Orchestrator (Joey, 2026-07-20) — runs the full weather pipeline in the correct sequence:
// Ingest -> Prune -> Check Markets -> Order Builder -> Early Exit -> PnL Snapshot. This is the
// single script the launchd .plist invokes on an hourly tick.
//
// CADENCE-AWARE BY DESIGN (Joey's explicit call, 2026-07-20): a single hourly tick does not mean
// every step runs every hour. The Execution Cycle table (docs/weather/WEATHER_ARCHITECTURE.md §3)
// already documents different cadences per job — running ingestOpenMeteo.ts (a ~35,000-row
// ensemble fetch) or a 43-station discoverMarkets.ts/pruneHistorical.ts sweep every hour instead
// of their documented 3-6h / daily cadence would be real, avoidable waste against sources that
// don't update that often. A small JSON state file (data/orchestrator-state.json) tracks each
// gated step's last-run time; steps whose interval hasn't elapsed are skipped this tick. Always-
// run steps (ingestMetar, checkMarkets, orderBuilder, earlyExit, updatePnl) match their own
// already-documented hourly/1-2h cadence, so no gating is needed for those.
//
// ABSOLUTE PATHS THROUGHOUT (the "crucial engineering note"): launchd runs in a restricted
// environment — no login shell, no ~/.zshrc, a minimal PATH that does NOT include this machine's
// node install (`node` resolves to /Users/joeychan/.local/bin/node here, not a standard system
// path). Every path this script uses (node itself, tsx's real entry point, each sibling script,
// the state file) is resolved from either `process.execPath`/`import.meta.url` or joined against
// them — never from `process.cwd()` or a bare command name relying on PATH. This matters twice
// over: not just for how launchd invokes THIS file, but for how this file, in turn, spawns every
// pipeline step as its own subprocess — tsx's own `.bin/tsx` is a /bin/sh wrapper script that
// falls back to a PATH lookup for `node` when no node binary sits next to it (confirmed by
// reading it directly), which fails under launchd's PATH exactly the way a bare `node` command
// would. Bypassed by invoking `process.execPath` (this process's own actual node binary) directly
// against tsx's real `cli.mjs`, never through the wrapper. Mirrors the same defensive pattern
// packages/db/src/env.ts already uses for the database path, for the same underlying reason.
//
// EACH STEP RUNS AS A REAL SUBPROCESS (`tsx src/<script>.ts`, not an in-process function import)
// — deliberately: this is exactly what a human runs manually today, so there's no risk of subtly
// different behavior between "imported and awaited" and "run from the CLI," and a hung/crashed
// step can't take the orchestrator process down with it.

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PACKAGE_DIR = path.resolve(__dirname, ".."); // src -> packages/weather
const SRC_DIR = path.join(PACKAGE_DIR, "src");
// tsx's own .bin/tsx is a /bin/sh wrapper that falls back to a PATH lookup (`command -v node`)
// when no node binary sits next to it — under launchd's minimal PATH (no ~/.local/bin, no
// version-manager shims) that lookup fails. Bypassing it: `process.execPath` is the ABSOLUTE path
// of whatever node binary is actually running THIS process (self-referential, zero hardcoding,
// correct under any environment including launchd's) — used directly to invoke tsx's real
// cli.mjs entry point, never through the wrapper script, for every subprocess this file spawns.
const NODE_BIN = process.execPath;
const TSX_CLI = path.join(PACKAGE_DIR, "node_modules", "tsx", "dist", "cli.mjs");
const STATE_DIR = path.join(PACKAGE_DIR, "data");
const STATE_FILE = path.join(STATE_DIR, "orchestrator-state.json");

const ENSEMBLE_INTERVAL_MS = 4 * 60 * 60 * 1000; // 4h — midpoint of the documented 3-6h cadence
const DAILY_INTERVAL_MS = 24 * 60 * 60 * 1000;
const STEP_TIMEOUT_MS = 5 * 60 * 1000; // defensive outer bound per step, beyond each script's own internal network timeouts

interface OrchestratorState {
  lastRun: Record<string, number>;
}

function loadState(): OrchestratorState {
  if (!existsSync(STATE_FILE)) return { lastRun: {} };
  try {
    const parsed = JSON.parse(readFileSync(STATE_FILE, "utf-8"));
    return { lastRun: parsed?.lastRun ?? {} };
  } catch {
    console.warn(`runWeatherLoop: ${STATE_FILE} was unreadable/corrupt — starting with fresh cadence state.`);
    return { lastRun: {} };
  }
}

function saveState(state: OrchestratorState): void {
  mkdirSync(STATE_DIR, { recursive: true });
  writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

function isDue(stepKey: string, intervalMs: number, state: OrchestratorState): boolean {
  const last = state.lastRun[stepKey];
  return last === undefined || Date.now() - last >= intervalMs;
}

function markRun(stepKey: string, state: OrchestratorState): void {
  state.lastRun[stepKey] = Date.now();
  saveState(state);
}

interface StepResult {
  ok: boolean;
}

/** Runs one script as a real subprocess (`tsx src/<scriptFile>`), inheriting stdio so its output
 * lands directly in whatever log file launchd redirects THIS process's stdout/stderr to. */
function runStep(label: string, scriptFile: string): StepResult {
  const scriptPath = path.join(SRC_DIR, scriptFile);
  console.log(`\n--- [${new Date().toISOString()}] ${label} (${scriptFile}) ---`);
  const start = Date.now();

  const result = spawnSync(NODE_BIN, [TSX_CLI, scriptPath], {
    cwd: PACKAGE_DIR,
    stdio: "inherit",
    timeout: STEP_TIMEOUT_MS,
  });

  const durationMs = Date.now() - start;
  if (result.error) {
    console.error(`--- ${label} FAILED to launch after ${durationMs}ms: ${result.error.message} ---`);
    return { ok: false };
  }
  if (result.status !== 0) {
    console.error(`--- ${label} FAILED (exit ${result.status}) after ${durationMs}ms ---`);
    return { ok: false };
  }
  console.log(`--- ${label} OK (${durationMs}ms) ---`);
  return { ok: true };
}

async function main() {
  console.log(`runWeatherLoop: starting run at ${new Date().toISOString()}`);
  const state = loadState();

  // 1. INGEST — ingestMetar always (hourly cadence), discoverMarkets + ingestOpenMeteo only when
  // their own longer interval is due. discoverMarkets.ts wasn't in the originally-named sequence
  // but belongs here in spirit (keeps market mappings current for checkMarkets.ts below) — never
  // auto-activates a market (Rule 8's is_active always defaults false), so running it on a
  // schedule carries no trading risk, only keeps the review queue fresh.
  runStep("Ingest: METAR", "ingestMetar.ts");

  if (isDue("discoverMarkets", DAILY_INTERVAL_MS, state)) {
    const result = runStep("Ingest: Discover Markets (daily)", "discoverMarkets.ts");
    if (result.ok) markRun("discoverMarkets", state);
  } else {
    console.log("--- Ingest: Discover Markets — skipped, not due yet (daily cadence) ---");
  }

  let ranEnsembleIngest = false;
  if (isDue("ingestOpenMeteo", ENSEMBLE_INTERVAL_MS, state)) {
    const result = runStep("Ingest: Open-Meteo Ensemble (4h)", "ingestOpenMeteo.ts");
    if (result.ok) {
      markRun("ingestOpenMeteo", state);
      ranEnsembleIngest = true;
    }
  } else {
    console.log("--- Ingest: Open-Meteo Ensemble — skipped, not due yet (4h cadence) ---");
  }

  // 2. PRUNE — pruneForecasts only meaningful right after a fresh ensemble ingest; pruneHistorical
  // on its own daily cadence, independent of the ingest steps above.
  if (ranEnsembleIngest) {
    runStep("Prune: Forecasts", "pruneForecasts.ts");
  }
  if (isDue("pruneHistorical", DAILY_INTERVAL_MS, state)) {
    const result = runStep("Prune: Historical (daily)", "pruneHistorical.ts");
    if (result.ok) markRun("pruneHistorical", state);
  } else {
    console.log("--- Prune: Historical — skipped, not due yet (daily cadence) ---");
  }

  // 3. CHECK MARKETS — critical: everything downstream (Order Builder, Early Exit) needs the
  // fresh weather_probability_estimate rows this step produces. A failure here means those two
  // steps would only have stale data to act on, so they're skipped rather than run anyway.
  const checkMarketsResult = runStep("Check Markets", "checkMarkets.ts");

  if (checkMarketsResult.ok) {
    // 4. ORDER BUILDER
    runStep("Order Builder", "orderBuilder.ts");
    // 5. EARLY EXIT
    runStep("Early Exit", "earlyExit.ts");
  } else {
    console.error("--- Check Markets failed — skipping Order Builder and Early Exit this run (stale data risk) ---");
  }

  // 6. PNL SNAPSHOT — always attempted, even if Check Markets failed above: it marks open
  // positions to market using whatever odds data already exists and rolls up realized PnL from
  // closed positions, neither of which strictly depends on this tick's checkMarkets run.
  runStep("PnL Snapshot", "updatePnl.ts");

  console.log(`\nrunWeatherLoop: run complete at ${new Date().toISOString()}`);
  if (!checkMarketsResult.ok) {
    process.exit(1); // signal a real failure to launchd/log monitoring, even though PnL still ran
  }
}

main().catch((err) => {
  console.error("runWeatherLoop: unexpected top-level failure:", err);
  process.exit(1);
});
