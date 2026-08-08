ALTER TABLE `paper_trade_realized_allocation` ADD `shares_closed` real;--> statement-breakpoint
ALTER TABLE `paper_trade_realized_allocation` ADD `shares_remaining` real;--> statement-breakpoint
ALTER TABLE `paper_trade` ADD `total_acquired_shares` real;--> statement-breakpoint
CREATE TABLE `paper_trade_event_seal` (
	`id` text PRIMARY KEY NOT NULL,
	`range_start` integer NOT NULL,
	`range_end` integer NOT NULL,
	`event_count` integer NOT NULL,
	`pnl_micros` integer NOT NULL,
	`shares_micros` integer NOT NULL,
	`canonical_sha256` text NOT NULL,
	`previous_chain_sha256` text NOT NULL,
	`chain_sha256` text NOT NULL,
	`sealer_version` text NOT NULL,
	`sealed_at` integer DEFAULT (unixepoch()) NOT NULL
);--> statement-breakpoint
CREATE UNIQUE INDEX `paper_trade_event_seal_range_unique` ON `paper_trade_event_seal` (`range_start`,`range_end`);--> statement-breakpoint
CREATE TRIGGER `paper_trade_event_seal_no_update` BEFORE UPDATE ON `paper_trade_event_seal`
BEGIN SELECT RAISE(ABORT, 'paper_trade_event_seal is append-only'); END;--> statement-breakpoint
CREATE TRIGGER `paper_trade_event_seal_no_delete` BEFORE DELETE ON `paper_trade_event_seal`
BEGIN SELECT RAISE(ABORT, 'paper_trade_event_seal is append-only'); END;
