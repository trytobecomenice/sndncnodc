CREATE TABLE `bot_market_event` (
	`market_slug` text PRIMARY KEY NOT NULL,
	`event_slug` text NOT NULL,
	`resolved_at` integer DEFAULT (unixepoch()) NOT NULL
);
--> statement-breakpoint
CREATE TABLE `bot_risk_state` (
	`key` text PRIMARY KEY NOT NULL,
	`value_json` text NOT NULL,
	`updated_at` integer DEFAULT (unixepoch()) NOT NULL
);
