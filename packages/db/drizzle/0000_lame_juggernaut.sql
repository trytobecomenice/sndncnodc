CREATE TABLE `bot_event_log` (
	`id` text PRIMARY KEY NOT NULL,
	`timestamp` integer DEFAULT (unixepoch()) NOT NULL,
	`event_type` text NOT NULL,
	`trader_address` text,
	`market_slug` text,
	`outcome` text,
	`side` text,
	`payload_json` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `bot_seen_trade` (
	`trade_id` text PRIMARY KEY NOT NULL,
	`seen_at` integer DEFAULT (unixepoch()) NOT NULL
);
--> statement-breakpoint
CREATE TABLE `bot_source_position` (
	`key` text PRIMARY KEY NOT NULL,
	`shares` real NOT NULL
);
--> statement-breakpoint
CREATE TABLE `daily_report` (
	`id` text PRIMARY KEY NOT NULL,
	`report_date` text NOT NULL,
	`generated_at` integer DEFAULT (unixepoch()) NOT NULL,
	`summary_json` text NOT NULL,
	`bot_filtered_pnl` real,
	`blind_leaderboard_pnl` real,
	`narrative_text` text,
	`telegram_sent_at` integer,
	`is_demo_data` integer DEFAULT false NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `daily_report_report_date_unique` ON `daily_report` (`report_date`);--> statement-breakpoint
CREATE TABLE `decision_journal` (
	`id` text PRIMARY KEY NOT NULL,
	`created_at` integer DEFAULT (unixepoch()) NOT NULL,
	`wallet_address` text NOT NULL,
	`observed_trade_id` text,
	`market_slug` text NOT NULL,
	`outcome` text NOT NULL,
	`side` text,
	`decision_type` text NOT NULL,
	`decision_reason` text NOT NULL,
	`score_breakdown_json` text,
	`rule_set_version` integer,
	`resulting_action` text,
	`linked_paper_trade_id` text,
	`source` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `decision_journal_wallet_idx` ON `decision_journal` (`wallet_address`);--> statement-breakpoint
CREATE INDEX `decision_journal_type_idx` ON `decision_journal` (`decision_type`);--> statement-breakpoint
CREATE TABLE `leaderboard_scan` (
	`id` text PRIMARY KEY NOT NULL,
	`scanned_at` integer DEFAULT (unixepoch()) NOT NULL,
	`source` text NOT NULL,
	`rank` integer,
	`wallet_address` text NOT NULL,
	`display_name` text,
	`pnl_7d` real,
	`pnl_30d` real,
	`pnl_all_time` real,
	`volume_7d` real,
	`volume_30d` real,
	`win_rate` real,
	`trade_count` integer,
	`raw_json` text NOT NULL,
	`created_at` integer DEFAULT (unixepoch()) NOT NULL
);
--> statement-breakpoint
CREATE INDEX `leaderboard_scan_wallet_idx` ON `leaderboard_scan` (`wallet_address`);--> statement-breakpoint
CREATE INDEX `leaderboard_scan_scanned_at_idx` ON `leaderboard_scan` (`scanned_at`);--> statement-breakpoint
CREATE TABLE `market_snapshot` (
	`id` text PRIMARY KEY NOT NULL,
	`market_slug` text NOT NULL,
	`outcome` text NOT NULL,
	`captured_at` integer DEFAULT (unixepoch()) NOT NULL,
	`best_bid` real,
	`best_ask` real,
	`midpoint` real,
	`last_trade` real,
	`spread_abs` real,
	`spread_rel` real,
	`liquidity_warning` text,
	`raw_json` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `market_snapshot_lookup_idx` ON `market_snapshot` (`market_slug`,`outcome`,`captured_at`);--> statement-breakpoint
CREATE TABLE `observed_trade` (
	`id` text PRIMARY KEY NOT NULL,
	`trade_id` text NOT NULL,
	`wallet_address` text NOT NULL,
	`market_slug` text NOT NULL,
	`market_title` text,
	`outcome` text NOT NULL,
	`side` text NOT NULL,
	`price` real NOT NULL,
	`size_usd` real,
	`source_timestamp` text NOT NULL,
	`observed_at` integer DEFAULT (unixepoch()) NOT NULL,
	`raw_json` text NOT NULL,
	`is_demo_data` integer DEFAULT false NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `observed_trade_trade_id_unique` ON `observed_trade` (`trade_id`);--> statement-breakpoint
CREATE INDEX `observed_trade_wallet_idx` ON `observed_trade` (`wallet_address`);--> statement-breakpoint
CREATE INDEX `observed_trade_market_idx` ON `observed_trade` (`market_slug`,`outcome`);--> statement-breakpoint
CREATE TABLE `outcome_review` (
	`id` text PRIMARY KEY NOT NULL,
	`market_slug` text NOT NULL,
	`outcome` text NOT NULL,
	`wallet_address` text NOT NULL,
	`paper_trade_id` text,
	`resolved_at` integer NOT NULL,
	`final_outcome` text NOT NULL,
	`was_correct_call` integer NOT NULL,
	`pnl_usd` real,
	`review_notes` text,
	`contributing_score_factors_json` text,
	`created_at` integer DEFAULT (unixepoch()) NOT NULL
);
--> statement-breakpoint
CREATE TABLE `paper_trade` (
	`id` text PRIMARY KEY NOT NULL,
	`strategy` text DEFAULT 'bot_filtered' NOT NULL,
	`wallet_address` text NOT NULL,
	`market_slug` text NOT NULL,
	`market_title` text,
	`outcome` text NOT NULL,
	`source_price` real,
	`source_size_usd` real,
	`our_size_usd` real NOT NULL,
	`cost_basis_usd` real DEFAULT 0 NOT NULL,
	`our_shares` real NOT NULL,
	`avg_entry_price` real NOT NULL,
	`buy_count` integer DEFAULT 0 NOT NULL,
	`status` text NOT NULL,
	`opened_at` integer DEFAULT (unixepoch()) NOT NULL,
	`closed_at` integer,
	`close_reason` text,
	`realized_pnl_usd` real,
	`peak_profit_pct` real DEFAULT 0 NOT NULL,
	`decision_journal_id` text,
	`is_demo_data` integer DEFAULT false NOT NULL
);
--> statement-breakpoint
CREATE INDEX `paper_trade_lookup_idx` ON `paper_trade` (`wallet_address`,`market_slug`,`outcome`,`status`);--> statement-breakpoint
CREATE TABLE `pnl_snapshot` (
	`id` text PRIMARY KEY NOT NULL,
	`captured_at` integer DEFAULT (unixepoch()) NOT NULL,
	`scope` text NOT NULL,
	`strategy` text DEFAULT 'bot_filtered' NOT NULL,
	`wallet_address` text,
	`realized_pnl_usd` real NOT NULL,
	`unrealized_pnl_usd` real NOT NULL,
	`open_positions_count` integer NOT NULL,
	`closed_trades_count` integer NOT NULL,
	`win_rate` real
);
--> statement-breakpoint
CREATE TABLE `rule_change` (
	`id` text PRIMARY KEY NOT NULL,
	`created_at` integer DEFAULT (unixepoch()) NOT NULL,
	`rule_set_version_from` integer,
	`rule_set_version_to` integer NOT NULL,
	`changed_field` text NOT NULL,
	`old_value` text,
	`new_value` text NOT NULL,
	`rationale` text NOT NULL,
	`triggering_outcome_review_ids` text,
	`applied_by` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `rule_set` (
	`id` text PRIMARY KEY NOT NULL,
	`version` integer NOT NULL,
	`is_active` integer DEFAULT false NOT NULL,
	`thresholds_json` text NOT NULL,
	`description` text,
	`created_at` integer DEFAULT (unixepoch()) NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `rule_set_version_unique` ON `rule_set` (`version`);--> statement-breakpoint
CREATE TABLE `wallet_profile` (
	`id` text PRIMARY KEY NOT NULL,
	`wallet_address` text NOT NULL,
	`nickname` text,
	`status` text DEFAULT 'watch' NOT NULL,
	`status_reason` text,
	`status_changed_at` integer,
	`circuit_breaker_muted` integer DEFAULT false NOT NULL,
	`mute_reason` text,
	`muted_at` integer,
	`consecutive_losses` integer DEFAULT 0 NOT NULL,
	`recent_results_json` text,
	`category` text,
	`is_likely_bot` integer,
	`is_whale` integer,
	`risk_tier` text,
	`copyability_tier` text,
	`profit_factor` real,
	`win_rate` real,
	`trade_count_all_time` integer,
	`volume_30d` real,
	`pnl_7d` real,
	`pnl_30d` real,
	`pnl_all_time` real,
	`roi_score` real,
	`consistency_score` real,
	`copyability_score` real,
	`one_hit_wonder_penalty` real,
	`composite_score` real,
	`score_breakdown_json` text,
	`last_scored_at` integer,
	`first_seen_at` integer DEFAULT (unixepoch()) NOT NULL,
	`notes` text,
	`is_demo_data` integer DEFAULT false NOT NULL,
	`created_at` integer DEFAULT (unixepoch()) NOT NULL,
	`updated_at` integer DEFAULT (unixepoch()) NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `wallet_profile_wallet_address_unique` ON `wallet_profile` (`wallet_address`);--> statement-breakpoint
CREATE INDEX `wallet_profile_status_idx` ON `wallet_profile` (`status`);--> statement-breakpoint
CREATE INDEX `wallet_profile_score_idx` ON `wallet_profile` (`composite_score`);--> statement-breakpoint
CREATE TABLE `weather_forecast_snapshot` (
	`id` text PRIMARY KEY NOT NULL,
	`station_id` text NOT NULL,
	`forecast_for` text NOT NULL,
	`issued_at` integer DEFAULT (unixepoch()) NOT NULL,
	`source` text NOT NULL,
	`t_max_forecast_f` real,
	`t_min_forecast_f` real,
	`pop_pct` real,
	`raw_json` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `weather_forecast_lookup_idx` ON `weather_forecast_snapshot` (`station_id`,`forecast_for`);--> statement-breakpoint
CREATE TABLE `weather_historical_observation` (
	`id` text PRIMARY KEY NOT NULL,
	`station_id` text NOT NULL,
	`obs_date` text NOT NULL,
	`t_max_f` real,
	`t_min_f` real,
	`precip_in` real,
	`condition_code` text,
	`source` text NOT NULL,
	`fetched_at` integer DEFAULT (unixepoch()) NOT NULL,
	`raw_json` text NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `weather_obs_unique_idx` ON `weather_historical_observation` (`station_id`,`obs_date`,`source`);--> statement-breakpoint
CREATE TABLE `weather_market_mapping` (
	`id` text PRIMARY KEY NOT NULL,
	`market_slug` text NOT NULL,
	`station_id` text NOT NULL,
	`settlement_source` text NOT NULL,
	`settlement_rule` text NOT NULL,
	`is_active` integer DEFAULT true NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `weather_market_mapping_market_slug_unique` ON `weather_market_mapping` (`market_slug`);--> statement-breakpoint
CREATE TABLE `weather_pnl_snapshot` (
	`id` text PRIMARY KEY NOT NULL,
	`captured_at` integer DEFAULT (unixepoch()) NOT NULL,
	`realized_pnl_usd` real NOT NULL,
	`unrealized_pnl_usd` real NOT NULL,
	`open_positions_count` integer NOT NULL,
	`win_rate` real
);
--> statement-breakpoint
CREATE TABLE `weather_position` (
	`id` text PRIMARY KEY NOT NULL,
	`market_slug` text NOT NULL,
	`outcome` text NOT NULL,
	`opened_at` integer DEFAULT (unixepoch()) NOT NULL,
	`closed_at` integer,
	`status` text NOT NULL,
	`entry_prob` real,
	`entry_price` real NOT NULL,
	`our_size_usd` real NOT NULL,
	`our_shares` real NOT NULL,
	`avg_entry_price` real NOT NULL,
	`peak_profit_pct` real DEFAULT 0 NOT NULL,
	`close_reason` text,
	`realized_pnl_usd` real,
	`is_demo_data` integer DEFAULT false NOT NULL
);
--> statement-breakpoint
CREATE TABLE `weather_probability_estimate` (
	`id` text PRIMARY KEY NOT NULL,
	`market_slug` text NOT NULL,
	`outcome` text NOT NULL,
	`estimated_at` integer DEFAULT (unixepoch()) NOT NULL,
	`climatology_prob` real NOT NULL,
	`forecast_prob` real,
	`blended_prob` real NOT NULL,
	`market_implied_prob` real,
	`edge` real,
	`model_version` text NOT NULL,
	`inputs_json` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `weather_prob_lookup_idx` ON `weather_probability_estimate` (`market_slug`,`outcome`,`estimated_at`);--> statement-breakpoint
CREATE TABLE `weather_station` (
	`id` text PRIMARY KEY NOT NULL,
	`external_id` text NOT NULL,
	`name` text NOT NULL,
	`source` text NOT NULL,
	`lat` real NOT NULL,
	`lon` real NOT NULL,
	`timezone` text NOT NULL,
	`notes` text
);
--> statement-breakpoint
CREATE UNIQUE INDEX `weather_station_external_id_unique` ON `weather_station` (`external_id`);