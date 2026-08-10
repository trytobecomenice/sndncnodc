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
5. The release contains ledger migrations/backfill through 0028, append-only
   retention seals, shares conservation, shared Gate A/B definitions and
   preflight, readiness-gated readers, shadow/challenger allocations, TTP
   quarantine and its tests. The seven-day
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
   Before changing the DB, run `seal_chain_manifest.py verify` against the
   last off-volume manifest. Compare `chain_head_sha256`, `seal_count`,
   `latest_range_end`, and seal-table presence. Missing/mismatched history
   stops deployment; a valid shorter chain is still a failure, not a new
   baseline. The initial migration may bootstrap an explicit genesis manifest
   only when seal count is factually zero under the incident record.
4. Pull the reviewed release and apply additive migrations 0024–0028. Do not
   create the readiness key manually. On the first 0028 deployment only,
   record the explicit genesis external anchor after the empty seal table
   exists and before any bot/prune can run; its table-presence field must be
   true and seal count zero. Use a canonical `seal-000000000000-genesis.json`
   filename inside `LEDGER_SEAL_MANIFEST_DIR`. Later deployments must verify,
   never re-bootstrap.
5. Generate a fresh reconciliation-v3 report *after the stop*. It must include
   `bot_filtered`, `shadow_rehab`, and `shadow_challenger`, with strategy in the
   allocation key. Require zero unmatched and zero ambiguous events.
6. Record the exact report SHA-256. Run the backfill without `--apply`, then
   apply that same byte-for-byte report with its exact expected SHA.
7. The apply transaction must upsert allocations, rebuild both cumulative lot
   PnL and its paired cumulative realized cost basis, prove zero
   unresolved/missing events across all three strategies, run the E/A/L
   integrity audit inside the uncommitted transaction, and write the readiness
   key last. Any invariant failure or exception rolls the entire transaction
   back. A cumulative numerator must never be divided by the mutable remaining
   `paper_trade.cost_basis_usd` field.
8. Before restart, independently verify:
   - readiness allocator/report versions and SHA;
   - zero unresolved/missing allocation events;
   - cumulative totals by strategy and phantom status;
   - no mutation of immutable event PnL, legacy final-row PnL, or phantom flags;
   - all decision-reader queries select the paired
     `cumulative_realized_pnl_usd` and
     `cumulative_realized_cost_basis_usd` when the key is present, and paired
     legacy PnL/basis when it is absent. A readiness key without either
     cumulative column is not ready.
   - `preflight_protocol_v2.py` reports the exact Gate A/B numerator,
     denominator, threshold and reason for every gate;
   - E/A/L money and shares invariants pass, and every already-pruned realized
     range reproduces its append-only retention seal and previous-hash chain.
   - export the latest retention-seal chain head into the non-overwriting,
     externally stored recovery manifest. In-database triggers do not protect
     against a same-owner process dropping the trigger and rewriting history.
9. Start exactly one Paper bot. Its first `realized_ledger_reader_status` event
   must say `reader_column=cumulative_realized_pnl_usd`,
   `reader_cost_basis_column=cumulative_realized_cost_basis_usd`, readiness
   true and unresolved zero. Verify wallet EV, rolling mute rebuild, rehab, challenger,
   replacement ranking, portfolio realized and clean stats against independent
   SQL—not merely the presence of a key.
10. Unpause watchdog only after it identifies that same one PID. Keep
    autodeploy locked through the observation window.
11. After post-restart integrity verification, verify the newest independent
    anchor again. Every later destructive realized-event prune automatically
    verifies the previous anchor, stages the next manifest before DB commit,
    and atomically finalizes it afterward. That finalized file becomes the
    required comparison input for the next prune/maintenance window; writing
    it never substitutes for the comparison.
    If a process crash leaves one `.pending.json`, keep pruning stopped and run
    `seal_chain_manifest.py recover-pending`; it finalizes only when the pending
    state exactly equals the committed DB and otherwise fails without rewriting
    either side.

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
matched terminations. Gate A excludes known quarantines from its network-fetch
denominator; Gate B exposes them separately. Frozen legacy identities do not
hide any quarantine created inside the qualification window: a new one resets
the seven-day clock.

Rate evidence requires at least 10,000 fetch attempts and 1,800 sweeps;
termination UNKNOWN evidence requires at least 100 final lots. A structural
suspect must be adjudicated within 24 hours and no suspect may remain when the
window qualifies. Classification is prospective only: failed calls recorded
before factual quarantine stay in Gate A even when that restarts the window.

Quarantine is not accounting finality. It stops repeated syscalls and sends one
operator alert, while the position stays unpriceable and remains in risk/SLO.
Only an official resolved outcome or a real executable exit can close it.
`SYSTEM_CENSORED` may label research evidence; it must never fabricate economic
PnL or silently release capital.

This release is the epoch-v2 build freeze. Once deployed, changes other than a
documented completeness-blocking integrity/exit-safety fix go to
`docs/research/EPOCH_V3_BACKLOG.md`. Every allowed code deployment resets the
qualification clock.
