CREATE TABLE `shadow_patient_exit` (
	`id` text PRIMARY KEY NOT NULL,
	`wallet_address` text NOT NULL,
	`market_slug` text NOT NULL,
	`outcome` text NOT NULL,
	`position_key` text NOT NULL,
	`shares` real NOT NULL,
	`init_price` real NOT NULL,
	`floor_price` real NOT NULL,
	`current_price` real NOT NULL,
	`immediate_exit_price` real NOT NULL,
	`close_reason` text NOT NULL,
	`status` text DEFAULT 'pending' NOT NULL,
	`resolved_price` real,
	`created_at` integer DEFAULT (unixepoch()) NOT NULL,
	`last_repriced_at` integer,
	`resolved_at` integer
);
--> statement-breakpoint
CREATE INDEX `shadow_patient_exit_status_idx` ON `shadow_patient_exit` (`status`);