CREATE TABLE `paper_trade_realized_allocation` (
	`event_id` text PRIMARY KEY NOT NULL,
	`paper_trade_id` text,
	`event_timestamp` integer NOT NULL,
	`event_type` text NOT NULL,
	`strategy` text NOT NULL,
	`pnl_usd` real NOT NULL,
	`cost_basis_usd` real,
	`allocation_status` text NOT NULL,
	`candidate_count` integer NOT NULL,
	`allocator_version` text NOT NULL,
	`allocation_source` text NOT NULL,
	`allocated_at` integer DEFAULT (unixepoch()) NOT NULL
);
--> statement-breakpoint
CREATE INDEX `paper_trade_realized_allocation_trade_idx` ON `paper_trade_realized_allocation` (`paper_trade_id`);--> statement-breakpoint
CREATE INDEX `paper_trade_realized_allocation_status_idx` ON `paper_trade_realized_allocation` (`allocation_status`);--> statement-breakpoint
CREATE INDEX `paper_trade_realized_allocation_timestamp_idx` ON `paper_trade_realized_allocation` (`event_timestamp`);--> statement-breakpoint
ALTER TABLE `paper_trade` ADD `cumulative_realized_pnl_usd` real DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE `paper_trade` ADD `realized_event_count` integer DEFAULT 0 NOT NULL;