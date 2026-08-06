CREATE TABLE `early_rejection_capture` (
	`id` text PRIMARY KEY NOT NULL,
	`bot_event_id` text NOT NULL,
	`captured_at` integer NOT NULL,
	`rejection_code` text NOT NULL,
	`wallet_address` text,
	`market_slug` text,
	`outcome` text,
	`source_trade_id` text,
	`source_price` real,
	`source_size_usd` real,
	`raw_evidence_table` text DEFAULT 'bot_event_log' NOT NULL,
	`analysis_state` text DEFAULT 'BLOCKED_UNTIL_LEDGER_V2' NOT NULL,
	`capture_version` text NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `early_rejection_capture_bot_event_id_unique` ON `early_rejection_capture` (`bot_event_id`);--> statement-breakpoint
CREATE INDEX `early_rejection_capture_time_idx` ON `early_rejection_capture` (`captured_at`);--> statement-breakpoint
CREATE INDEX `early_rejection_capture_code_idx` ON `early_rejection_capture` (`rejection_code`);