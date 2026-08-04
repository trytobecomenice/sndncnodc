// TS twin of bullpen_client.py (repo root). Mirrors its exact subprocess
// semantics — exit-code-aware error extraction, single-shot-by-default
// retries, timeout-as-unknown-fill-state — so the new TS research/scoring
// layer (packages/copy-trading) shells out to the same
// `bullpen` CLI the same way bot.py does. Keep the two in sync; this file
// has no logic of its own beyond what bullpen_client.py already documents.

import { execFile } from "node:child_process";

const DEFAULT_TIMEOUT_MS = 60_000;

export class BullpenTimeoutError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BullpenTimeoutError";
  }
}

export interface RunBullpenJsonOptions {
  // retries=1 means "try once, no retry" — MUST stay the default for any
  // call that can move funds (buy/sell). Only read-only calls should pass
  // retries>1: a retried buy/sell risks double-executing a trade that
  // actually filled but errored on the response leg.
  retries?: number;
  retryDelayMs?: number;
  timeoutMs?: number;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

interface ExecResult {
  stdout: string;
  stderr: string;
  exitCode: number;
  timedOut: boolean;
}

// Node's execFile callback error is typed as ExecFileException | null, which
// (unlike the narrower NodeJS.ErrnoException) does include `killed`/`signal`
// — this local type just documents the two fields we actually read from it.
interface ExecFileError {
  killed?: boolean;
  signal?: NodeJS.Signals | null;
  code?: number | string | null;
}

function execBullpen(args: string[], timeoutMs: number): Promise<ExecResult> {
  return new Promise((resolve) => {
    execFile(
      "bullpen",
      [...args, "--output", "json"],
      { timeout: timeoutMs, maxBuffer: 10 * 1024 * 1024 },
      (error: ExecFileError | null, stdout, stderr) => {
        if (error && error.killed && error.signal) {
          resolve({ stdout, stderr, exitCode: 1, timedOut: true });
          return;
        }
        const exitCode = error && typeof error.code === "number" ? error.code : error ? 1 : 0;
        resolve({ stdout, stderr, exitCode, timedOut: false });
      }
    );
  });
}

async function runBullpenJsonOnce(args: string[], timeoutMs: number): Promise<unknown> {
  const { stdout, stderr, exitCode, timedOut } = await execBullpen(args, timeoutMs);

  if (timedOut) {
    // The bullpen subprocess hit its timeout. For money-moving calls this is
    // fundamentally different from a clean failure: the order MAY have
    // executed on-chain even though we never saw the response — callers
    // must log it as unknown_fill_state for manual reconciliation, never
    // retry it.
    throw new BullpenTimeoutError(
      `bullpen ${args.join(" ")} timed out after ${timeoutMs}ms; if this was a trade, the order MAY still have executed`
    );
  }

  let data: any = null;
  if (stdout.trim()) {
    try {
      data = JSON.parse(stdout);
    } catch {
      data = null;
    }
  }

  // A trade command can exit non-zero (e.g. exit 4 "trade execution
  // failed") while still printing a JSON error body to stdout — check the
  // exit code independent of whether stdout parsed, or a rejected/reverted
  // order silently sails through as if it succeeded.
  if (exitCode !== 0) {
    let detail = stderr.trim();
    if (data && typeof data === "object") {
      detail = data.error || data.error_code || data.message || detail;
    }
    throw new Error(`bullpen ${args.join(" ")} exited ${exitCode}: ${detail || "no error detail"}`);
  }
  if (data === null) {
    throw new Error(`bullpen ${args.join(" ")} produced no parseable JSON output: ${JSON.stringify(stdout)}`);
  }
  if (data && typeof data === "object" && data.ok === false) {
    throw new Error(`bullpen ${args.join(" ")} error: ${data.error}`);
  }
  return data;
}

export async function runBullpenJson(
  args: string[],
  { retries = 1, retryDelayMs = 500, timeoutMs = DEFAULT_TIMEOUT_MS }: RunBullpenJsonOptions = {}
): Promise<any> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      return await runBullpenJsonOnce(args, timeoutMs);
    } catch (err) {
      lastError = err;
      if (attempt < retries) await sleep(retryDelayMs);
    }
  }
  throw lastError;
}

// Statuses `bullpen polymarket buy/sell --output json` can report. Only
// MATCHED means the CLI actually settled shares/cash on-chain — UNMATCHED
// (no counterparty), DELAYED, or a resting LIVE limit order are not fills.
// A 0 exit code plus parseable JSON only proves the CLI accepted the
// request, not that it filled.
export const FILLED_TRADE_STATUSES = new Set(["MATCHED"]);

export function requireFilled(response: any, actionDesc: string): any {
  const status = String(response?.status ?? "").toUpperCase();
  const txHashes: unknown[] = response?.transaction_hashes ?? [];
  if (!FILLED_TRADE_STATUSES.has(status) || txHashes.length === 0) {
    throw new Error(
      `${actionDesc} did not confirm an on-chain fill (status=${status || "missing"}, transaction_hashes=${JSON.stringify(txHashes)})`
    );
  }
  return response;
}

// Best-effort read of the ACTUAL average fill price from a buy/sell
// response. Tries the plausible field-name candidates and returns null if
// none is present.
export function extractFillPrice(response: any): number | null {
  for (const key of ["avg_price", "average_price", "fill_price", "executed_price", "price"]) {
    const value = response?.[key];
    if (typeof value === "number" && value > 0 && value <= 1) {
      return value;
    }
  }
  return null;
}
