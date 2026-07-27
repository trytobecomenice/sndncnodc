CREATE TABLE `pending_exit_order` (
	`id` text PRIMARY KEY NOT NULL,
	`wallet_address` text NOT NULL,
	`market_slug` text NOT NULL,
	`outcome` text NOT NULL,
	`position_key` text NOT NULL,
	`shares` real NOT NULL,
	`init_price` real NOT NULL,
	`floor_price` real NOT NULL,
	`current_price` real NOT NULL,
	`bullpen_order_id` text,
	`close_reason` text NOT NULL,
	`status` text DEFAULT 'pending' NOT NULL,
	`created_at` integer DEFAULT (unixepoch()) NOT NULL,
	`last_repriced_at` integer,
	`filled_at` integer
);
--> statement-breakpoint
CREATE INDEX `pending_exit_order_status_idx` ON `pending_exit_order` (`status`);