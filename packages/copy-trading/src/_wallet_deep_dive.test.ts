// Unit tests for _wallet_deep_dive.ts's strategy-tier classification — a
// deliberate exception to this repo's "underscore files aren't tested"
// convention, since classifyStrategyTiers() is pure, trivially testable,
// and worth getting right independent of the rest of the script's
// network-bound body (see the strategy-tiered-discovery plan).

import { describe, expect, it } from "vitest";
import { classifyStrategyTiers, type StrategyTierInput } from "./_wallet_deep_dive";

function input(overrides: Partial<StrategyTierInput>): StrategyTierInput {
  return {
    category: "politics",
    winRate: 0.7,
    tradeCount: 50,
    avgPnlUsd: 10,
    avgEntryPrice: 0.5,
    entryPriceCv: 0.5,
    categoryCount: 1,
    top3ConcentrationPct: 0.5,
    ...overrides,
  };
}

describe("classifyStrategyTiers", () => {
  it("classifies a Political Macro Whale: politics, big avg_pnl, infrequent", () => {
    const tiers = classifyStrategyTiers(input({ category: "politics", avgPnlUsd: 100, tradeCount: 40 }));
    expect(tiers).toContain("Political Macro Whale");
  });

  it("does not classify a politics wallet with small avg_pnl as a Whale", () => {
    const tiers = classifyStrategyTiers(input({ category: "politics", avgPnlUsd: 10, tradeCount: 40 }));
    expect(tiers).not.toContain("Political Macro Whale");
  });

  it("does not classify a politics wallet with high trade count as a Whale (too frequent for 'high conviction')", () => {
    const tiers = classifyStrategyTiers(input({ category: "politics", avgPnlUsd: 100, tradeCount: 150 }));
    expect(tiers).not.toContain("Political Macro Whale");
  });

  it("classifies a Sports Scalper: sports, >100 trades, small avg_pnl", () => {
    const tiers = classifyStrategyTiers(input({ category: "sports", tradeCount: 150, avgPnlUsd: 5 }));
    expect(tiers).toContain("Sports High-Frequency Scalper");
  });

  it("does not classify a sports wallet with <=100 trades as a Scalper", () => {
    const tiers = classifyStrategyTiers(input({ category: "sports", tradeCount: 100, avgPnlUsd: 5 }));
    expect(tiers).not.toContain("Sports High-Frequency Scalper");
  });

  it("does not classify a high-avg_pnl sports wallet as a Scalper even with many trades", () => {
    const tiers = classifyStrategyTiers(input({ category: "sports", tradeCount: 150, avgPnlUsd: 50 }));
    expect(tiers).not.toContain("Sports High-Frequency Scalper");
  });

  it("labels any crypto candidate as Crypto Specialist, honestly caveated", () => {
    const tiers = classifyStrategyTiers(input({ category: "crypto" }));
    expect(tiers.some((t) => t.startsWith("Crypto Specialist"))).toBe(true);
    expect(tiers.find((t) => t.startsWith("Crypto Specialist"))).toContain("NOT verified");
  });

  it("classifies a Cross-Category Generalist: 3+ categories, low concentration", () => {
    const tiers = classifyStrategyTiers(input({ categoryCount: 3, top3ConcentrationPct: 0.1 }));
    expect(tiers).toContain("Cross-Category Quant Generalist");
  });

  it("does not classify a 2-category wallet as a Generalist", () => {
    const tiers = classifyStrategyTiers(input({ categoryCount: 2, top3ConcentrationPct: 0.1 }));
    expect(tiers).not.toContain("Cross-Category Quant Generalist");
  });

  it("does not classify a 3-category but concentrated wallet as a Generalist", () => {
    const tiers = classifyStrategyTiers(input({ categoryCount: 3, top3ConcentrationPct: 0.5 }));
    expect(tiers).not.toContain("Cross-Category Quant Generalist");
  });

  it("classifies a Tail-End Yield Farmer: high win rate, high entry price, real CV", () => {
    const tiers = classifyStrategyTiers(input({ winRate: 0.95, avgEntryPrice: 0.9, entryPriceCv: 0.3 }));
    expect(tiers).toContain("Tail-End Yield Farmer");
  });

  it("rejects a Yield-Farmer-shaped candidate with suspiciously uniform entry prices (the Rule-27-mirror safeguard)", () => {
    // Same win-rate/price-range signature as a real Yield Farmer, but CV is
    // low — the exact 0xc21ea96b failure mode mirrored at the high end of
    // the price spectrum instead of the low end.
    const tiers = classifyStrategyTiers(input({ winRate: 1.0, avgEntryPrice: 0.9, entryPriceCv: 0.05 }));
    expect(tiers).not.toContain("Tail-End Yield Farmer");
  });

  it("rejects a Yield-Farmer candidate with a null CV (insufficient data, never guessed)", () => {
    const tiers = classifyStrategyTiers(input({ winRate: 0.95, avgEntryPrice: 0.9, entryPriceCv: null }));
    expect(tiers).not.toContain("Tail-End Yield Farmer");
  });

  it("rejects a Yield-Farmer candidate with a null avg_entry_price", () => {
    const tiers = classifyStrategyTiers(input({ winRate: 0.95, avgEntryPrice: null, entryPriceCv: 0.3 }));
    expect(tiers).not.toContain("Tail-End Yield Farmer");
  });

  it("classifies any pop-culture candidate as a Pop-Culture Specialist", () => {
    const tiers = classifyStrategyTiers(input({ category: "pop-culture" }));
    expect(tiers).toContain("Pop-Culture Specialist");
  });

  it("returns multiple tiers when a candidate genuinely matches more than one", () => {
    // A politics whale that's ALSO a 3+-category generalist.
    const tiers = classifyStrategyTiers(
      input({ category: "politics", avgPnlUsd: 100, tradeCount: 40, categoryCount: 3, top3ConcentrationPct: 0.1 })
    );
    expect(tiers).toContain("Political Macro Whale");
    expect(tiers).toContain("Cross-Category Quant Generalist");
  });

  it("returns an empty array when nothing matches any tier", () => {
    const tiers = classifyStrategyTiers(
      input({
        category: "other", avgPnlUsd: 5, tradeCount: 10, winRate: 0.6,
        avgEntryPrice: 0.5, entryPriceCv: 0.5, categoryCount: 1, top3ConcentrationPct: 0.9,
      })
    );
    expect(tiers).toEqual([]);
  });
});
