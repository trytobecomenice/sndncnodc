CREATE TABLE `weather_ensemble_forecast` (
	`id` text PRIMARY KEY NOT NULL,
	`station_id` text NOT NULL,
	`forecast_for` text NOT NULL,
	`issued_at` integer DEFAULT (unixepoch()) NOT NULL,
	`model` text NOT NULL,
	`member_index` integer NOT NULL,
	`t_max_f` real NOT NULL,
	`t_min_f` real NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `weather_ensemble_forecast_unique_idx` ON `weather_ensemble_forecast` (`station_id`,`forecast_for`,`issued_at`,`model`,`member_index`);--> statement-breakpoint
CREATE INDEX `weather_ensemble_forecast_lookup_idx` ON `weather_ensemble_forecast` (`station_id`,`forecast_for`,`model`);