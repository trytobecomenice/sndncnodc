CREATE TABLE `wallet_approval_request` (
	`id` text PRIMARY KEY NOT NULL,
	`wallet_address` text NOT NULL,
	`requested_tier` text NOT NULL,
	`source` text NOT NULL,
	`category` text,
	`score_snapshot_json` text NOT NULL,
	`reason` text NOT NULL,
	`status` text DEFAULT 'pending' NOT NULL,
	`telegram_message_id` integer,
	`telegram_chat_id` text,
	`created_at` integer DEFAULT (unixepoch()) NOT NULL,
	`resolved_at` integer
);
--> statement-breakpoint
CREATE INDEX `wallet_approval_request_wallet_tier_status_idx` ON `wallet_approval_request` (`wallet_address`,`requested_tier`,`status`);--> statement-breakpoint
CREATE INDEX `wallet_approval_request_status_idx` ON `wallet_approval_request` (`status`);