# Current Handoff — Read This First

**Authority:** this is the single current operational handoff for the Polymarket Copy Bot.
Historical detail remains in `CLAUDE_HANDOFF.md`, `docs/CURRENT_STATE.md`, and the rule ledgers,
but their older sections must not override this file. Never rely on chat memory alone.

**Last control-plane verification:** 2026-08-18 HKT, read-only against AWS.
**Last full equity review:** 2026-08-16 11:43:31 UTC.

## New-session instruction

Paste this exact message in a new Codex session after opening this repository:

> Read `CURRENT_HANDOFF.md` completely first. Treat it as the current authority, then read the
> linked safety/risk sections needed for the task. Verify local git status and verify AWS state
> read-only before acting. Continue the stopped Copy Bot recovery; do not reset the kill switch,
> start the bot, remove either lock, change `LIVE_MODE`, or use stale/indicative prices as
> executable evidence unless every gate in the handoff currently passes. Preserve unrelated dirty
> files and update this handoff in the same commit as any production-state change.

## Current production truth

- AWS SSH alias: `polymarket-copybot`.
- AWS repository: `/home/ubuntu/polymarket-copybot`.
- Application/risk-code baseline verified locally and on AWS: `910244b`. A later
  documentation-only handoff commit may be the repository HEAD without changing runtime behavior.
- `LIVE_MODE=False`; this project remains Paper-only.
- Bot process count: `0`.
- Kill switch: **latched**, original trigger timestamp
  `2026-08-10T20:01:56.455740+00:00`.
- `data/watchdog_paused`: present.
- `data/autodeploy.lock`: present.
- The 24-hour stability epoch has **not** started.
- Do not remove either lock and do not start the bot merely to collect evidence.
- AWS has intentional dirty `config.py` deployment overrides. Never hard-reset or overwrite them.

## What was repaired and verified

The stopped ledger-v4 forensic/reconciliation work is complete:

- 35,579 realized events classified before backfill.
- 31,970 events matched exactly.
- 3,609 events retained as `historical_unreconstructable`; decision/research readers exclude them.
- Risk equity includes unknown historical losses but gives unknown historical gains zero credit.
- Missing, unresolved, economic, lot, quantity, sequence, and seal failures are all zero.
- Production ledger integrity is `PASS`.
- The apparent `-$18.6k` equity was caused by legacy cross-strategy Shadow Rehab PnL pollution,
  not a verified Copy Bot drawdown.
- The single quantity mismatch was a round-each-component-before-summing error, fixed by summing
  exact decimals and quantizing once. No reconciliation plug was written.
- Bullpen-derived discovery/scoring data is provenance-gated away from automatic roster and sizing
  decisions. Bullpen remains execution-only; Paper mode performs no execution calls.
- Unprovenanced sizing fails to the explicit `$3` constant, not the `$5` base size.
- Entry interlock recovery is automatic after sustained healthy samples; it has no manual-clear
  step.

Key commits, in order:

- `e6abf9b` — quarantine unreconstructable history.
- `568d434` — declare interlock automatic recovery.
- `467d5b7` — classify non-executable equity marks.
- `4a829af` — count factual resolved payouts as redeemable value.
- `910244b` — record the refused production reset.

Final local Python regression at the handoff: **888 passed, 5 skipped**.

## Why the bot is still stopped

The resolved-aware AWS review file is:

`data/backups/kill-switch-equity-review-resolved-aware-20260816.json`

Its last results were:

| Check | Result |
| --- | ---: |
| Ledger integrity | `PASS` |
| Open Copy Bot positions | 51 |
| Normal risk-mark equity | `$995.89`, no trigger |
| Strict executable/redeemable equity | `$818.69` |
| Required floor | `$900.00` |
| Fresh executable bid | 21 positions |
| Stale book | 25 positions |
| Resolved/redeemable | 1 position |
| Price/metadata error | 4 positions |
| Stale-bid diagnostic equity | `$951.60` — non-executable |

Strict equity still breaches the floor. Stale bids are diagnostic only and cannot authorize a
reset. The remaining blocker is genuine old-inventory liquidity/market-lifecycle risk, not ledger
integrity. A duplicate-looking `(wallet, market, outcome)` was checked: the two rows belonged to
different strategies (`bot_filtered` and `shadow_rehab`); the active clean Copy Bot book has zero
duplicate open keys.

## Mandatory continuation order

1. Preserve Paper mode, stopped bot, watchdog pause, and autodeploy lock.
2. Read `docs/copy-trading/RISK_MANAGEMENT.md` Rule 27 addendum and
   `docs/copy-trading/SAFETY.md` section 70.
3. Verify local dirty files before editing. They belong to the user unless proven otherwise.
4. Verify AWS commit/process/locks/kill state read-only; do not trust this snapshot blindly.
5. Run a fresh resolved-aware equity review from the repaired ledger.
6. Investigate every `price_error` and stale-book concentration without substituting midpoint,
   last trade, stale bid, cost, or a guessed resolution price.
7. If ledger integrity is not `PASS`, stop. Do not backfill or reset.
8. If either normal risk triggers or strict executable/redeemable triggers remain, do not reset.
9. Only when both trigger lists are empty may `reset_kill_switch.py --clear` be considered. Its
   exact-trigger-timestamp and fresh-reviewed-equity guards must pass unchanged.
10. After a valid reset, inspect all five entry-interlock inputs. Ledger/audit health should recover
    automatically after its sustained healthy window; latency failures require a separate root
    cause and must not be manually cleared.
11. Start exactly one Paper bot process, confirm Phase-0 writes and expected sizing/roster behavior,
    then remove only the watchdog pause. Keep autodeploy locked.
12. Observe 24 hours for process stability, recorder coverage, sizing provenance, roster stability,
    and risk-gate behavior. This is **not** an alpha test.
13. Remove the autodeploy lock only after the full stability window passes. Strategy alpha needs
    weeks of delayed, executable, fee/slippage-adjusted net-PnL evidence.

## Never repeat these failure modes

- Never let an unversioned/unprovenanced derived number affect roster, sizing, promotion, PnL,
  HWM, or kill-switch decisions.
- Never treat rich vendor fields as truth merely because they exist; verify source, mapping,
  coverage, and invariants first.
- Never backfill an unclassified cohort. Freeze source cutoffs/digests, classify clean/phantom/new,
  dry-run on a real DB copy, and require conservation before production writes.
- Never fix a conservation mismatch by clamping or inserting a balancing entry. Explain it.
- Never equate indicative, midpoint, last-trade, or stale bid with executable liquidation value.
- Never clear a latch first and wait to see whether it re-latches. Recompute from repaired facts,
  evaluate the current condition, then decide.
- Never manually clear the entry interlock; identify which of its five inputs failed.
- Never infer production truth from the laptop database or a historical handoff. Query AWS
  read-only and timestamp the evidence.
- Never describe a 24-hour stability observation as strategy alpha.
- Never stage unrelated dirty files or secrets. Use explicit file names, not `git add .`.
- Never hard-reset the AWS checkout.

## Mac migration checklist

Git preserves code and this handoff, but it deliberately does not preserve credentials or local
uncommitted work.

1. Before leaving the old Mac, separately back up every intentional dirty/untracked file shown by
   `git status --short`. They are not part of this handoff commit.
2. Push committed code to GitHub. Verify the new Mac checks out at least commit `910244b` or later.
3. Transfer SSH keys/config through a secure channel or password manager, never through Git or
   chat. Restore the `polymarket-copybot` SSH alias and verify it read-only.
4. Restore `.env`/tokens from the secret manager, not from this repository. Never paste them into
   a prompt.
5. Open the repository, paste the new-session instruction above, and require the agent to report
   local HEAD, dirty files, AWS HEAD, Paper mode, process count, both locks, and kill status before
   any mutation.

## Documents that remain authoritative for rules/history

- `docs/copy-trading/RISK_MANAGEMENT.md`
- `docs/copy-trading/SAFETY.md`
- `docs/copy-trading/LEDGER_V2_DEPLOYMENT_RUNBOOK.md`
- `docs/research/LEDGER_V2_FORENSIC_STOP_RULE_2026-08-11.md`
- `docs/research/PAPER_LEDGER_INTEGRITY_INCIDENT_2026-08-07.md`

Whenever production state changes, update this file in the **same scoped commit**, including the
verification timestamp, deployed commit, tests, AWS process/lock state, exact remaining blocker,
and next permitted action. A production change is incomplete until this handoff is current.
