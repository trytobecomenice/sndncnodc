CREATE TABLE `token_registry` (
	`token_id` text PRIMARY KEY NOT NULL,
	`market_slug` text NOT NULL,
	`outcome` text NOT NULL,
	`updated_at` integer DEFAULT (unixepoch()) NOT NULL
);
