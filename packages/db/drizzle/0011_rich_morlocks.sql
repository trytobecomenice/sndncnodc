CREATE TABLE `live_whale_event` (
	`id` text PRIMARY KEY NOT NULL,
	`wallet_address` text NOT NULL,
	`contract_address` text NOT NULL,
	`event_type` text NOT NULL,
	`direction` text NOT NULL,
	`token_id` text NOT NULL,
	`share_amount` text NOT NULL,
	`usdc_amount` real,
	`price` real,
	`tx_hash` text NOT NULL,
	`log_index` integer NOT NULL,
	`block_number` integer NOT NULL,
	`detected_at` integer DEFAULT (unixepoch()) NOT NULL,
	`consumed_at` integer
);
--> statement-breakpoint
CREATE UNIQUE INDEX `live_whale_event_tx_log_idx` ON `live_whale_event` (`tx_hash`,`log_index`);--> statement-breakpoint
CREATE INDEX `live_whale_event_consumed_idx` ON `live_whale_event` (`consumed_at`);