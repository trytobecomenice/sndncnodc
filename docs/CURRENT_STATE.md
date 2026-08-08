# Current State — Copy Bot Only

> **Economic-integrity alert — 2026-08-07:** historical Paper PnL is quarantined after an external
> review identified post-event/replayed-trade phantom profits and Codex independently reproduced
> the core finding against the authoritative AWS database read-only. Read
> `docs/research/PAPER_LEDGER_INTEGRITY_INCIDENT_2026-08-07.md` before using any PnL, wallet rank,
> cap, mute, promotion, HWM, or drawdown figure. The external review's exact local figures are not
> AWS truth; use the verified causal-classifier figures below.
>
> **Last live verification:** 2026-08-07 02:19 HKT. Read `CLAUDE_HANDOFF.md` for the exact AWS
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
- **P0 integrity isolation deployed:** AWS has 455 rows marked by classifier
  `paper-ledger-integrity-v1`; manifest SHA-256 starts `75755957`. No rows or PnL were deleted or
  rebased, and zero open positions matched the classifier.
- **Factual resolution audit and v2 isolation:** Gamma `closedTime`/`umaEndDate` auditing of all
  186 v1-clean rows produced 155 legit, 31 factual phantom, and zero unknown. All 23 residual
  sub-ten-minute `resolved` rows were factual phantom, but duration remains a screen rather than
  the classifier; eight additional factual phantom rows closed through `source_sell`. AWS marked
  those 31 rows with `paper-ledger-resolution-facts-v2` under manifest SHA-256
  `a2b4c7c20cd16aaa70092402a72d51b4886c9f57f8491d1458574f4f210dd820`. No raw row or PnL was
  deleted/rebased and HWM was not changed.
- **Current fact-clean row ledger:** 155 closes, `$865.51` cost, `-$78.50`, `-9.07%` ROI, 45.16%
  win rate. This is not economic realized PnL because a `source_sell` row stores only its final
  partial-close event.
- **Exact event/tax-lot reconciliation:** all 14,614 realized events matched exactly one tax lot;
  unmatched=0 and ambiguous=0. Clean closed-lot cumulative realized PnL is `-$20.364972`; open-lot
  partial realized PnL is `-$0.004762`; total clean portfolio realized is `-$20.369734`. Phantom
  events contributed `+$717.385620`. Open-lot unrealized PnL is separate, so this is not total
  equity or final strategy expectancy.
- **Superseded row-close uncertainty diagnostic:** 10,000-draw all-row bootstrap on final-row PnL
  gave cost-weighted ROI 95% CI
  `[-19.90%, +1.90%]`; wallet-cluster bootstrap gives `[-35.42%, -1.30%]` with effective wallet
  N=10. Current-eligible rows are 101 trades across only six effective wallets and their
  wallet-cluster ROI CI is `[-52.56%, +3.53%]`. These intervals are not economic strategy CIs
  because final rows omit earlier partial-close PnL. Rerun both bootstraps on allocated cumulative
  lot PnL; do not change the roster from this sample.
- **Open book at verification:** 52 clean/unclassified positions; all 52 received a fresh mark at
  2026-08-07 01:44 HKT. This is markability evidence, not strategy profitability.
- **Risk state:** contaminated HWM `$2,173.60` was removed after backup and classification; the
  restarted bot cleanly reseeded HWM to `$1,112.60`.
- **Runtime:** exactly one paper process PID `105229`, still loaded from the earlier controlled
  restart; AWS checkout is `164c7bd`. The autodeploy lock intentionally prevents an implicit main
  bot restart.
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
5. Historical Paper PnL remains auditable but quarantined. Confirmed phantom rows are now excluded
   from wallet EV/ranking, rolling evidence, dashboards, open-state loading, and clean PnL readers.
   Existing mute/status decisions were deliberately not auto-reversed; review them manually from
   clean evidence.
6. `pnl_snapshot` is no longer empty: 22 rows were present at 02:17 HKT (11 portfolio and 11
   clean-epoch). The writer was delayed, not absent: repeated full scans of the multi-million-row
   event log make the Paper loop enter long I/O waits. Commit `3315698` removes one duplicate scan,
   but the running PID has not been restarted onto that code. Snapshot portfolio realized PnL is
   still wrong because runtime subtracts final-row phantom PnL instead of allocated phantom event
   PnL; durable allocation accounting/backfill is the next P0 implementation.
7. At 02:19 HKT Codex found two concurrent `bot.py` processes. The cron watchdog had trusted only
   a missing/stale shared `bot.pid` and started a duplicate even though PID `105229` was alive.
   Both cron duplicates were terminated, leaving PID `105229`. Commit `164c7bd` now discovers all
   same-repository bot processes through `/proc`, repairs a missing PID file, serializes starts with
   an OS file lock, and alerts/fails closed if duplicates exist. A live missing-PID simulation and
   the next cron tick both left exactly one process.
8. **P0 ledger v2 is implemented locally, not yet deployed/promoted:** migrations 0024–0027 add
   immutable realized-event allocations, per-lot cumulative PnL, event-time termination causes,
   and raw early-rejection pointers. Migration alone cannot change equity: the new ledger becomes
   authoritative only after an exact-SHA backfill proves zero unmatched/ambiguous/missing events
   and atomically writes its readiness key. Protocol v2 remains `DRAFT_NOT_FROZEN`; no epoch clock
   or Live authority exists.
9. **Qualification instrumentation is implemented locally:** one aggregate `ttp_sweep_observation`
   per sweep records the attempted/successful/executable-bid denominator plus latency. New
   resolution closes snapshot source inventory at event time. Historical resolution closes without
   that evidence remain `UNKNOWN`; no retrospective intent is invented.
10. **2026-08-09 local hardening, still not deployed:** every economic decision reader is now
    readiness-gated to cumulative allocated PnL; reconciliation v3 covers real, rehab and
    challenger strategies without cross-strategy matching. AWS Telegram alarms were traced
    read-only to three permanently unreadable slugs (699/703 alarm-class rows in 24h), not a kill,
    duplicate, 429 or auth failure. Persistent quarantine now stops high-frequency retries after
    three structural failures, sends one specific alert, retains the position in risk and the SLO
    denominator, and leaves official resolution to the low-frequency sweep. Deployment is blocked
    by disk preflight: free `8,537,518,080` bytes is below required `9,696,513,433`; another local
    backup would project 88.61% utilization. No AWS mutation was attempted.

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
