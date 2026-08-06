ALTER TABLE `paper_trade_realized_allocation` ADD `termination_cause` text NOT NULL;--> statement-breakpoint
ALTER TABLE `paper_trade_realized_allocation` ADD `source_shares_at_termination` real;--> statement-breakpoint
ALTER TABLE `paper_trade_realized_allocation` ADD `termination_classifier_version` text NOT NULL;