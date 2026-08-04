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
    // track | challenger | retiring | watch | ignore. Scorers write watch/
    // ignore recommendations; the Python challenger/Telegram workflow owns
    // challenger -> track -> retiring lifecycle transitions.
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
    // Per-category breakdown (added for category-specific wallet scoring, 2026-07):
    // {"politics": {"score": 0.72, "trade_count": 34, "win_rate": 0.65, "avg_pnl_usd": 12.4}, ...}
    // — same "extra scoring detail lives in a JSON blob" pattern as scoreBreakdownJson above,
    // not a new table, since the category set is small and fixed (config.CATEGORY_TAG_SLUGS).
    // Written by scoreWalletCategories.ts; read by bot.py's compute_trade_size_usd() via
    // db.get_wallet_composite_scores(). NULL until that job's first run for a given wallet —
    // callers must treat absence as "no category signal yet," not an error.
    categoryScoresJson: text("category_scores_json"),
    lastScoredAt: integer("last_scored_at", { mode: "timestamp" }),
    firstSeenAt: timestampCol("first_seen_at"),
    notes: text("notes"),
    isDemoData: integer("is_demo_data", { mode: "boolean" }).notNull().default(false),
    // Half-Kelly sizing range multiplier (2026-07-28, rule_set v7) —
    // scales bot.py's compute_trade_size_usd() MIN/MAX_TRADE_USD band
    // per-wallet, based on a raw (unsaturated) Sharpe-proxy from
    // analyzePnlSeries. Deliberately NOT a replacement for the existing
    // Kelly formula (which needs the market price at TRADE time, not scan
    // time) — this only stretches the RANGE Kelly then operates within.
    // NULL until this wallet's first v7+ scoring pass; bot.py treats NULL
    // as 1.0 (no adjustment), never as 0.
    capitalMultiplier: real("capital_multiplier"),
    // Tiered-scoring self-throttle (2026-07-28): NULL means "never scored,
    // always due." Set at write time to now + the wallet's tier-derived
    // cadence (see scoreWallets.ts's TIER_RESCORE_INTERVAL_DAYS) — the
    // pass-1 candidate query skips any wallet whose value here is still in
    // the future, so a frequent scheduler run mostly does nothing once the
    // pool is "warm," rather than needing three separately-scheduled jobs.
    nextRescoreDueAt: integer("next_rescore_due_at", { mode: "timestamp" }),
    createdAt: createdAt(),
    updatedAt: integer("updated_at", { mode: "timestamp" })
      .notNull()
      .default(sql`(unixepoch())`),
  },
  (t) => [
    index("wallet_profile_status_idx").on(t.status),
    index("wallet_profile_score_idx").on(t.compositeScore),
    index("wallet_profile_next_rescore_due_idx").on(t.nextRescoreDueAt),
  ]
);

// Telegram wallet-approval workflow (2026-08-01): the single funnel every
// promotion path (scoreWallets.ts's global pool AND
// discoverCategorySpecialists.ts's category-quota system) writes through
// instead of committing wallet_profile.status='track'/'bench' directly — see
// packages/copy-trading/src/walletApprovalQueue.ts. Joey approves/rejects via
// Telegram inline buttons (send_wallet_approvals.py /
// telegram_approval_listener.py); only on 'approved' does
// wallet_profile.status actually flip.
export const walletApprovalRequest = sqliteTable(
  "wallet_approval_request",
  {
    id: id(),
    walletAddress: text("wallet_address").notNull(),
    // 'track' | 'bench' — the status this request would flip wallet_profile
    // to on approval.
    requestedTier: text("requested_tier").notNull(),
    // 'global_pool' | 'category_quota' | 'challenger_shadow' — which
    // pipeline proposed this candidate, for audit/debugging.
    source: text("source").notNull(),
    // Null for global_pool candidates (that system isn't category-scoped).
    category: text("category"),
    // Composite score / t-stat / win rate / trade count / roi snapshot at
    // proposal time — same "extra detail lives in a JSON blob" pattern as
    // wallet_profile.scoreBreakdownJson, not a wide bespoke column set.
    // challenger_shadow additionally embeds the clean shadow evidence and
    // replacementWalletAddress used for the transactional one-in-one-out.
    scoreSnapshotJson: text("score_snapshot_json").notNull(),
    // Human-readable justification, reused from decideStatus()'s own reason
    // string or the category-quota candidate's stats line — shown verbatim
    // in the Telegram message.
    reason: text("reason").notNull(),
    // 'pending' | 'approved' | 'rejected' | 'expired'
    status: text("status").notNull().default("pending"),
    telegramMessageId: integer("telegram_message_id"),
    telegramChatId: text("telegram_chat_id"),
    createdAt: createdAt(),
    resolvedAt: integer("resolved_at", { mode: "timestamp" }),
  },
  (t) => [
    // The dedup/cooldown lookup's access pattern (queueApprovalRequest):
    // "does this wallet already have a pending/recent request for this tier?"
    index("wallet_approval_request_wallet_tier_status_idx").on(t.walletAddress, t.requestedTier, t.status),
    index("wallet_approval_request_status_idx").on(t.status),
  ]
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
    // Unix seconds of this position's last SUCCESSFUL price read (bot.py's
    // check_trailing_take_profit, on every sweep it manages to price this
    // key) — NULL until the first successful read. Drives the "zombie
    // position" dump-exit fallback (2026-07-27): a position with no
    // successful read in config.ZOMBIE_POSITION_THRESHOLD_SECONDS (24h)
    // becomes eligible for a forced, staleness-bypassing exit rather than
    // leaving capital trapped in a permanently-illiquid/delisted market
    // indefinitely. Persisted (not in-memory only) because the bot restarts
    // often enough that an in-memory 24h clock would rarely, if ever, fire.
    lastPricedAt: integer("last_priced_at", { mode: "timestamp" }),
    decisionJournalId: text("decision_journal_id"),
    isDemoData: integer("is_demo_data", { mode: "boolean" }).notNull().default(false),
  },
  (t) => [index("paper_trade_lookup_idx").on(t.walletAddress, t.marketSlug, t.outcome, t.status)]
);

// One row per UTC calendar day (2026-07-28) — the Grafana personal
// dashboard's equity-curve/Sharpe/tiering-efficiency source table.
// Exclusively written by bot.py's maybe_snapshot_daily_portfolio(); never
// written from TS. Deliberately NOT a repurposing of pnl_snapshot above
// (an existing, currently-unwritten table with a different shape — no
// total_cash/active_traders_followed, and a multi-scope per-wallet/
// per-market design this one-row-per-day table doesn't need) — a fresh,
// narrowly-scoped table matches exactly what was asked for rather than
// overloading an unrelated existing one.
export const dailyPortfolioSnapshot = sqliteTable("daily_portfolio_snapshots", {
  date: text("date").primaryKey(), // 'YYYY-MM-DD', UTC — the idempotency key
  snapshotAt: integer("snapshot_at", { mode: "timestamp" }).notNull(), // exact capture moment, Grafana's time column
  totalEquity: real("total_equity").notNull(),
  totalCash: real("total_cash").notNull(),
  totalUnrealizedPnl: real("total_unrealized_pnl").notNull(),
  realizedPnlToday: real("realized_pnl_today").notNull(),
  activeTradersFollowed: integer("active_traders_followed").notNull(),
});

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

export const botEventLog = sqliteTable(
  "bot_event_log",
  {
    id: id(),
    timestamp: integer("timestamp", { mode: "timestamp" }).notNull().default(sql`(unixepoch())`),
    eventType: text("event_type").notNull(),
    traderAddress: text("trader_address"),
    marketSlug: text("market_slug"),
    outcome: text("outcome"),
    side: text("side"),
    payloadJson: text("payload_json").notNull(),
  },
  // Added 2026-07-31: found live, this table had NO index beyond its primary
  // key despite being the single busiest table in the DB (every event.py
  // append_log() call inserts a row) -- apps/dashboard's /overview page does
  // `ORDER BY timestamp DESC LIMIT 15` on every load, and db.py's own
  // prune_event_log() does `WHERE timestamp < cutoff`, both full-table-scanning
  // and sorting 450K+ rows without this. Confirmed live: /overview page loads
  // had degraded to 7-38s (should be well under 1s) once this table grew that
  // large from one night's incident-driven event volume (repeated stale-trade
  // skips, kill-switch churn, etc.) -- every other significant table in this
  // schema already has an index on its own hot lookup column; this one simply
  // never got one.
  (t) => [index("bot_event_log_timestamp_idx").on(t.timestamp)]
);

export const botSeenTrade = sqliteTable("bot_seen_trade", {
  tradeId: text("trade_id").primaryKey(),
  seenAt: timestampCol("seen_at"),
  // Added 2026-07-31: the old global-cap dedup (top 2000 trade_ids across ALL
  // wallets combined) let one busy wallet's trade volume evict an inactive
  // wallet's older trade_ids from the cap. Since DIRECT_API_PER_WALLET_LIMIT
  // (bot.py/config.py) always returns each wallet's most recent N trades
  // regardless of how long ago they happened, an evicted-but-still-fetched
  // trade_id from a quiet wallet would get treated as brand new on the very
  // next bot.py restart -- confirmed live: two restarts in one session each
  // triggered a burst of "new" copies of month-old, already-resolved-market
  // trades. Nullable because existing rows have no wallet attribution to
  // backfill (trade_id is tx_hash:asset:side:timestamp, not wallet-keyed).
  walletAddress: text("wallet_address"),
});

export const botSourcePosition = sqliteTable("bot_source_position", {
  key: text("key").primaryKey(), // trader|market_slug|outcome
  shares: real("shares").notNull(),
  // Weighted-average cost basis of the SOURCE trader's currently-held shares
  // at this key (added 2026-07-24 for the pending_execution VWAP anchor —
  // see that table's comment). Same weighted-average-on-buy /
  // proportional-reduce-on-sell model bot.py already uses for our own
  // positions[key]["cost_basis_usd"], just mirrored onto the whale's side.
  // Default 0 for rows written before this column existed (source_positions
  // predates this — those wallets simply have no VWAP history until their
  // next observed buy).
  costBasisUsd: real("cost_basis_usd").notNull().default(0),
});

// A resting "wait for confirmation" paper order against ONE tracked wallet's
// own cost basis, added 2026-07-24 (RISK_MANAGEMENT.md Rule 29) to address
// the naive "buy the moment price touches the whale's price" design's
// adverse-selection problem: a prediction-market price dip is often real
// news moving against the position, not noise, so this waits for BOTH a dip
// below the whale's own average cost (anchorPrice, a VWAP of their observed
// fills, ratcheting down only — see compute_anchor_price()) AND a confirmed
// rebound off the resulting local minimum (see compute_rebound_threshold())
// before ever calling _execute_buy() — and re-verifies the whale hasn't
// exited their position in the meantime (whaleSharesAtCreation, see
// whale_still_holding()) before firing. Owned exclusively by bot.py's
// sweep_pending_executions(), like the other bot_* plumbing tables above:
// TS owns the DDL, only bot.py reads/writes rows.
//
// Deliberately a table, not a field folded into bot_source_position/
// paper_trade: it has its own lifecycle (pending -> filled/expired/
// invalidated) independent of both, and needs to be cheaply queryable by
// status+expiry for the TTL sweep. Column shape is also intentionally
// event-shaped (one row per tracked signal, timestamped, with a terminal
// status and reason) rather than a single mutable slot, so that a future
// swap of the polling sweep for a real-time Polygon RPC websocket feed (see
// Rule 29's roadmap note) only has to change WHERE rows get created/updated
// FROM, not what the table records.
export const pendingExecution = sqliteTable(
  "pending_execution",
  {
    id: id(),
    walletAddress: text("wallet_address").notNull(),
    marketSlug: text("market_slug").notNull(),
    outcome: text("outcome").notNull(),
    sourceTradeId: text("source_trade_id"),
    category: text("category"),
    // Whale's VWAP cost basis at creation (or last ratchet-down update) —
    // the hard "never chase above this" ceiling, per the no-high-chasing
    // rule from the original request.
    anchorPrice: real("anchor_price").notNull(),
    // Local minimum observed price since the market first dipped below
    // anchorPrice. NULL until the first such dip — the rebound trigger
    // cannot fire before this is set (see has_rebounded()).
    lowestSeenPrice: real("lowest_seen_price"),
    // source_positions[key] snapshot at creation — the baseline
    // whale_still_holding() compares against on every sweep pass.
    whaleSharesAtCreation: real("whale_shares_at_creation"),
    // Frozen at creation time using the sizing formula active then, same
    // reasoning as paper_trade rows never retroactively resizing on a later
    // rescore.
    targetUsd: real("target_usd").notNull(),
    status: text("status").notNull().default("pending"), // pending | filled | expired | invalidated
    createdAt: timestampCol("created_at"),
    expiresAt: integer("expires_at", { mode: "timestamp" }).notNull(),
    filledAt: integer("filled_at", { mode: "timestamp" }),
    invalidatedReason: text("invalidated_reason"),
  },
  (t) => [index("pending_execution_status_idx").on(t.status, t.walletAddress, t.marketSlug, t.outcome)]
);

// Real-time on-chain trade detection, added 2026-07-24 as the Producer side
// of a Producer-Consumer hand-off: wss_listener.py (a SEPARATE standalone
// process, not bot.py) subscribes to Polygon WebSocket logs for a tracked
// wallet's CTF (ERC1155) TransferSingle events and INSERTs one row per
// detected transfer here. bot.py's normal synchronous sweep loop is meant
// to be the sole CONSUMER (poll `WHERE consumed_at IS NULL`, process, stamp
// consumed_at) — that consumer sweep is not built yet, see wss_listener.py's
// own module docstring for the real integration gap (token_id has no
// market_slug/outcome without a further lookup). Two separate OS processes
// writing/reading the same SQLite file is safe here specifically because
// this DB already runs in WAL mode with a busy_timeout (see migrate.ts) —
// this table's design (one INSERT per producer, one UPDATE per consumer,
// idempotent on (tx_hash, log_index)) does not depend on any additional
// locking beyond that.
export const liveWhaleEvent = sqliteTable(
  "live_whale_event",
  {
    id: id(),
    walletAddress: text("wallet_address").notNull(),
    contractAddress: text("contract_address").notNull(),
    eventType: text("event_type").notNull(), // "TransferSingle" (the only type wss_listener.py emits today)
    // "buy" (wallet is the ERC1155 `to`) | "sell" (wallet is the `from`) —
    // derived from which of the two subscriptions matched, not re-derived
    // from the raw log by the consumer.
    direction: text("direction").notNull(),
    // uint256 values stored as decimal STRINGS, not JS/SQLite numbers —
    // both tokenId and shareAmount can exceed Number.MAX_SAFE_INTEGER.
    tokenId: text("token_id").notNull(),
    shareAmount: text("share_amount").notNull(),
    // Best-effort price derivation: wss_listener.py looks for a paired
    // ERC20 Transfer in the SAME transaction (the collateral leg of the
    // trade) and computes usdcAmount/price from it when found. Both stay
    // NULL when no such transfer was found in the tx — never fabricated.
    usdcAmount: real("usdc_amount"),
    price: real("price"),
    txHash: text("tx_hash").notNull(),
    logIndex: integer("log_index").notNull(),
    blockNumber: integer("block_number").notNull(),
    detectedAt: timestampCol("detected_at"),
    // NULL until a consumer has processed this row — the sweep's
    // "WHERE consumed_at IS NULL" cursor.
    consumedAt: integer("consumed_at", { mode: "timestamp" }),
  },
  (t) => [
    // Idempotency: the producer's own reconnect-and-resubscribe logic can
    // re-deliver a log it already saw right before a drop — this makes a
    // re-insert of the identical (tx_hash, log_index) a silent no-op
    // (INSERT OR IGNORE) rather than a duplicate row.
    uniqueIndex("live_whale_event_tx_log_idx").on(t.txHash, t.logIndex),
    index("live_whale_event_consumed_idx").on(t.consumedAt),
  ]
);

// Local cache mapping a raw on-chain CTF position id (token_id, as seen in
// live_whale_event/wss_listener.py) to the Polymarket market_slug/outcome a
// consumer actually needs — added 2026-07-24 to close exactly the gap
// flagged in live_whale_event's own comment above ("token_id has no
// market_slug/outcome without a further lookup"). Populated by
// token_sync_worker.py (a separate script, same Producer-role precedent as
// wss_listener.py) polling Polymarket's Gamma API. Owned exclusively by
// that worker — same TS-owns-DDL/Python-owns-rows split as every other
// bot_*/live_* table.
export const tokenRegistry = sqliteTable("token_registry", {
  // STRING, not a numeric type — a CTF token_id is a uint256 (up to 78
  // decimal digits) and would silently lose precision in a JS/SQLite
  // number column. Primary key: one row per token_id, upserted in place on
  // every sync rather than accumulating history.
  tokenId: text("token_id").primaryKey(),
  marketSlug: text("market_slug").notNull(),
  outcome: text("outcome").notNull(),
  updatedAt: timestampCol("updated_at"),
});

// Bifurcated dynamic order pegging for SELL/exit execution (2026-07-26,
// "Priority 3") — a patient maker-sell order that walks its price down
// toward a floor over time instead of taking an immediate market fill.
// Deliberately OPT-IN (config.ENABLE_PATIENT_EXIT_PEGGING, default False)
// and bounded: this table's own status lifecycle always terminates in
// either a real fill or a guaranteed market-sell fallback
// ("fallback_market_sell") — it is never allowed to just cancel and leave
// the position open indefinitely. That bound exists specifically because
// this project treats "never delay an exit" as a load-bearing safety
// principle (see RISK_MANAGEMENT.md Rule 6/11) — a resting sell that could
// silently never fill would be exactly the failure mode those rules exist
// to prevent. See bot.sweep_pending_exit_orders() for the full design.
export const pendingExitOrder = sqliteTable(
  "pending_exit_order",
  {
    id: id(),
    walletAddress: text("wallet_address").notNull(),
    marketSlug: text("market_slug").notNull(),
    outcome: text("outcome").notNull(),
    positionKey: text("position_key").notNull(), // trader|market_slug|outcome, matches bot.py's position_key()
    shares: real("shares").notNull(),
    // P_init/P_floor computed ONCE at creation (P_floor from the live
    // measured edge at that moment, see db.compute_live_edge_pct) —
    // currentPrice is what actually gets updated on each reprice.
    initPrice: real("init_price").notNull(),
    floorPrice: real("floor_price").notNull(),
    currentPrice: real("current_price").notNull(),
    bullpenOrderId: text("bullpen_order_id"),
    // whale_sell | trailing_tp -- what triggered this exit, for audit.
    closeReason: text("close_reason").notNull(),
    // pending | filled | fallback_market_sell | canceled
    status: text("status").notNull().default("pending"),
    createdAt: timestampCol("created_at"),
    lastRepricedAt: integer("last_repriced_at", { mode: "timestamp" }),
    filledAt: integer("filled_at", { mode: "timestamp" }),
  },
  (t) => [index("pending_exit_order_status_idx").on(t.status)]
);

// Shadow (paper-only, comparison-only) validation of the patient-exit-peg
// idea from pending_exit_order above — added 2026-08-01 because that
// mechanism is LIVE_MODE-only by construction (a real resting limit order
// has no meaning in paper mode) and this bot currently only runs in paper
// mode, so its actual edge has never been measured. This table records,
// for EVERY trailing-TP exit (paper AND live), what a patient resting-peg
// exit would THEORETICALLY have captured — same P_init/P_floor/decay math
// as pending_exit_order, but driven purely by direct market-data reads
// (bot.get_market_bid_ask), never a bullpen order. It never touches
// `positions` or any real risk/PnL accounting; it exists solely to compare
// resolvedPrice against immediateExitPrice (the price the bot's real,
// already-executing exit actually got) over enough samples to answer
// "would pegging have been worth building for real." See
// bot.sweep_shadow_patient_exits() for the full design.
export const shadowPatientExit = sqliteTable(
  "shadow_patient_exit",
  {
    id: id(),
    walletAddress: text("wallet_address").notNull(),
    marketSlug: text("market_slug").notNull(),
    outcome: text("outcome").notNull(),
    positionKey: text("position_key").notNull(),
    shares: real("shares").notNull(),
    initPrice: real("init_price").notNull(),
    floorPrice: real("floor_price").notNull(),
    currentPrice: real("current_price").notNull(),
    // The real price the bot's actual (immediate) exit obtained at the same
    // trigger moment -- the baseline this simulation is measured against.
    immediateExitPrice: real("immediate_exit_price").notNull(),
    closeReason: text("close_reason").notNull(),
    // pending | filled | fallback_timeout | abandoned
    status: text("status").notNull().default("pending"),
    resolvedPrice: real("resolved_price"),
    createdAt: timestampCol("created_at"),
    lastRepricedAt: integer("last_repriced_at", { mode: "timestamp" }),
    resolvedAt: integer("resolved_at", { mode: "timestamp" }),
  },
  (t) => [index("shadow_patient_exit_status_idx").on(t.status)]
);

// Portfolio-level risk state, owned exclusively by bot.py's risk layer
// (risk_manager.py, via db.py) — same ownership rule as the other bot_*
// plumbing tables: TS owns the DDL, only bot.py reads/writes rows. Known
// keys: "equity_hwm" (float — portfolio equity high-water mark) and
// "kill_switch" (object {triggered_at, reasons, equity, hwm} — present and
// non-null means new BUYs are halted until manually cleared via
// reset_kill_switch.py).
export const botRiskState = sqliteTable("bot_risk_state", {
  key: text("key").primaryKey(),
  valueJson: text("value_json").notNull(),
  updatedAt: timestampCol("updated_at"),
});

// market_slug -> parent event slug memo, resolved once per market via
// `bullpen polymarket market <slug>` (events[0].slug) and cached forever —
// feeds the per-event exposure cap. Owned by bot.py like bot_risk_state.
export const botMarketEvent = sqliteTable("bot_market_event", {
  marketSlug: text("market_slug").primaryKey(),
  eventSlug: text("event_slug").notNull(),
  // Bucketed category (added for category-specific wallet scoring, 2026-07) — resolved
  // alongside eventSlug via the event's own `tags` array (config.CATEGORY_TAG_SLUGS,
  // first-match-wins, else "other"), cached here rather than a second lookup table since a
  // market's event (and therefore its category) never changes once resolved. NULL for rows
  // resolved before this column existed, or for a market whose event lookup didn't include
  // category resolution — callers must not assume it's always populated.
  category: text("category"),
  // Whether this market pays Polymarket's holding rewards (added for TCA/scoring provenance,
  // 2026-07) — a real 3.25% APR paid for merely HOLDING a position on eligible markets,
  // independent of trading skill. Captured purely for documentation/audit purposes: this bot's
  // category_scores_json (RISK_MANAGEMENT.md Rule 18/21) is reconstructed entirely from raw
  // BUY/SELL trade events (packages/copy-trading/src/scoreWalletCategories.ts), and a holding
  // reward payout is not a trade event, so it structurally cannot enter that reconstruction —
  // this column does not feed any scoring formula, it exists so that claim is independently
  // auditable per-market rather than asserted only in prose. Sourced from the same Gamma
  // `/markets?slug=` response category resolution already fetches (`holdingRewardsEnabled`
  // field) — zero extra API calls. NULL for rows resolved before this column existed.
  holdingRewardsEnabled: integer("holding_rewards_enabled", { mode: "boolean" }),
  // Market resolution date (YYYY-MM-DD), added 2026-07-26 for "Priority
  // 4"'s theta-decay TTP activation threshold (config.
  // ENABLE_THETA_DECAY_TP_ACTIVATION) — needs days-remaining-to-resolution
  // per open position. Resolved via bot.resolve_market_end_date(), its own
  // `bullpen polymarket market` call (a genuinely separate concern from
  // event/category resolution above — see that function's own comment for
  // why it isn't folded into resolve_market_event() despite hitting the
  // same endpoint). NULL for rows resolved before this column existed, or
  // when Theta-decay TP is disabled and this was never looked up.
  endDateIso: text("end_date_iso"),
  resolvedAt: timestampCol("resolved_at"),
});
