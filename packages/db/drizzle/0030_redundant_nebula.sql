CREATE TABLE `bot_event_sequence_counter` (
	`singleton` integer PRIMARY KEY NOT NULL,
	`next_value` integer NOT NULL
);
--> statement-breakpoint
ALTER TABLE `bot_event_log` ADD `event_sequence` integer DEFAULT 0 NOT NULL;--> statement-breakpoint
-- One-time conversion only: capture the current physical insertion order as
-- durable evidence before any future VACUUM/table rebuild/dump-reload can
-- change implicit rowids.  Runtime code never reads rowid as authority.
UPDATE `bot_event_log` SET `event_sequence`=`rowid`;--> statement-breakpoint
CREATE UNIQUE INDEX `bot_event_log_event_sequence_unique_idx` ON `bot_event_log` (`event_sequence`);--> statement-breakpoint
INSERT INTO `bot_event_sequence_counter` (`singleton`,`next_value`)
SELECT 1,COALESCE(MAX(`event_sequence`),0)+1 FROM `bot_event_log`;--> statement-breakpoint
ALTER TABLE `paper_trade_realized_allocation` ADD `event_sequence` integer DEFAULT 0 NOT NULL;
