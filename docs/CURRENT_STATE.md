# Current State — Copy Bot Only

> **Last live verification:** 2026-08-05 03:50 HKT. Read `CLAUDE_HANDOFF.md` for the exact AWS
> evidence, P0 replay fix, restart gate, and open operational risks.

## Repository scope

This repository contains only the Polymarket Copy Bot. Other trading systems are maintained in
separate workspaces and are outside this repository's code, schema, documentation, database,
deployment, capital, and PnL scope.

The former secondary-bot package and documentation were removed from this repository on
2026-08-05. Historical Drizzle migration files remain immutable because they may already be
recorded in deployed migration journals; the current schema and latest removal migration are the
authority for fresh/current databases.

## Production truth

- **Mode:** paper trading only; `LIVE_MODE=False`.
- **AWS host:** `polymarket-copybot`, repository path
  `/home/ubuntu/polymarket-copybot`.
- **Roster:** 17 tracked wallets, 5 circuit-breaker muted, 12 eligible for normal copying at the
  last live check.
- **Normal Copy Bot book:** 634 closed positions with cumulative realized PnL `+$639.74`; 56 open
  positions with `$356.26` cost basis at the last live check.
- **HKT 2026-08-05 realized PnL at 03:50:** `$0.00`.
- **Risk state:** kill switch inactive; all 56 open positions had a successful recent mark and no
  open position was more than 24 hours stale.
- **Runtime:** the P0-fixed paper process was PID `75742`; verify live before relying on this PID.
- **Services:** watchdog, autodeploy, Telegram approval listener, daily wallet scan/score workflow,
  and OMS shadow mirror were deployed at the last check.

## Current operational risks

1. EC2 has two intentional uncommitted `config.py` overrides
   (`TRACKED_TRADERS_SOURCE="db"`, `ENABLE_OMS_SHADOW_MIRROR=True`). Do not hard-reset the host.
2. A newly Telegram-approved wallet changes the DB but does not enter the in-memory poll set until
   a safe Copy Bot restart.
3. Shadow Rehab storage/accounting growth needs retention work; it must remain excluded from normal
   Copy Bot exposure and PnL.
4. Historical PnL can remain concentrated in a small number of wallets even when the roster count
   appears diversified.

## 2026-08-05 repository separation

1. **When:** 2026-08-05 HKT.
2. **Files adjusted:**
   - Removed the secondary-bot package and its documentation from this repository.
   - Removed its table definitions from `packages/db/src/schema.ts` and generated a forward
     removal migration without applying destructive SQL to the live AWS database.
   - Updated `README.md`, `.claudeprompt`, `ROADMAP.md`, dashboard comments, Copy Bot comments,
     `CLAUDE_HANDOFF.md`, this file, and the pnpm lockfile to describe a Copy-Bot-only repository.
3. **What changed:**
   - Source, dependency, documentation, schema, and deployment ownership are now repository-level
     separated, not merely process-level isolated.
   - No file, database, process, log, scheduler, or secret in the separately maintained bot
     workspace was changed.
   - No `.env`, key, credential, or unrelated dirty-worktree file was staged or pushed.
