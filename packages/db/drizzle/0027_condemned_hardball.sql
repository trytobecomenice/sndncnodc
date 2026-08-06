CREATE TABLE `paper_trade_termination_state` (
	`paper_trade_id` text PRIMARY KEY NOT NULL,
	`ttp_eligible_pricing_failure_count` integer DEFAULT 0 NOT NULL,
	`exit_signal_unexecutable_count` integer DEFAULT 0 NOT NULL,
	`updated_at` integer DEFAULT (unixepoch()) NOT NULL
);
