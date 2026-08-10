-- Migration 0028 was deliberately hand-authored before its Drizzle snapshot
-- existed.  Its seal/quantity objects are already live; 0029 adds only the
-- missing denominator paired with cumulative realized PnL.
ALTER TABLE `paper_trade` ADD `cumulative_realized_cost_basis_usd` real DEFAULT 0 NOT NULL;
