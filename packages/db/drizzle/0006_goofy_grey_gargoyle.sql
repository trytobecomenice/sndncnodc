ALTER TABLE `weather_position` ADD `ensemble_prob` real;--> statement-breakpoint
ALTER TABLE `weather_position` ADD `polymarket_prob` real;--> statement-breakpoint
ALTER TABLE `weather_position` ADD `probability_difference` real;--> statement-breakpoint
ALTER TABLE `weather_position` ADD `is_same_day` integer;--> statement-breakpoint
ALTER TABLE `weather_position` ADD `station_local_time` text;--> statement-breakpoint
ALTER TABLE `weather_position` ADD `temp_buffer_f` real;--> statement-breakpoint
ALTER TABLE `weather_position` ADD `full_kelly_fraction` real;--> statement-breakpoint
ALTER TABLE `weather_position` ADD `applied_fraction` real;