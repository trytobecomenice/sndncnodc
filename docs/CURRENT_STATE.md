# Current State — Copy Bot Only

> **Economic-integrity alert — 2026-08-07:** historical Paper PnL is quarantined after an external
> review identified post-event/replayed-trade phantom profits and Codex independently reproduced
> the core finding against the authoritative AWS database read-only. Read
> `docs/research/PAPER_LEDGER_INTEGRITY_INCIDENT_2026-08-07.md` before using any PnL, wallet rank,
> cap, mute, promotion, HWM, or drawdown figure. The external review's exact local figures are not
> AWS truth; use the verified causal-classifier figures below.
>
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
- **Authoritative AWS read-only reproduction (2026-08-07 about 00:37 HKT):** 641 closed normal
  Copy Bot rows, `$4,332.84` cost, raw row-ledger PnL `+$620.40`. A versioned causal classifier
  identifies 455 high-confidence replay/post-event candidates carrying `+$669.00`; the 186-row
  candidate-clean remainder is `-$48.60`, `-4.60%` ROI, 48.39% win rate. This disproves the old
  profit claim but remains a forensic classification, not demonstrated live alpha.
- **External/local scope remains separate:** the reviewer's laptop sample was 457 closes /
  `+$519.16`; its 127 / `-$59.86` result came from treating all 330 sub-ten-minute closes as bad.
  We confirmed the raw local total but rejected holding time alone as ground truth. The causal
  classifier on that same local DB flags 316 rows / `+$524.59`, leaving 141 / `-$5.43` /
  `-0.75%` ROI.
- **Post-stale-guard AWS cohort:** 65 closes, `$385.85` cost, `-$33.06`, `-8.57%` ROI. The external
  claim of 14 / `-$40.74` was a different snapshot and is not current AWS truth.
- **AWS equity HWM:** `$2,173.60` at the read-only check, not the reviewer's older/local
  `$1,853.36`; it remains quarantined because historical phantom PnL fed the ratchet.
- **Open book at the earlier live check:** 56 positions with `$356.26` recorded cost basis. The
  markability statement below describes that point-in-time operational check, not clean strategy
  profitability.
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
5. Historical Paper PnL, wallet EV/ranking, circuit-breaker outcomes, and equity HWM are
   contaminated by replayed/post-event phantom positions. A local isolation patch is tested but
   not yet applied to AWS; treat downstream economic decisions as quarantined until controlled
   migration, classification, HWM reseed, and restart verification complete.

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
