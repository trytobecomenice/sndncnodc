ALTER TABLE `wallet_profile` ADD `recommended_status` text;--> statement-breakpoint
ALTER TABLE `wallet_profile` ADD `recommendation_reason` text;--> statement-breakpoint
ALTER TABLE `wallet_profile` ADD `recommendation_source` text;--> statement-breakpoint
ALTER TABLE `wallet_profile` ADD `recommendation_version` text;--> statement-breakpoint
ALTER TABLE `wallet_profile` ADD `recommendation_at` integer;--> statement-breakpoint
ALTER TABLE `wallet_profile` ADD `derived_metrics_source` text;--> statement-breakpoint
ALTER TABLE `wallet_profile` ADD `derived_metrics_version` text;--> statement-breakpoint
ALTER TABLE `wallet_profile` ADD `derived_metrics_ready` integer DEFAULT false NOT NULL;