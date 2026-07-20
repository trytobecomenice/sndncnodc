// pnpm --filter @copybot/weather settle:positions
//
// The Settlement / Resolution Engine (Joey, 2026-07-20) — closes the last real gap in the
// position lifecycle: a position held to expiration (not exited early by earlyExit.ts) needs its
// final PnL realized once Polymarket actually resolves the market, or it sits "open" forever and
// the equity curve (Rule 17) stays permanently incomplete for exactly the trades we most need
// real outcomes from.
//
// SETTLEMENT SOURCE: Polymarket's own on-chain resolution (`bullpen polymarket event <slug>`),
// NOT Wunderground — confirmed correct via AskUserQuestion (2026-07-20), not assumed. Our paper
// position's real-world equivalent payout is determined by whatever Polymarket actually resolved
// to, not by an independently-read weather station value; Rule 5's Wunderground-authority
// requirement is about a DIFFERENT, still-deferred job (verifySettlement.ts's pre-resolution risk
// check), not this one. Verified live against 3 real cases before writing any settlement logic —
// a resolved-No market, a resolved-Yes market, and a still-open market: `closed: true` plus a
// clean {0, 1} pair in `outcomeTokens[].price` is exactly and only what a genuinely resolved
// market returns; a still-open market's prices are fractional (e.g. 0.255/0.745), never 0/1.
//
// FAILS CLOSED (same principle as Rule 6): a market that's `closed: true` but whose outcome
// prices are NOT a clean {0, 1} pair (e.g. mid-dispute) is left open and flagged, never
// force-settled on an ambiguous read. A single unreachable market (network error, unknown slug)
// is skipped, not treated as a reason to abort the whole pass over every other open position.

import { runBullpenJson } from "@copybot/bullpen-client";
import { closePaperTradeOrder, fetchOpenPositions } from "./db/writers";
import { computeRealizedPnl } from "./exitSignals";

interface EventOutcomeToken {
  outcome: string;
  price: string;
}

interface EventResolution {
  closed: boolean;
  outcomeTokens: EventOutcomeToken[];
}

async function fetchEventResolution(marketSlug: string): Promise<EventResolution | null> {
  try {
    const response = await runBullpenJson(["polymarket", "event", marketSlug], { retries: 2, retryDelayMs: 500 });
    return { closed: Boolean(response?.closed), outcomeTokens: response?.outcomeTokens ?? [] };
  } catch {
    return null;
  }
}

/** A market is only trusted as genuinely resolved if it's closed AND its outcome prices form a
 * clean {0, 1} pair — anything else (mid-dispute, a closed-but-not-cleanly-priced edge case) is
 * treated as not-yet-settleable, never guessed at. */
function resolveOutcomePrice(resolution: EventResolution, heldOutcome: "Yes" | "No"): number | null {
  if (!resolution.closed) return null;
  const token = resolution.outcomeTokens.find((t) => t.outcome === heldOutcome);
  if (!token) return null;
  const price = Number(token.price);
  if (price !== 0 && price !== 1) return null;
  return price;
}

async function main() {
  const positions = await fetchOpenPositions();
  console.log(`settlePositions: ${positions.length} open position(s) to check.\n`);
  if (positions.length === 0) {
    console.log("No open positions — nothing to do.");
    return;
  }

  let settled = 0;
  let stillOpen = 0;
  let flaggedAmbiguous = 0;
  let fetchFailed = 0;

  for (const position of positions) {
    const resolution = await fetchEventResolution(position.marketSlug);
    if (!resolution) {
      console.log(`SKIP ${position.marketSlug}: could not fetch event status (network/unknown slug).`);
      fetchFailed++;
      continue;
    }

    if (!resolution.closed) {
      stillOpen++;
      continue; // the normal case on most ticks — not worth logging every time
    }

    const exitPrice = resolveOutcomePrice(resolution, position.outcome);
    if (exitPrice === null) {
      console.log(`FLAG ${position.marketSlug}: closed but outcome prices are not a clean settled pair — leaving open for manual review.`);
      flaggedAmbiguous++;
      continue;
    }

    const realizedPnlUsd = computeRealizedPnl(position.entryPrice, exitPrice, position.ourShares);
    await closePaperTradeOrder(position.id, exitPrice, realizedPnlUsd, "settled");
    settled++;
    console.log(
      `SETTLE ${position.marketSlug}: held ${position.outcome}, resolved ${exitPrice === 1 ? "WIN" : "LOSS"} ` +
        `(exit=${exitPrice.toFixed(2)}) — realizedPnl=$${realizedPnlUsd.toFixed(2)}.`
    );
  }

  console.log("\n=== Summary ===");
  console.log(`Open positions checked:   ${positions.length}`);
  console.log(`Settled (resolved):       ${settled}`);
  console.log(`Still open (unresolved):  ${stillOpen}`);
  console.log(`Flagged (ambiguous):      ${flaggedAmbiguous}`);
  console.log(`Fetch failed:             ${fetchFailed}`);
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  main().catch((err) => {
    console.error("settle:positions failed:", err);
    process.exit(1);
  });
}
