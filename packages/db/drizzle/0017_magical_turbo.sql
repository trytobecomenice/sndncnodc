CREATE TABLE `daily_portfolio_snapshots` (
	`date` text PRIMARY KEY NOT NULL,
	`snapshot_at` integer NOT NULL,
	`total_equity` real NOT NULL,
	`total_cash` real NOT NULL,
	`total_unrealized_pnl` real NOT NULL,
	`realized_pnl_today` real NOT NULL,
	`active_traders_followed` integer NOT NULL
);
