ALTER TABLE `paper_trade` ADD `is_phantom` integer DEFAULT false NOT NULL;--> statement-breakpoint
ALTER TABLE `paper_trade` ADD `phantom_reason` text;--> statement-breakpoint
ALTER TABLE `paper_trade` ADD `phantom_classifier_version` text;--> statement-breakpoint
ALTER TABLE `paper_trade` ADD `phantom_classified_at` integer;