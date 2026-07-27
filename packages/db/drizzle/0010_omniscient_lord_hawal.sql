CREATE TABLE `pending_execution` (
	`id` text PRIMARY KEY NOT NULL,
	`wallet_address` text NOT NULL,
	`market_slug` text NOT NULL,
	`outcome` text NOT NULL,
	`source_trade_id` text,
	`category` text,
	`anchor_price` real NOT NULL,
	`lowest_seen_price` real,
	`whale_shares_at_creation` real,
	`target_usd` real NOT NULL,
	`status` text DEFAULT 'pending' NOT NULL,
	`created_at` integer DEFAULT (unixepoch()) NOT NULL,
	`expires_at` integer NOT NULL,
	`filled_at` integer,
	`invalidated_reason` text
);
--> statement-breakpoint
CREATE INDEX `pending_execution_status_idx` ON `pending_execution` (`status`,`wallet_address`,`market_slug`,`outcome`);--> statement-breakpoint
ALTER TABLE `bot_source_position` ADD `cost_basis_usd` real DEFAULT 0 NOT NULL;