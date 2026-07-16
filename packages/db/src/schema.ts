import { randomUUID } from "node:crypto";
import { sql } from "drizzle-orm";
import { integer, real, sqliteTable, text, index, uniqueIndex } from "drizzle-orm/sqlite-core";

const id = () =>
  text("id")
    .primaryKey()
    .$defaultFn(() => randomUUID());

// Takes an explicit column name so semantically distinct timestamp fields
// (openedAt, seenAt, observedAt, ...) never silently collide on the same
// underlying "created_at" column — each call site below names its own.
const timestampCol = (colName: string) =>
  integer(colName, { mode: "timestamp" })
    .notNull()
    .default(sql`(unixepoch())`);

const createdAt = () => timestampCol("created_at");

// ---------------------------------------------------------------------------
// Copy-trading domain (Hermes build-prompt models)
// ---------------------------------------------------------------------------

export const leaderboardScan = sqliteTable(
  "leaderboard_scan",
  {
    id: id(),
    scannedAt: integer("scanned_at", { mode: "timestamp" }).notNull().default(sql`(unixepoch())`),
    // "discover_traders_overall" | "discover_traders_<category>" |
    // "event_top_holders_<slug>" | "market_holders_<slug>" — see
    // packages/copy-trading/src/scanLeaderboard.ts for the fan-out design;
    // bullpen has no single call that returns a 500-row leaderboard.
    source: text("source").notNull(),
    rank: integer("rank"),
    walletAddress: text("wallet_address").notNull(),
    displayName: text("display_name"),
    pnl7d: real("pnl_7d"),
    pnl30d: real("pnl_30d"),
    pnlAllTime: real("pnl_all_time"),
    volume7d: real("volume_7d"),
    volume30d: real("volume_30d"),
    winRate: real("win_rate"),
    tradeCount: integer("trade_count"),
    rawJson: text("raw_json").notNull(),
    createdAt: createdAt(),
  },
  (t) => [index("leaderboard_scan_wallet_idx").on(t.walletAddress), index("leaderboard_scan_scanned_at_idx").on(t.scannedAt)]
);

export const walletProfile = sqliteTable(
  "wallet_profile",
  {
    id: id(),
    walletAddress: text("wallet_address").notNull().unique(),
    nickname: text("nickname"),
    // track | watch | ignore — owned by the TS scorer/rule engine, NOT bot.py.
    status: text("status").notNull().default("watch"),
    statusReason: text("status_reason"),
    statusChangedAt: integer("status_changed_at", { mode: "timestamp" }),
    // Owned exclusively by bot.py's existing circuit-breaker logic. Deliberately
    // a separate field from `status` so the two writers never fight over one
    // column — bot.py's trading gate checks circuitBreakerMuted OR status != 'track'.
    circuitBreakerMuted: integer("circuit_breaker_muted", { mode: "boolean" }).notNull().default(false),
    muteReason: text("mute_reason"),
    mutedAt: integer("muted_at", { mode: "timestamp" }),
    consecutiveLosses: integer("consecutive_losses").notNull().default(0),
    recentResultsJson: text("recent_results_json"),
    category: text("category"),
    isLikelyBot: integer("is_likely_bot", { mode: "boolean" }),
    isWhale: integer("is_whale", { mode: "boolean" }),
    riskTier: text("risk_tier"),
    copyabilityTier: text("copyability_tier"),
    profitFactor: real("profit_factor"),
    winRate: real("win_rate"),
    tradeCountAllTime: integer("trade_count_all_time"),
    volume30d: real("volume_30d"),
    pnl7d: real("pnl_7d"),
    pnl30d: real("pnl_30d"),
    pnlAllTime: real("pnl_all_time"),
    roiScore: real("roi_score"),
    consistencyScore: real("consistency_score"),
    copyabilityScore: real("copyability_score"),
    oneHitWonderPenalty: real("one_hit_wonder_penalty"),
    compositeScore: real("composite_score"),
    scoreBreakdownJson: text("score_breakdown_json"),
    lastScoredAt: integer("last_scored_at", { mode: "timestamp" }),
    firstSeenAt: timestampCol("first_seen_at"),
    notes: text("notes"),
    isDemoData: integer("is_demo_data", { mode: "boolean" }).notNull().default(false),
    createdAt: createdAt(),
    updatedAt: integer("updated_at", { mode: "timestamp" })
      .notNull()
      .default(sql`(unixepoch())`),
  },
  (t) => [index("wallet_profile_status_idx").on(t.status), index("wallet_profile_score_idx").on(t.compositeScore)]
);

export const observedTrade = sqliteTable(
  "observed_trade",
  {
    id: id(),
    tradeId: text("trade_id").notNull().unique(),
    walletAddress: text("wallet_address").notNull(),
    marketSlug: text("market_slug").notNull(),
    marketTitle: text("market_title"),
    outcome: text("outcome").notNull(),
    side: text("side").notNull(), // BUY | SELL
    price: real("price").notNull(),
    sizeUsd: real("size_usd"),
    sourceTimestamp: text("source_timestamp").notNull(),
    observedAt: timestampCol("observed_at"),
    rawJson: text("raw_json").notNull(),
    isDemoData: integer("is_demo_data", { mode: "boolean" }).notNull().default(false),
  },
  (t) => [
    index("observed_trade_wallet_idx").on(t.walletAddress),
    index("observed_trade_market_idx").on(t.marketSlug, t.outcome),
  ]
);

export const marketSnapshot = sqliteTable(
  "market_snapshot",
  {
    id: id(),
    marketSlug: text("market_slug").notNull(),
    outcome: text("outcome").notNull(),
    capturedAt: integer("captured_at", { mode: "timestamp" }).notNull().default(sql`(unixepoch())`),
    bestBid: real("best_bid"),
    bestAsk: real("best_ask"),
    midpoint: real("midpoint"),
    lastTrade: real("last_trade"),
    spreadAbs: real("spread_abs"),
    spreadRel: real("spread_rel"),
    liquidityWarning: text("liquidity_warning"),
    rawJson: text("raw_json").notNull(),
  },
  (t) => [index("market_snapshot_lookup_idx").on(t.marketSlug, t.outcome, t.capturedAt)]
);

export const decisionJournal = sqliteTable(
  "decision_journal",
  {
    id: id(),
    createdAt: createdAt(),
    walletAddress: text("wallet_address").notNull(),
    observedTradeId: text("observed_trade_id"),
    marketSlug: text("market_slug").notNull(),
    outcome: text("outcome").notNull(),
    side: text("side"),
    decisionType: text("decision_type").notNull(), // copy | watchlist | skip
    decisionReason: text("decision_reason").notNull(),
    scoreBreakdownJson: text("score_breakdown_json"),
    ruleSetVersion: integer("rule_set_version"),
    resultingAction: text("resulting_action"),
    linkedPaperTradeId: text("linked_paper_trade_id"),
    // "bot.py" | "score:trades" — which system made the call.
    source: text("source").notNull(),
  },
  (t) => [index("decision_journal_wallet_idx").on(t.walletAddress), index("decision_journal_type_idx").on(t.decisionType)]
);

export const paperTrade = sqliteTable(
  "paper_trade",
  {
    id: id(),
    // bot_filtered | blind_leaderboard_benchmark
    strategy: text("strategy").notNull().default("bot_filtered"),
    walletAddress: text("wallet_address").notNull(),
    marketSlug: text("market_slug").notNull(),
    marketTitle: text("market_title"),
    outcome: text("outcome").notNull(),
    sourcePrice: real("source_price"),
    sourceSizeUsd: real("source_size_usd"),
    ourSizeUsd: real("our_size_usd").notNull(), // size of the most recent buy (config.FIXED_TRADE_USD at fill time)
    // Running total invested in this position across all buys (bot.py's
    // pos["cost_basis_usd"]) — distinct from ourSizeUsd (latest buy only)
    // once MAX_BUYS_PER_TRADER_OUTCOME allows averaging up.
    costBasisUsd: real("cost_basis_usd").notNull().default(0),
    ourShares: real("our_shares").notNull(),
    avgEntryPrice: real("avg_entry_price").notNull(),
    // Number of buys applied to this position (bot.py's pos["buy_count"]),
    // enforced against config.MAX_BUYS_PER_TRADER_OUTCOME.
    buyCount: integer("buy_count").notNull().default(0),
    status: text("status").notNull(), // open | closed
    openedAt: timestampCol("opened_at"),
    closedAt: integer("closed_at", { mode: "timestamp" }),
    // source_sell | trailing_tp | resolved | circuit_breaker
    closeReason: text("close_reason"),
    realizedPnlUsd: real("realized_pnl_usd"),
    peakProfitPct: real("peak_profit_pct").notNull().default(0),
    decisionJournalId: text("decision_journal_id"),
    isDemoData: integer("is_demo_data", { mode: "boolean" }).notNull().default(false),
  },
  (t) => [index("paper_trade_lookup_idx").on(t.walletAddress, t.marketSlug, t.outcome, t.status)]
);

export const pnlSnapshot = sqliteTable("pnl_snapshot", {
  id: id(),
  capturedAt: integer("captured_at", { mode: "timestamp" }).notNull().default(sql`(unixepoch())`),
  scope: text("scope").notNull(), // overall | per_wallet | per_market
  strategy: text("strategy").notNull().default("bot_filtered"),
  walletAddress: text("wallet_address"),
  realizedPnlUsd: real("realized_pnl_usd").notNull(),
  unrealizedPnlUsd: real("unrealized_pnl_usd").notNull(),
  openPositionsCount: integer("open_positions_count").notNull(),
  closedTradesCount: integer("closed_trades_count").notNull(),
  winRate: real("win_rate"),
});

export const outcomeReview = sqliteTable("outcome_review", {
  id: id(),
  marketSlug: text("market_slug").notNull(),
  outcome: text("outcome").notNull(),
  walletAddress: text("wallet_address").notNull(),
  paperTradeId: text("paper_trade_id"),
  resolvedAt: integer("resolved_at", { mode: "timestamp" }).notNull(),
  finalOutcome: text("final_outcome").notNull(),
  wasCorrectCall: integer("was_correct_call", { mode: "boolean" }).notNull(),
  pnlUsd: real("pnl_usd"),
  reviewNotes: text("review_notes"),
  contributingScoreFactorsJson: text("contributing_score_factors_json"),
  createdAt: createdAt(),
});

export const ruleSet = sqliteTable("rule_set", {
  id: id(),
  version: integer("version").notNull().unique(),
  isActive: integer("is_active", { mode: "boolean" }).notNull().default(false),
  // spread_tolerance, ttp_activation_pct, ttp_drawdown_pct,
  // mute_consecutive_loss_streak, mute_win_rate_threshold,
  // min_composite_score_to_track, ...
  thresholdsJson: text("thresholds_json").notNull(),
  description: text("description"),
  createdAt: createdAt(),
});

export const ruleChange = sqliteTable("rule_change", {
  id: id(),
  createdAt: createdAt(),
  ruleSetVersionFrom: integer("rule_set_version_from"),
  ruleSetVersionTo: integer("rule_set_version_to").notNull(),
  changedField: text("changed_field").notNull(),
  oldValue: text("old_value"),
  newValue: text("new_value").notNull(),
  rationale: text("rationale").notNull(),
  triggeringOutcomeReviewIds: text("triggering_outcome_review_ids"), // json array of ids
  appliedBy: text("applied_by").notNull(), // auto | manual
});

export const dailyReport = sqliteTable("daily_report", {
  id: id(),
  reportDate: text("report_date").notNull().unique(), // YYYY-MM-DD
  generatedAt: timestampCol("generated_at"),
  summaryJson: text("summary_json").notNull(),
  botFilteredPnl: real("bot_filtered_pnl"),
  blindLeaderboardPnl: real("blind_leaderboard_pnl"),
  narrativeText: text("narrative_text"),
  telegramSentAt: integer("telegram_sent_at", { mode: "timestamp" }),
  isDemoData: integer("is_demo_data", { mode: "boolean" }).notNull().default(false),
});

// --- bot.py-owned plumbing tables (not in the spec's model list, needed for
// parity with today's state.json/trades_log.json). TS/Drizzle still owns the
// DDL; only bot.py/db.py read and write the rows. ---------------------------

export const botEventLog = sqliteTable("bot_event_log", {
  id: id(),
  timestamp: integer("timestamp", { mode: "timestamp" }).notNull().default(sql`(unixepoch())`),
  eventType: text("event_type").notNull(),
  traderAddress: text("trader_address"),
  marketSlug: text("market_slug"),
  outcome: text("outcome"),
  side: text("side"),
  payloadJson: text("payload_json").notNull(),
});

export const botSeenTrade = sqliteTable("bot_seen_trade", {
  tradeId: text("trade_id").primaryKey(),
  seenAt: timestampCol("seen_at"),
});

export const botSourcePosition = sqliteTable("bot_source_position", {
  key: text("key").primaryKey(), // trader|market_slug|outcome
  shares: real("shares").notNull(),
});

// ---------------------------------------------------------------------------
// Weather domain
// ---------------------------------------------------------------------------

export const weatherStation = sqliteTable("weather_station", {
  id: id(),
  externalId: text("external_id").notNull().unique(),
  name: text("name").notNull(),
  source: text("source").notNull(), // noaa | openmeteo | wunderground
  lat: real("lat").notNull(),
  lon: real("lon").notNull(),
  timezone: text("timezone").notNull(),
  notes: text("notes"),
});

export const weatherHistoricalObservation = sqliteTable(
  "weather_historical_observation",
  {
    id: id(),
    stationId: text("station_id").notNull(),
    obsDate: text("obs_date").notNull(), // YYYY-MM-DD
    tMaxF: real("t_max_f"),
    tMinF: real("t_min_f"),
    precipIn: real("precip_in"),
    conditionCode: text("condition_code"),
    source: text("source").notNull(),
    fetchedAt: timestampCol("fetched_at"),
    rawJson: text("raw_json").notNull(),
  },
  (t) => [uniqueIndex("weather_obs_unique_idx").on(t.stationId, t.obsDate, t.source)]
);

export const weatherForecastSnapshot = sqliteTable(
  "weather_forecast_snapshot",
  {
    id: id(),
    stationId: text("station_id").notNull(),
    forecastFor: text("forecast_for").notNull(), // YYYY-MM-DD
    issuedAt: timestampCol("issued_at"),
    source: text("source").notNull(), // openmeteo | nws
    tMaxForecastF: real("t_max_forecast_f"),
    tMinForecastF: real("t_min_forecast_f"),
    popPct: real("pop_pct"),
    rawJson: text("raw_json").notNull(),
  },
  (t) => [index("weather_forecast_lookup_idx").on(t.stationId, t.forecastFor)]
);

export const weatherMarketMapping = sqliteTable("weather_market_mapping", {
  id: id(),
  marketSlug: text("market_slug").notNull().unique(),
  stationId: text("station_id").notNull(),
  // The ACTUAL settlement authority for this market — critical field, since the
  // probability model must be calibrated to whatever source each market cites.
  settlementSource: text("settlement_source").notNull(), // noaa | wunderground | ...
  settlementRule: text("settlement_rule").notNull(), // free text, e.g. "YES if official NWS max >= 90F"
  isActive: integer("is_active", { mode: "boolean" }).notNull().default(true),
});

export const weatherProbabilityEstimate = sqliteTable(
  "weather_probability_estimate",
  {
    id: id(),
    marketSlug: text("market_slug").notNull(),
    outcome: text("outcome").notNull(),
    estimatedAt: integer("estimated_at", { mode: "timestamp" }).notNull().default(sql`(unixepoch())`),
    climatologyProb: real("climatology_prob").notNull(),
    forecastProb: real("forecast_prob"),
    blendedProb: real("blended_prob").notNull(),
    marketImpliedProb: real("market_implied_prob"),
    edge: real("edge"),
    modelVersion: text("model_version").notNull(),
    inputsJson: text("inputs_json").notNull(),
  },
  (t) => [index("weather_prob_lookup_idx").on(t.marketSlug, t.outcome, t.estimatedAt)]
);

export const weatherPosition = sqliteTable("weather_position", {
  id: id(),
  marketSlug: text("market_slug").notNull(),
  outcome: text("outcome").notNull(),
  openedAt: timestampCol("opened_at"),
  closedAt: integer("closed_at", { mode: "timestamp" }),
  status: text("status").notNull(), // open | closed
  entryProb: real("entry_prob"),
  entryPrice: real("entry_price").notNull(),
  ourSizeUsd: real("our_size_usd").notNull(),
  ourShares: real("our_shares").notNull(),
  avgEntryPrice: real("avg_entry_price").notNull(),
  peakProfitPct: real("peak_profit_pct").notNull().default(0),
  closeReason: text("close_reason"),
  realizedPnlUsd: real("realized_pnl_usd"),
  isDemoData: integer("is_demo_data", { mode: "boolean" }).notNull().default(false),
});

export const weatherPnlSnapshot = sqliteTable("weather_pnl_snapshot", {
  id: id(),
  capturedAt: integer("captured_at", { mode: "timestamp" }).notNull().default(sql`(unixepoch())`),
  realizedPnlUsd: real("realized_pnl_usd").notNull(),
  unrealizedPnlUsd: real("unrealized_pnl_usd").notNull(),
  openPositionsCount: integer("open_positions_count").notNull(),
  winRate: real("win_rate"),
});
