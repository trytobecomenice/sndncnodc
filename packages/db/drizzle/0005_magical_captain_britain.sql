CREATE TABLE `weather_market_odds_snapshot` (
	`id` text PRIMARY KEY NOT NULL,
	`market_slug` text NOT NULL,
	`recorded_at` integer DEFAULT (unixepoch()) NOT NULL,
	`implied_probability` real NOT NULL
);
--> statement-breakpoint
CREATE INDEX `weather_market_odds_snapshot_lookup_idx` ON `weather_market_odds_snapshot` (`market_slug`,`recorded_at`);--> statement-breakpoint
ALTER TABLE `weather_market_mapping` ADD `metric` text;--> statement-breakpoint
ALTER TABLE `weather_market_mapping` ADD `forecast_for` text;--> statement-breakpoint
ALTER TABLE `weather_market_mapping` ADD `target_temp_min_f` real;--> statement-breakpoint
ALTER TABLE `weather_market_mapping` ADD `target_temp_max_f` real;