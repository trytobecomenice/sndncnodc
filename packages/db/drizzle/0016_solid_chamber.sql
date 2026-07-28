ALTER TABLE `wallet_profile` ADD `capital_multiplier` real;--> statement-breakpoint
ALTER TABLE `wallet_profile` ADD `next_rescore_due_at` integer;--> statement-breakpoint
CREATE INDEX `wallet_profile_next_rescore_due_idx` ON `wallet_profile` (`next_rescore_due_at`);