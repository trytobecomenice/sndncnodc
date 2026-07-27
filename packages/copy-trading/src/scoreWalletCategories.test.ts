// Unit tests for scoreWalletCategories.ts's pure position-reconstruction and
// aggregation logic — the category/resolution lookups are injected as fakes
// so this exercises the actual PnL math without mocking three network layers.

import { describe, expect, it } from "vitest";
import { aggregateCategoryScores, reconstructRealizedCloses } from "./scoreWalletCategories";
import type { RawActivityRecord } from "./polymarketDataApi";

function trade(overrides: Partial<RawActivityRecord>): RawActivityRecord {
  return {
    proxyWallet: "0xabc",
    timestamp: 0,
    transactionHash: "0xhash",
    price: 0.5,
    asset: "111",
    size: 100,
    usdcSize: 50,
    side: "BUY",
    slug: "some-market",
    outcome: "Yes",
    ...overrides,
  };
}

const alwaysCrypto = async () => "crypto";
const alwaysUnresolved = async () => ({ closed: false, outcomes: [], outcomePrices: [] });
const fixedFeeRate = async () => 0.05;

describe("reconstructRealizedCloses", () => {
  it("computes exact realized PnL for a simple full buy-then-sell", async () => {
    const trades = [
      trade({ timestamp: 1, side: "BUY", size: 100, usdcSize: 40 }), // bought 100 shares for $40
      trade({ timestamp: 2, side: "SELL", size: 100, usdcSize: 60 }), // sold all 100 for $60
    ];
    const closes = await reconstructRealizedCloses(trades, alwaysCrypto, alwaysUnresolved, fixedFeeRate);
    expect(closes).toEqual([{ category: "crypto", pnlUsd: 20, costBasisUsd: 40, sharesClosed: 100, feeRate: 0.05 }]);
  });

  it("computes proportional PnL for a partial sell", async () => {
    const trades = [
      trade({ timestamp: 1, side: "BUY", size: 100, usdcSize: 40 }),
      trade({ timestamp: 2, side: "SELL", size: 50, usdcSize: 30 }), // sells half the shares
    ];
    const closes = await reconstructRealizedCloses(trades, alwaysCrypto, alwaysUnresolved, fixedFeeRate);
    // fractionSold = 50/100 = 0.5 -> cost basis closed = 40*0.5 = 20 -> pnl = 30-20 = 10
    expect(closes).toEqual([{ category: "crypto", pnlUsd: 10, costBasisUsd: 20, sharesClosed: 50, feeRate: 0.05 }]);
  });

  it("excludes a sell against a position with no prior buy in the window (pre-window bootstrap gap)", async () => {
    const trades = [trade({ timestamp: 1, side: "SELL", size: 50, usdcSize: 30 })];
    const closes = await reconstructRealizedCloses(trades, alwaysCrypto, alwaysUnresolved, fixedFeeRate);
    expect(closes).toEqual([]);
  });

  it("excludes a still-open position when the market has not resolved", async () => {
    const trades = [trade({ timestamp: 1, side: "BUY", size: 100, usdcSize: 40 })];
    const closes = await reconstructRealizedCloses(trades, alwaysCrypto, alwaysUnresolved, fixedFeeRate);
    expect(closes).toEqual([]);
  });

  it("marks a still-open position to its resolution payout when the market has resolved", async () => {
    const trades = [trade({ timestamp: 1, side: "BUY", size: 100, usdcSize: 40, outcome: "Yes" })];
    const resolvedYesWins = async () => ({ closed: true, outcomes: ["Yes", "No"], outcomePrices: [1, 0] });
    const closes = await reconstructRealizedCloses(trades, alwaysCrypto, resolvedYesWins, fixedFeeRate);
    // 100 shares * payout 1.0 = $100 payout, cost basis $40 -> pnl = 60
    expect(closes).toEqual([{ category: "crypto", pnlUsd: 60, costBasisUsd: 40, sharesClosed: 100, feeRate: 0.05 }]);
  });

  it("marks a still-open position to zero when it resolved against the held outcome", async () => {
    const trades = [trade({ timestamp: 1, side: "BUY", size: 100, usdcSize: 40, outcome: "Yes" })];
    const resolvedNoWins = async () => ({ closed: true, outcomes: ["Yes", "No"], outcomePrices: [0, 1] });
    const closes = await reconstructRealizedCloses(trades, alwaysCrypto, resolvedNoWins, fixedFeeRate);
    // 100 shares * payout 0.0 = $0 payout, cost basis $40 -> pnl = -40 (total loss)
    expect(closes).toEqual([{ category: "crypto", pnlUsd: -40, costBasisUsd: 40, sharesClosed: 100, feeRate: 0.05 }]);
  });

  it("processes trades in chronological order regardless of input order", async () => {
    // Deliberately out of order in the input array.
    const trades = [
      trade({ timestamp: 2, side: "SELL", size: 100, usdcSize: 60 }),
      trade({ timestamp: 1, side: "BUY", size: 100, usdcSize: 40 }),
    ];
    const closes = await reconstructRealizedCloses(trades, alwaysCrypto, alwaysUnresolved, fixedFeeRate);
    expect(closes).toEqual([{ category: "crypto", pnlUsd: 20, costBasisUsd: 40, sharesClosed: 100, feeRate: 0.05 }]);
  });

  it("tracks separate markets/outcomes independently", async () => {
    const trades = [
      trade({ timestamp: 1, side: "BUY", size: 100, usdcSize: 40, slug: "market-a", outcome: "Yes" }),
      trade({ timestamp: 2, side: "BUY", size: 50, usdcSize: 30, slug: "market-b", outcome: "No" }),
      trade({ timestamp: 3, side: "SELL", size: 100, usdcSize: 60, slug: "market-a", outcome: "Yes" }),
    ];
    const closes = await reconstructRealizedCloses(trades, alwaysCrypto, alwaysUnresolved, fixedFeeRate);
    // market-b position is still open and unresolved -> excluded; only market-a's close counts
    expect(closes).toEqual([{ category: "crypto", pnlUsd: 20, costBasisUsd: 40, sharesClosed: 100, feeRate: 0.05 }]);
  });

  it("clamps an oversized sell (more shares than currently held) to a full close", async () => {
    const trades = [
      trade({ timestamp: 1, side: "BUY", size: 100, usdcSize: 40 }),
      trade({ timestamp: 2, side: "SELL", size: 150, usdcSize: 90 }), // selling more than held
    ];
    const closes = await reconstructRealizedCloses(trades, alwaysCrypto, alwaysUnresolved, fixedFeeRate);
    // fractionSold clamped to 1.0 -> full cost basis ($40) closed, full proceeds ($90) counted
    expect(closes).toEqual([{ category: "crypto", pnlUsd: 50, costBasisUsd: 40, sharesClosed: 100, feeRate: 0.05 }]);
  });

  it("resolves category once per distinct market, not once per trade", async () => {
    let callCount = 0;
    const countingCategoryOf = async () => {
      callCount += 1;
      return "crypto";
    };
    const trades = [
      trade({ timestamp: 1, side: "BUY", size: 100, usdcSize: 40 }),
      trade({ timestamp: 2, side: "SELL", size: 50, usdcSize: 30 }),
      trade({ timestamp: 3, side: "SELL", size: 50, usdcSize: 30 }),
    ];
    await reconstructRealizedCloses(trades, countingCategoryOf, alwaysUnresolved, fixedFeeRate);
    expect(callCount).toBe(1);
  });

  it("resolves fee rate once per distinct market, not once per trade", async () => {
    let callCount = 0;
    const countingFeeRateOf = async () => {
      callCount += 1;
      return 0.05;
    };
    const trades = [
      trade({ timestamp: 1, side: "BUY", size: 100, usdcSize: 40 }),
      trade({ timestamp: 2, side: "SELL", size: 50, usdcSize: 30 }),
      trade({ timestamp: 3, side: "SELL", size: 50, usdcSize: 30 }),
    ];
    await reconstructRealizedCloses(trades, alwaysCrypto, alwaysUnresolved, countingFeeRateOf);
    expect(callCount).toBe(1);
  });
});

function close(overrides: Partial<Parameters<typeof aggregateCategoryScores>[0][number]>) {
  return { category: "crypto", pnlUsd: 10, costBasisUsd: 40, sharesClosed: 80, feeRate: 0.05, ...overrides };
}

describe("aggregateCategoryScores", () => {
  it("omits a category with fewer closes than the minimum sample gate", () => {
    const closes = [close({}), close({})]; // only 2, below DEFAULT_RULES.hardMinTrades (5)
    expect(aggregateCategoryScores(closes)).toEqual({});
  });

  it("computes win_rate, avg_pnl_usd, and a score once the sample gate is met", () => {
    const closes = [
      close({ pnlUsd: 10 }),
      close({ pnlUsd: 10 }),
      close({ pnlUsd: 10 }),
      close({ pnlUsd: -5 }),
      close({ pnlUsd: -5 }),
    ]; // 5 closes: 3 wins, 2 losses
    const result = aggregateCategoryScores(closes);
    expect(result.crypto.trade_count).toBe(5);
    expect(result.crypto.win_rate).toBeCloseTo(0.6);
    expect(result.crypto.avg_pnl_usd).toBeCloseTo((10 + 10 + 10 - 5 - 5) / 5);
    expect(result.crypto.score).toBeGreaterThan(0);
    expect(result.crypto.score).toBeLessThanOrEqual(1);
  });

  it("keeps categories independent of each other", () => {
    const fiveCryptoWins = Array.from({ length: 5 }, () => close({ pnlUsd: 10 }));
    const twoSportsLosses = [close({ category: "sports", pnlUsd: -5 }), close({ category: "sports", pnlUsd: -5 })];
    const result = aggregateCategoryScores([...fiveCryptoWins, ...twoSportsLosses]);
    expect(result.crypto).toBeDefined();
    expect(result.sports).toBeUndefined(); // below the sample gate
  });

  it("a losing category still gets a score, just a low one, not omitted", () => {
    const closes = Array.from({ length: 5 }, () => close({ category: "sports", pnlUsd: -10 }));
    const result = aggregateCategoryScores(closes);
    expect(result.sports).toBeDefined();
    expect(result.sports.score).toBeLessThan(0.5);
  });

  it("computes roi as total PnL over total cost basis", () => {
    const closes = Array.from({ length: 5 }, () => close({ pnlUsd: 10, costBasisUsd: 40 }));
    const result = aggregateCategoryScores(closes);
    // 5 closes * $10 pnl / 5 closes * $40 cost basis = 50/200 = 0.25
    expect(result.crypto.roi).toBeCloseTo(0.25);
  });

  it("computes avg_entry_price as cost-basis-weighted price across closes", () => {
    const closes = [
      close({ costBasisUsd: 30, sharesClosed: 100 }), // entry price 0.30
      close({ costBasisUsd: 30, sharesClosed: 100 }),
      close({ costBasisUsd: 70, sharesClosed: 100 }), // entry price 0.70
      close({ costBasisUsd: 70, sharesClosed: 100 }),
      close({ costBasisUsd: 70, sharesClosed: 100 }),
    ];
    const result = aggregateCategoryScores(closes);
    // total cost basis = 30+30+70+70+70 = 270, total shares = 500 -> 0.54
    expect(result.crypto.avg_entry_price).toBeCloseTo(270 / 500);
  });

  it("computes avg_fee_rate as cost-basis-weighted fee rate across closes", () => {
    const closes = [
      close({ costBasisUsd: 100, feeRate: 0.0 }),
      close({ costBasisUsd: 100, feeRate: 0.0 }),
      close({ costBasisUsd: 100, feeRate: 0.1 }),
      close({ costBasisUsd: 100, feeRate: 0.1 }),
      close({ costBasisUsd: 100, feeRate: 0.1 }),
    ];
    const result = aggregateCategoryScores(closes);
    // (0*100*2 + 0.1*100*3) / 500 = 30/500 = 0.06
    expect(result.crypto.avg_fee_rate).toBeCloseTo(0.06);
  });

  describe("pnl_t_stat", () => {
    it("is a large negative finite sentinel (not Infinity) when every close has identical negative PnL", () => {
      // Zero variance -> maximally strong evidence of harm, but must survive
      // JSON.stringify (Infinity silently becomes null — confirmed live).
      const closes = Array.from({ length: 5 }, () => close({ category: "sports", pnlUsd: -10 }));
      const result = aggregateCategoryScores(closes);
      expect(result.sports.pnl_t_stat).not.toBeNull();
      expect(Number.isFinite(result.sports.pnl_t_stat)).toBe(true);
      expect(result.sports.pnl_t_stat).toBeLessThan(-100);
      expect(JSON.parse(JSON.stringify(result)).sports.pnl_t_stat).toBe(result.sports.pnl_t_stat);
    });

    it("is a large positive finite sentinel when every close has identical positive PnL", () => {
      const closes = Array.from({ length: 5 }, () => close({ category: "sports", pnlUsd: 10 }));
      const result = aggregateCategoryScores(closes);
      expect(result.sports.pnl_t_stat).toBeGreaterThan(100);
    });

    it("is negative but modest for a mix of wins and losses averaging slightly negative", () => {
      const closes = [
        close({ category: "sports", pnlUsd: 10 }),
        close({ category: "sports", pnlUsd: -12 }),
        close({ category: "sports", pnlUsd: 10 }),
        close({ category: "sports", pnlUsd: -12 }),
        close({ category: "sports", pnlUsd: -11 }),
      ];
      const result = aggregateCategoryScores(closes);
      expect(result.sports.pnl_t_stat).not.toBeNull();
      expect(result.sports.pnl_t_stat as number).toBeLessThan(0);
      // Real variance present -> nowhere near the zero-variance sentinel magnitude.
      expect(Math.abs(result.sports.pnl_t_stat as number)).toBeLessThan(100);
    });

    it("is near zero (not significant) when wins and losses roughly cancel out", () => {
      const closes = [
        close({ category: "sports", pnlUsd: 50 }),
        close({ category: "sports", pnlUsd: -48 }),
        close({ category: "sports", pnlUsd: 45 }),
        close({ category: "sports", pnlUsd: -49 }),
        close({ category: "sports", pnlUsd: 2 }),
      ];
      const result = aggregateCategoryScores(closes);
      expect(Math.abs(result.sports.pnl_t_stat as number)).toBeLessThan(1.645); // not significant at 95%
    });
  });
});
