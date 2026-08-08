# Ledger v2 + TTP quarantine controlled deployment

Status: **NOT EXECUTED**. This runbook does not start the research epoch and
does not authorize Live, a wallet change, an HWM reset, or larger sizing.

## Non-negotiable preconditions

1. `LIVE_MODE=False`; autodeploy remains locked; watchdog is paused for the
   maintenance window.
2. Record Git SHA, the one real `bot.py` PID, recorder state, DB byte size,
   filesystem total/free bytes, and existing backups.
3. The backup destination must satisfy both:
   - projected utilization after a full-size backup is at most 85%; and
   - free bytes are at least `DB size + max(0.5 * DB size, 15% of volume)`.
4. If the root volume fails either test, move a verified old backup off-volume
   or use another mounted volume. Never begin migration/backfill hoping space
   will last. Never delete the only verified recovery point.
5. The release contains ledger migrations/backfill, readiness-gated readers,
   shadow/challenger allocations, TTP quarantine and its tests. The seven-day
   qualification clock is zero until this final release is running.

## One maintenance window

1. Pause watchdog/autodeploy and verify the Phase-0 recorder is independent of
   `app.db`. Do not stop it merely to make bot health look cleaner.
2. Gracefully SIGTERM the single Paper bot. Wait for persistence and verify
   there are **zero** real `bot.py` processes. If zero cannot be proven, stop.
3. Create a non-overwriting recovery point with
   `backup_sqlite_online.py`; retain its SHA-256 and require
   `PRAGMA integrity_check = ok`. Because the bot is stopped first, restoring
   this backup cannot discard post-backup bot trades.
4. Pull the reviewed release and apply additive migrations 0024–0027. Do not
   create the readiness key manually.
5. Generate a fresh reconciliation-v3 report *after the stop*. It must include
   `bot_filtered`, `shadow_rehab`, and `shadow_challenger`, with strategy in the
   allocation key. Require zero unmatched and zero ambiguous events.
6. Record the exact report SHA-256. Run the backfill without `--apply`, then
   apply that same byte-for-byte report with its exact expected SHA.
7. The apply transaction must upsert allocations, rebuild cumulative lot PnL,
   prove zero unresolved/missing events across all three strategies, and write
   the readiness key last. Any exception rolls the entire transaction back.
8. Before restart, independently verify:
   - readiness allocator/report versions and SHA;
   - zero unresolved/missing allocation events;
   - cumulative totals by strategy and phantom status;
   - no mutation of immutable event PnL, legacy final-row PnL, or phantom flags;
   - all decision-reader queries select `cumulative_realized_pnl_usd` when the
     key is present and legacy PnL when it is absent.
9. Start exactly one Paper bot. Its first `realized_ledger_reader_status` event
   must say `reader_column=cumulative_realized_pnl_usd`, readiness true and
   unresolved zero. Verify wallet EV, rolling mute rebuild, rehab, challenger,
   replacement ranking, portfolio realized and clean stats against independent
   SQL—not merely the presence of a key.
10. Unpause watchdog only after it identifies that same one PID. Keep
    autodeploy locked through the observation window.

## Failure and rollback semantics

- Before readiness promotion, migrations are additive and old code/readers can
  coexist with them. Leave the key absent, keep the bot stopped, diagnose and
  rerun the idempotent backfill. Do **not** restore the DB just because an
  allocation validation failed.
- The promote transaction cannot leave a partial ledger with readiness true.
- If an independent post-promotion check fails, stop the bot and remove/disable
  readiness under an explicit incident record so readers fall back to legacy;
  do not reset HWM or alter historical PnL. Repair and re-promote from the same
  stopped state.
- Restore the verified backup only for actual SQLite/schema corruption, not an
  ordinary validation failure. Record exactly which writes would be lost.

## Qualification boundary

The seven-day clock starts after the final release and successful cutover—not
after migration, and never across a restart/code change. Historical allocations
use `allocation_source='historical_backfill'` and are excluded from termination
UNKNOWN qualification. UNKNOWN <=1% applies only to in-window, live-classifier,
matched terminations. Active `QUARANTINED_UNPRICEABLE` positions fail the epoch
precondition even though their high-frequency retries are suppressed.

Quarantine is not accounting finality. It stops repeated syscalls and sends one
operator alert, while the position stays unpriceable and remains in risk/SLO.
Only an official resolved outcome or a real executable exit can close it.
`SYSTEM_CENSORED` may label research evidence; it must never fabricate economic
PnL or silently release capital.
