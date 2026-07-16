// Seeds a handful of clearly-labeled demo rows (isDemoData: true) so a fresh
// install has something to render in the dashboard before any real scan has
// run. Per the Hermes spec: "Demo or seed data is allowed only if clearly
// labeled as demo data." Real scans overwrite nothing here — demo rows just
// sit alongside real ones, distinguished by the isDemoData flag.
import { db } from "./client";
import { walletProfile, paperTrade } from "./schema";

async function main() {
  const demoWallet = "0xDEMO0000000000000000000000000000000001";

  await db
    .insert(walletProfile)
    .values({
      walletAddress: demoWallet,
      nickname: "demo-wallet (seed data)",
      status: "watch",
      compositeScore: 0.5,
      category: "demo",
      isDemoData: true,
      notes: "Seed row inserted by packages/db/src/seed.ts — not a real tracked wallet.",
    })
    .onConflictDoNothing();

  await db.insert(paperTrade).values({
    strategy: "bot_filtered",
    walletAddress: demoWallet,
    marketSlug: "demo-market-seed-row",
    marketTitle: "Demo seed paper trade (not real)",
    outcome: "Yes",
    ourSizeUsd: 10,
    ourShares: 20,
    avgEntryPrice: 0.5,
    status: "open",
    isDemoData: true,
  });

  console.log("Seed complete (demo rows only, isDemoData=true).");
}

main().catch((err) => {
  console.error("Seed failed:", err);
  process.exit(1);
});
