DROP INDEX `weather_station_external_id_unique`;--> statement-breakpoint
CREATE UNIQUE INDEX `weather_station_external_id_source_unique_idx` ON `weather_station` (`external_id`,`source`);